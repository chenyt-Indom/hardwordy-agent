# -*- coding: utf-8 -*-
"""回归：验证入口本身不能「假绿」，写盘必须走 safeio。

这一轮修掉的三类真故障，都属于**「验证看起来通过了，其实没验」**，
所以必须用断言钉住，否则下次重构又会悄悄退回去：

  1. `regress.py` 只有 `run()` 没有 `__main__` —— 直接跑这个文件
     只是 import 一遍就退出：**0.1 秒、零输出、退出码 0**。
     自动化里这是最危险的假绿（「跑过了」其实一条用例都没执行）。
  2. `sessions._write` / `iterloop` 的三处写盘各自裸调 `os.replace`，
     Windows 文件锁下会抛 WinError 5（前端正读文件时必现）。
     现在必须全部走 `safeio`，不能再有裸 `os.replace`。
  3. `safeio` 的临时文件名原来固定是 `path + ".tmp"`，
     并发写者会互相把对方的 tmp 替换走 —— 必须带进程/线程/随机后缀。

跑法：py -3 backend/_feature_tests/test_verify_entrypoints.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

OK, BAD = [], []


def ck(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra and not cond else ""))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_regress_has_entrypoint():
    """regress 必须能当脚本跑，且打印中文结论。"""
    s = _src("backend/regress.py")
    ck("regress 有 __main__ 入口", 'if __name__ == "__main__":' in s)
    ck("regress 有 _main_cli", "def _main_cli(" in s)
    ck("_main_cli 会真的调 run()", re.search(r"r\s*=\s*run\(scope=scope\)", s) is not None)
    ck("_main_cli 用退出码表达成败", "return 0 if r.get(\"ok\") else 1" in s)
    ck("regress 输出按 UTF-8（否则中文结论是乱码）",
       "sys.stdout.reconfigure(encoding=\"utf-8\"" in s)


def test_no_bare_os_replace():
    """写盘不许再裸调 os.replace —— 必须经 safeio（带重试）。"""
    for rel in ("backend/sessions.py", "backend/iterloop.py"):
        s = _src(rel)
        n = len(re.findall(r"os\.replace\(", s))
        ck("%s 没有裸 os.replace" % rel, n == 0, "还有 %d 处" % n)

    ck("sessions._write 走 safeio",
       "safeio.atomic_write_json(path, data, indent=2)" in _src("backend/sessions.py"))
    it = _src("backend/iterloop.py")
    ck("iterloop 引入了 safeio", "from . import safeio" in it)
    ck("账本写盘走 safeio", "safeio.atomic_write_json(_ledger_path()" in it)
    ck("失败计数写盘走 safeio", "safeio.atomic_write_json(_att_path()" in it)
    ck("卡片写盘走 safeio", "safeio.atomic_write_text(_cards_path()" in it)


def test_safeio_tmp_is_unique():
    """临时文件名必须按写者唯一，否则并发会互相踩踏。"""
    s = _src("backend/safeio.py")
    ck("tmp 名带 pid", "os.getpid()" in s)
    ck("tmp 名带线程 id", "threading.get_ident()" in s)
    ck("tmp 名带随机后缀", "uuid.uuid4().hex" in s)
    ck("有替换重试", "def _replace_with_retry" in s)
    ck("全失败时降级不丢数据", "退化为原地覆盖" in s)


def test_safeio_behaviour():
    """真跑一遍：被拒后重试成功；一直失败也不丢数据。"""
    import tempfile
    from unittest import mock
    from backend import safeio

    d = tempfile.mkdtemp(prefix="entry_")
    p = os.path.join(d, "a.json")
    real = os.replace
    st = {"n": 0}

    def flaky(a, b):
        st["n"] += 1
        if st["n"] <= 1:
            raise PermissionError(5, "被占用")
        return real(a, b)

    with mock.patch("backend.safeio.os.replace", side_effect=flaky):
        how = safeio.atomic_write_json(p, {"k": 1})
    ck("被拒一次后重试成功", how == "replace", how)

    with mock.patch("backend.safeio.os.replace", side_effect=PermissionError(5, "x")):
        how2 = safeio.atomic_write_json(p, {"k": 2})
    ck("一直失败时降级写成功", how2 == "overwrite", how2)
    import json
    with open(p, encoding="utf-8") as f:
        ck("降级后数据是新的", json.load(f) == {"k": 2})


def test_no_prod_data_write_in_tests():
    """测试不许直接写生产数据：危险调用所在文件必须有隔离手段。"""
    d = os.path.join(ROOT, "backend", "_feature_tests")
    risky = re.compile(r"(SD\.write\(|_do_selfdev\(\s*\"selfdev_write\"|CFG\.save_config\()")
    guard = re.compile(r"(MM_DATA_DIR|_task_path\s*=|load_config\s*=\s*_load|mkdtemp)")
    offenders = []
    for fn in sorted(os.listdir(d)):
        if not (fn.startswith("test_") and fn.endswith(".py")):
            continue
        s = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
        if risky.search(s) and not guard.search(s):
            offenders.append(fn)
    ck("危险写盘测试都做了数据隔离", not offenders, offenders)


def main():
    print("=" * 62)
    print("验证入口与安全写盘回归")
    print("=" * 62)
    for fn in (test_regress_has_entrypoint, test_no_bare_os_replace,
               test_safeio_tmp_is_unique, test_safeio_behaviour,
               test_no_prod_data_write_in_tests):
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            import traceback
            traceback.print_exc()
            BAD.append("%s 抛异常: %s" % (fn.__name__, e))

    print("\n通过 %d / 失败 %d" % (len(OK), len(BAD)))
    for b in BAD:
        print("   !! " + str(b))
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
