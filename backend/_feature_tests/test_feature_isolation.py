# -*- coding: utf-8 -*-
"""回归：feature 断言用例之间必须**绝对隔离**，一条脏补丁不许传染别的用例。

真实故障史（2026-11-24）
------------------------
`test_handoff_card.py` 里有一行

    real_ss.get_messages = lambda s: fake     # real_ss = backend.sessions

改了真模块的全局属性且**不还原**。而 regress 原来是把断言文件
exec_module 进**当前进程**再调 run()/main()，于是排在它后面的
`test_surrogate_safety` / `test_selfiter_resilience` 读会话时拿到那个假
返回值 → 两条断言必挂，**单独跑却全过**；而且失败原因只有
「main() 返回 1」，看不出挂在哪条断言上，长期被误判成「功能坏了」。

现在的做法：一条断言一个**子进程**（regress._feature_runner）。本文件守四件事：
  1. 跑一条「故意污染全局」的断言，当前进程的 backend.sessions 必须没被改；
  2. 这种断言失败时，原因里必须带它自己打印的输出（能定位到哪条断言）；
  3. 正常的断言文件照样判通过（别把隔离做成一律失败）；
  4. run_all.py / conftest.py 这种基础设施不能被当成断言用例。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DIRTY = '''# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, %(root)r)
from backend import sessions as S
S.get_messages = lambda sid: [{"role": "user", "content": "假的"}]
print("  FAIL 故意写的一条失败断言")
sys.exit(1)
'''

_CLEAN = '''# -*- coding: utf-8 -*-
import sys
print("  ok   干净断言")
sys.exit(0)
'''


def run() -> int:
    from backend import regress, sessions

    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(detail) if detail else ""))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="mm_iso_")
    dirty = os.path.join(tmp, "test_dirty_probe.py")
    clean = os.path.join(tmp, "test_clean_probe.py")
    with open(dirty, "w", encoding="utf-8") as f:
        f.write(_DIRTY % {"root": _ROOT})
    with open(clean, "w", encoding="utf-8") as f:
        f.write(_CLEAN)

    # 1) 脏断言：必须判失败，且**不能**污染当前进程
    before = sessions.get_messages
    why = ""
    raised = False
    try:
        regress._feature_runner(__import__("pathlib").Path(dirty), timeout=60)()
    except AssertionError as e:
        raised = True
        why = str(e)
    except Exception as e:                              # noqa: BLE001
        why = "%s: %s" % (type(e).__name__, e)
    ck("脏断言被判失败（不是静默通过）", raised, why[:120])
    ck("当前进程的 sessions.get_messages 没被改", sessions.get_messages is before)
    ck("失败原因带用例自己的输出（能定位断言）", "故意写的一条失败断言" in why, why[:160])
    ck("失败原因带退出码", "退出码 1" in why, why[:120])

    # 2) 干净断言：必须判通过
    ok_err = ""
    passed = False
    try:
        r = regress._feature_runner(__import__("pathlib").Path(clean), timeout=60)()
        passed = r is None
    except Exception as e:                              # noqa: BLE001
        ok_err = "%s: %s" % (type(e).__name__, e)
    ck("干净断言判通过（隔离没做成一律失败）", passed, ok_err[:120])

    # 3) 基础设施文件不能被当成断言用例
    names = [c["name"] for c in regress._load_dynamic()]
    ck("run_all.py 不作为断言用例", "feature.run_all" not in names)
    ck("conftest.py 不作为断言用例", "feature.conftest" not in names)
    ck("真断言文件仍在用例里", any(n.startswith("feature.test_") for n in names), names[:3])
    ck("用例名不重复", len(names) == len(set(names)))

    # 4) 每条动态用例都得是可调用的
    ck("动态用例都有可调用 fn",
       all(callable(c.get("fn")) for c in regress._load_dynamic()))

    print("\n%s（失败 %d 项）" % ("通过" if not fails else "未通过", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
