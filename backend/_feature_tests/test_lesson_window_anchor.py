"""观察窗起点不得被「文件 mtime」污染（2026-11-25）。

## 实测 bug（本轮真实发生）

`lesson_hit.json` 里两条教训的首次命中时间被写成了 `11-25 01:30:57` ——
正是上一次改 `backend/lessonloop.py`（只是加了几行注释）的文件 mtime，
而它们的 `first_ts` 是 `11-22`（62 小时前）。后果：

| | 修复前 | 应有 |
|---|---|---|
| progress | 0.158 | 1.0 |
| 教训落地率 | 0.311 | 0.592 |
| RSI | 76.8 | 83.8 |

根因是两处 fallback 都掺了 mtime：
  ① `_first_hit_ts(kind, max(edited, t0))` —— 把 mtime **持久化**进账本；
  ② `base = _since` —— 观察窗起点直接用了含 mtime 的值。
自迭代每次都在改这些文件，于是进度永远攒不起来，落地率在 0.31~0.45 震荡。

## 本测试断言（都是上表那个场景的精确复现）

① t0 很早 + 文件 mtime 很新 + 账本为空 → 起点仍取 t0，progress 不被清零；
② 无关编辑（只推 mtime）前后，progress 完全不变；
③ `_last_recur_ts` 能拿到最近一次复发时间（起点要「最后一次坏事之后」）；
④ 首次命中时间的 fallback 是 t0，不是 mtime。

⚠️ 全程用**临时** lesson_hit.json，一个字节都不碰真实账本（并发安全）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _utf8():
    """只改编码，不新建 sys.stdout 对象（新建会在模块回收时关掉 regress 的缓冲）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                    # noqa: BLE001
        pass


sys.path.insert(0, ROOT)

from backend import lessonloop as L      # noqa: E402

_results = []


def check(name, cond, extra=""):
    _results.append((name, bool(cond), extra))
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))


def _pick_probe():
    """挑一个当前真能命中的探针 kind（合成教训用，不依赖真实账本）。"""
    for k, pr in L.PROBES.items():
        if L._probe_hit(pr[0], pr[1]) is True:
            return k
    return None


def _body():
    cand = _pick_probe()
    check("存在可命中的探针（合成教训用）", cand is not None)
    if not cand:
        return

    rel = L.PROBES[cand][0]
    target = os.path.join(ROOT, rel.replace("/", os.sep))
    old_mtime = os.stat(target).st_mtime
    t0 = time.time() - 48 * 3600.0        # 教训是 48 小时前记的
    synth = {"kind": cand, "first_ts": t0, "ts": t0, "count": 1, "fix": "合成教训"}

    try:
        # ① 账本为空 + 文件 mtime 很新 → 起点必须还是 t0
        L._clear_hit_ts(cand)
        os.utime(target, (time.time(), time.time()))          # mtime = 现在
        a = L.judge(synth)
        check("t0 早 48h、mtime 是现在 → 判 fixed（不被清零）",
              a.get("status") == "fixed" and (a.get("progress") or 0) >= 0.999,
              {"status": a.get("status"), "progress": a.get("progress"),
               "evidence": a.get("evidence", "")[:70]})

        # ② 无关编辑：只推 mtime，progress 一分不能掉
        before = a.get("progress") or 0.0
        os.utime(target, (time.time() + 30, time.time() + 30))
        b = L.judge(synth)
        check("无关编辑后 progress 不变（核心修复）",
              (b.get("progress") or 0.0) >= before - 1e-9,
              {"before": before, "after": b.get("progress"),
               "status": (a.get("status"), b.get("status"))})

        # ③ 首次命中时间的 fallback 是 t0，不是 mtime
        L._clear_hit_ts(cand)
        got = L._first_hit_ts(cand, t0)
        check("首次命中 fallback = t0（不是 mtime）", abs(got - t0) < 1.0, got)

        # ④ 最近复发时间：有记录取最大，没有取 0
        rec = L._last_recur_ts(cand)
        check("_last_recur_ts 返回数值且 <= 现在",
              isinstance(rec, float) and 0 <= rec <= time.time() + 5, rec)
        check("_last_recur_ts 对空 kind 返回 0", L._last_recur_ts("") == 0.0)
    finally:
        try:
            os.utime(target, (old_mtime, old_mtime))          # 还原 mtime
        except OSError:
            pass
        L._clear_hit_ts(cand)                                 # 别把测试键留下


def main():
    _utf8()
    tmpdir = tempfile.mkdtemp(prefix="hitstate_anchor_")
    orig = L._hit_state_path
    L._hit_state_path = lambda: os.path.join(tmpdir, "lesson_hit.json")
    try:
        _body()
    finally:
        L._hit_state_path = orig

    bad = [x for x in _results if not x[1]]
    print("\n通过 %d / 失败 %d" % (len(_results) - len(bad), len(bad)))
    for n, _, e in bad:
        print("  FAILED:", n, e)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
