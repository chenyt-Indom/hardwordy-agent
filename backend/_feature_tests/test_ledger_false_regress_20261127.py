# -*- coding: utf-8 -*-
"""回归：假退步（口径不一致）与重复记账。

对应 2026-11-27 实测事故（用户看到曲线右端垂直掉到 0）：
  ① 11:10 那次评分里 regress_part() 返回 None → ability 掉了
     regress_pass(103) + fix_landed(45) = 148 分 → total 280.21 → 132.29；
  ② 账本据此记了 -147.92，累计分 95.71 → -52.21；
  ③ 11:10:52「自动修复后重评」复用**同一份**评分又记一次 → -200.13。

本测试锁三件事：
  A. judge() 遇到口径不同的两次评分 → 不判进退（delta=None）；
  B. 回归没跑起来时，ability 沿用上一版回归派生项（不当作掉分）；
  C. 同一份评分（after.iso 相同）只记一次账。

⚠️ 全程在临时目录里跑：patch IL._data_dir，并屏蔽 bench 子进程与 RSI 采样，
   不碰真实账本 / 真实曲线。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                   # noqa: BLE001
    pass

from backend import iterloop as IL          # noqa: E402
from backend import lessonloop as LL        # noqa: E402

ok, bad = [], []


def check(name, cond, detail=""):
    if cond:
        ok.append(name)
        print("  [OK]", name)
    else:
        bad.append(name)
        print("  [NG]", name, ("| " + str(detail)) if detail else "")


def _score(total, rows, static=None, regress=None, func=None):
    return {"total": total, "quality": total, "iso": "",
            "ability": {"score": sum(float(r["score"]) for r in rows), "rows": rows},
            "static": static, "regress": regress, "func": func}


def main():
    print("=" * 64)
    print("账本 · 假退步 / 重复记账（2026-11-27 事故回归）")
    print("=" * 64)
    tmp = tempfile.mkdtemp(prefix="hwa_false_regress_")

    old_data = IL._data_dir
    old_bv, old_rbl = IL.bench_verdict, IL.read_bench_last
    old_env = os.environ.get("HWA_BENCH")
    old_s1, old_s2 = LL.rsi_sample_score, LL.rsi_sample_iter
    old_lesson = IL._lesson_fixed_rows
    IL._data_dir = lambda: tmp
    os.environ["HWA_BENCH"] = os.path.join(tmp, "__no_bench__.py")
    IL.bench_verdict = lambda window=6: {"ok": False}
    IL.read_bench_last = lambda: None
    LL.rsi_sample_score = lambda: {"ok": False, "why": "test"}
    LL.rsi_sample_iter = lambda: {"ok": False, "why": "test"}

    st = {"score": 39.5, "max": 40}
    fn = {"score": 30.0, "max": 30}
    rg = {"score": 29.71, "max": 30}
    full_before = _score(280.21, [], st, rg, fn)
    full_before["iso"] = "2026-11-27 11:01:47"
    no_regress_after = _score(132.29, [], st, None, fn)
    no_regress_after["iso"] = "2026-11-27 11:10:18"

    try:
        # ---------------- A. 口径不一致不判进退 ----------------
        print("\n[A] judge() 拒绝跨口径比较")
        v = IL.judge(full_before, no_regress_after)
        check("判成 incomparable", v.get("verdict") == "incomparable",
              "verdict=%r" % v.get("verdict"))
        check("delta 为 None（不产出假数字）", v.get("delta") is None,
              "delta=%r" % v.get("delta"))
        check("不许回滚", v.get("should_rollback") is False)
        check("说明里点出是哪两项口径不同",
              "regress" in str(v.get("note") or ""),
              "note=%r" % v.get("note"))

        # 口径相同 → 正常判进退，不能被误伤
        v2 = IL.judge(_score(100.0, [], st, rg, fn), _score(102.0, [], st, rg, fn))
        check("同口径 +2 仍判 improved",
              v2.get("verdict") == "improved" and abs(v2.get("delta") - 2.0) < 1e-6,
              "verdict=%r delta=%r" % (v2.get("verdict"), v2.get("delta")))
        v3 = IL.judge(_score(102.0, [], st, rg, fn), _score(100.0, [], st, rg, fn))
        check("同口径 -2 仍判 regressed 且要求回滚",
              v3.get("verdict") == "regressed" and v3.get("should_rollback") is True,
              "verdict=%r" % v3.get("verdict"))

        # ---------------- B. 回归没跑起来时沿用上一版 ----------------
        print("\n[B] ability_part：回归缺测时沿用上一版（不当作掉分）")
        with open(os.path.join(tmp, "last_score.json"), "w", encoding="utf-8") as fh:
            json.dump({"total": 280.21, "ability_rows": [
                {"key": "regress_pass", "label": "回归用例通过", "count": 103,
                 "unit": 1.0, "score": 103.0},
                {"key": "fix_landed", "label": "失败用例翻通过", "count": 15,
                 "unit": 3.0, "score": 45.0},
                {"key": "lesson_fixed", "label": "教训转为已修复", "count": 5,
                 "unit": 5.0, "score": 25.0},
            ]}, fh, ensure_ascii=False)

        IL._lesson_fixed_rows = lambda: None
        ab = IL.ability_part(None, {"smoke": [], "symbols_ok": False, "py_files": 0})
        keys = [r.get("key") for r in ab["rows"]]
        check("regress_pass 被沿用", "regress_pass" in keys, "keys=%r" % keys)
        check("fix_landed 被沿用", "fix_landed" in keys, "keys=%r" % keys)
        check("沿用项合计 148 分没丢", ab["score"] >= 148.0,
              "score=%r" % ab["score"])
        check("沿用项都标了 carried",
              all(r.get("carried") for r in ab["rows"] if r.get("key") in
                  ("regress_pass", "fix_landed")))
        check("不带 carried 标记的项不会被误标",
              all(not r.get("carried") for r in ab["rows"]
                  if r.get("key") not in ("regress_pass", "fix_landed")))
        check("lesson_fixed 不从 last_score 里偷（它是现算的）",
              "lesson_fixed" not in keys, "keys=%r" % keys)
        check("返回值带 carried_regress 标记", ab.get("carried_regress") is True)

        # 回归正常跑起来时 → 用实测值，不许沿用旧值
        ab2 = IL.ability_part({"passed": 90, "all_names": [], "failed_names": []},
                              {"smoke": [], "symbols_ok": False, "py_files": 0})
        check("回归正常时用实测 90 分（不沿用 103）",
              any(r.get("key") == "regress_pass" and abs(r["score"] - 90.0) < 1e-6
                  for r in ab2["rows"]),
              "rows=%r" % [(r.get("key"), r.get("score")) for r in ab2["rows"]])
        check("回归正常时不标 carried_regress", ab2.get("carried_regress") is False)

        # ---------------- C. 同一份评分只记一次账 ----------------
        print("\n[C] commit_round：同一份评分不重复累加")
        a1 = _score(120.0, [], st, rg, fn)
        a1["iso"] = "2026-11-27 11:10:18"
        b1 = _score(200.0, [], st, rg, fn)
        b1["iso"] = "2026-11-27 11:01:47"

        r1 = IL.commit_round(before=b1, after=a1, note="第一次记（真退步）")
        check("第一次照常记 -80", abs(float(r1["cum"]) + 80.0) < 1e-6,
              "cum=%r delta=%r" % (r1.get("cum"), r1.get("delta")))
        cum_after_first = float(IL.load_ledger().get("cum") or 0.0)

        r2 = IL.commit_round(before=b1, after=a1, note="同一份评分再来一次")
        check("第二次 delta 归 0", abs(float(r2["delta"] or 0)) < 1e-9,
              "delta=%r" % r2.get("delta"))
        check("第二次判成 duplicate", r2.get("verdict") == "duplicate",
              "verdict=%r" % r2.get("verdict"))
        check("累计分没有被扣第二遍",
              abs(float(IL.load_ledger().get("cum") or 0.0) - cum_after_first) < 1e-9,
              "cum=%r vs %r" % (IL.load_ledger().get("cum"), cum_after_first))
        check("重复轮不增加轮次计数", int(IL.load_ledger().get("rounds") or 0) == 1,
              "rounds=%r" % IL.load_ledger().get("rounds"))

        # 换一份新评分（iso 不同）→ 必须照常记
        a2 = _score(110.0, [], st, rg, fn)
        a2["iso"] = "2026-11-27 11:20:00"
        r3 = IL.commit_round(before=a1, after=a2, note="新一轮真退步")
        check("新评分照常记 -10", abs(float(r3["delta"] or 0) + 10.0) < 1e-6,
              "delta=%r" % r3.get("delta"))
        check("累计分继续累加",
              abs(float(IL.load_ledger().get("cum") or 0) + 90.0) < 1e-6,
              "cum=%r" % IL.load_ledger().get("cum"))

        # 口径不一致的轮：delta 记 null，累计分不动
        r4 = IL.commit_round(before=full_before, after=no_regress_after,
                             note="口径不一致那轮")
        check("口径不一致 → delta 为 None", r4.get("delta") is None,
              "delta=%r" % r4.get("delta"))
        check("口径不一致 → 累计分不变",
              abs(float(IL.load_ledger().get("cum") or 0) + 90.0) < 1e-6,
              "cum=%r" % IL.load_ledger().get("cum"))

        # ---------------- D. 源码级护栏 ----------------
        print("\n[D] 源码护栏（防止以后被改回去）")
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "iterloop.py"), encoding="utf-8").read()
        check("judge 里有口径检查", "_score_scope(before)" in src)
        check("commit_round 里有指纹去重", "last_committed_sig" in src)
        check("ability_part 有沿用分支", "_carried_regress_rows()" in src)
        check("读曲线时跳过作废轮", 'if r.get("void")' in src)
    finally:
        IL._data_dir = old_data
        IL.bench_verdict, IL.read_bench_last = old_bv, old_rbl
        IL._lesson_fixed_rows = old_lesson
        LL.rsi_sample_score, LL.rsi_sample_iter = old_s1, old_s2
        if old_env is None:
            os.environ.pop("HWA_BENCH", None)
        else:
            os.environ["HWA_BENCH"] = old_env

    print("\n" + "-" * 64)
    print("通过 %d / 失败 %d" % (len(ok), len(bad)))
    if bad:
        for b in bad:
            print("  失败：", b)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
