"""验证观察窗起点改用「探针首次命中时间」（2026-11-24）。

实测缺陷：原来 base 取文件最后编辑时间，导致**任何无关改动**都把 5 条教训的
progress 清零（教训落地率 0.354→0.147，RSI 71.1→66.0）。这里断言：
  ① 首次命中时间一旦记下就不再被后续调用覆盖；
  ② 修复消失 / 复发会清掉它（不能拿旧进度冒充）；
  ③ 真的把探针文件 mtime 推到"现在"后，progress 不归零（这条最要紧）。
"""
from __future__ import annotations

import io
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _utf8():
    """只改编码，不新建 sys.stdout 对象（新建会在模块回收时关闭 regress 的缓冲）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                    # noqa: BLE001
        pass
sys.path.insert(0, ROOT)

from backend import lessonloop as L      # noqa: E402

_results = []
KEY = "__test_hit_ts__"


def check(name, cond, extra=""):
    _results.append((name, bool(cond), extra))
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))


def main():
    _utf8()
    # 清掉可能残留的测试键
    L._clear_hit_ts(KEY)

    # ① 首次记下后不被覆盖
    t1 = L._first_hit_ts(KEY, 1000.0)
    t2 = L._first_hit_ts(KEY, 2000.0)
    check("首次命中时间被记下", t1 == 1000.0, t1)
    check("后续调用不覆盖（这是修的核心）", t2 == 1000.0, t2)
    check("落盘了", KEY in L._hit_state(), list(L._hit_state().keys())[:5])

    # ② 清掉后重新计时
    L._clear_hit_ts(KEY)
    t3 = L._first_hit_ts(KEY, 3000.0)
    check("清掉后重新计时", t3 == 3000.0, t3)
    L._clear_hit_ts(KEY)
    check("清掉后键真的没了", KEY not in L._hit_state())

    # ③ 真实场景：把探针文件 mtime 推到"现在"，progress 不应归零
    a0 = L.audit()
    before = {r["kind"]: r.get("progress") for r in a0["rows"] if r.get("progress") is not None}
    check("改动前有可判定的教训", len(before) >= 1, list(before)[:3])

    target = os.path.join(ROOT, "backend", "lessonloop.py")
    old = os.stat(target).st_mtime
    try:
        os.utime(target, (time.time(), time.time()))     # 只改 mtime，不动内容
        a1 = L.audit()
        after = {r["kind"]: r.get("progress") for r in a1["rows"] if r.get("progress") is not None}
        # 只比较两轮都有的条目
        common = [k for k in before if k in after]
        check("有可比条目", len(common) >= 1, common[:3])
        dropped = [k for k in common if (after[k] or 0) < (before[k] or 0) - 1e-9]
        check("mtime 变化后 progress 不下降（核心修复）", not dropped,
              {"before": {k: before[k] for k in common},
               "after": {k: after[k] for k in common},
               "dropped": dropped})
    finally:
        os.utime(target, (old, old))                     # 还原 mtime

    bad = [x for x in _results if not x[1]]
    print("\n通过 %d / 失败 %d" % (len(_results) - len(bad), len(bad)))
    for n, _, e in bad:
        print("  FAILED:", n, e)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
