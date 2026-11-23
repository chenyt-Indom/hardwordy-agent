"""验证观察窗起点改用「探针首次命中时间」（2026-11-24）。

实测缺陷：原来 base 取文件最后编辑时间，导致**任何无关改动**都把 5 条教训的
progress 清零（教训落地率 0.354→0.147，RSI 71.1→66.0）。这里断言：
  ① 首次命中时间一旦记下就不再被后续调用覆盖；
  ② 修复消失 / 复发会清掉它（不能拿旧进度冒充）；
  ③ 真的把探针文件 mtime 推到"现在"后，progress 不归零（这条最要紧）。

⚠️ 2026-11-24 并发修复：原来全程读写**真实**的 lesson_hit.json，并且用
   全局固定的测试键 `__test_hit_ts__`。两个后果：
   · 两套测试同时跑时，A 的 `_clear_hit_ts` 会清掉 B 刚写下的时间 →
     「落盘了」「后续调用不覆盖」随机失败（单跑永远绿）；
   · 更严重：③ 里 `L._clear_hit_ts(cand)` 清的是**真实教训**的首次命中时间，
     会把用户真实的观察窗进度抹掉 —— 测试污染生产数据。
   现在把 `_hit_state_path` 整体指到本进程专属的临时文件：
   各跑各的、并发不冲突，且一个字节都不碰真实账本。
"""
from __future__ import annotations

import os
import sys
import tempfile
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


def _body():
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

    # ③ 核心场景：把探针文件 mtime 推到"现在"，progress 不应归零
    #
    # ⚠️ 2026-11-24 改：原来直接拿**真实账本**里「有 progress 的条目」来比 ——
    #   账本为空（MM_DATA_DIR 指向临时目录 / 全新安装 / 教训被清过）时
    #   「改动前有可判定的教训」必挂，这是**假失败**：测的是用户数据里恰好
    #   有没有教训，而不是机制对不对。
    #   改成用一条**合成教训**：挑一个当前真能命中的探针 kind，ts 取现在，
    #   两次 judge 之间只推 mtime。这样无论账本有没有数据，都在测同一条机制。
    cand = None
    for _k, _pr in L.PROBES.items():
        if L._probe_hit(_pr[0], _pr[1]) is True:
            cand = _k
            break
    check("存在可命中的探针（合成教训用）", cand is not None)
    if cand:
        L._clear_hit_ts(cand)                 # 别拿上一次跑留下的首次命中时间
        synth = {"kind": cand, "ts": time.time(), "count": 1, "fix": "合成教训"}
        b = L.judge(synth)
        check("合成教训有可判定的 progress", b.get("progress") is not None,
              (b.get("status"), b.get("progress")))
        rel_probe = L.PROBES[cand][0]
        target = os.path.join(ROOT, rel_probe.replace("/", os.sep))
        old = os.stat(target).st_mtime
        try:
            os.utime(target, (time.time() + 5, time.time() + 5))   # 只改 mtime，不动内容
            a = L.judge(synth)
        finally:
            os.utime(target, (old, old))                           # 还原 mtime
        check("mtime 变化后 progress 不下降（核心修复）",
              (a.get("progress") or 0.0) >= (b.get("progress") or 0.0) - 1e-9,
              {"before": b.get("progress"), "after": a.get("progress"),
               "status": (b.get("status"), a.get("status"))})
        L._clear_hit_ts(cand)                 # 收尾，别把测试键留在账本里


def main():
    _utf8()
    # 隔离：本进程用独立的 lesson_hit.json，并发安全且不碰真实数据
    _tmpdir = tempfile.mkdtemp(prefix="hitstate_")
    _orig = L._hit_state_path
    L._hit_state_path = lambda: os.path.join(_tmpdir, "lesson_hit.json")
    try:
        _body()
    finally:
        L._hit_state_path = _orig

    bad = [x for x in _results if not x[1]]
    print("\n通过 %d / 失败 %d" % (len(_results) - len(bad), len(bad)))
    for n, _, e in bad:
        print("  FAILED:", n, e)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
