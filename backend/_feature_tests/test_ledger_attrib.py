# -*- coding: utf-8 -*-
"""账本记分 · 归因 / 刷分判定 / 曲线连续性 · 离线回归。

对应 2026-11-24 用户问的三件事：
  「账本记分是否正常？曲线好久没动，是不是我看错了？」
  「账本记分机制是什么情况，目前正不正常？」
  「反复验证修改再验证直到完全正常为止」

在真实数据里查到的三处缺陷（都有实测证据，不是猜的）：

  A. **归因漏了能力分项**
     total = quality(static/regress/func) + ability(无上限)，而 explain_delta
     只比 quality 三项。于是能力分涨了时三项全是 0，summary 写成
     「没有可归因的变化」—— 账本 delta≠0 却查不到原因。
     实测账本 17:30 +8.00 / 17:42 +4.00 / 18:11 +3.00 连续三条都是这个形态，
     涨的全是 ability 里的 fix_landed / lesson_fixed 计数。

  B. **重启复核往 events 里塞 delta，但不累加累计分**
     run_post_verify 设计上「同一状态重测，不再累加」，这是对的；
     但它把 delta 原样写进 events（实测出现 delta=+5.00 / +6.00 而 cum 没动），
     又不写 history.jsonl —— 前端按 history 画线时那段时间**一个点都没有**，
     看起来就像「曲线好久不动」。现在 delta 置 None、标 kind=post_restart。

  C. **刷分判定算不出来（grind 永远 False）**
     bench_verdict 读 history.jsonl 时按 kind=="score_round" 过滤，
     而 ledger.jsonl 的行**没有 kind 字段**；两套格式不一致，迟早出事。
     这里锁住：commit_round 写出的行必须带 kind，且「账本涨、尺子没涨」
     必须被判成 grind（verdict 不能写 improved）。

  D. 尺子读数过期（>24h）时不下刷分结论 —— 拿旧体检报告开今天的药是错的。

不联网、不碰真实数据（全部走临时目录）。
跑法：python backend/_feature_tests/test_ledger_attrib.py
"""
import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

from backend import iterloop as IL                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _score(total, ability_rows):
    """构造一份 score() 形状的结果（只留判定要用到的字段）。"""
    return {
        "total": total,
        "static": {"score": 40.0, "max": 40, "findings": []},
        "regress": {"score": 30.0, "max": 30, "failed_items": [], "passed": 79},
        "func": {"score": 30.0, "max": 30, "syntax_bad": [], "smoke": []},
        "ability": {"score": round(total - 100.0, 2), "rows": ability_rows,
                    "unbounded": True},
    }


def main():
    print("=" * 62)
    print("账本记分 · 归因 / 刷分 / 曲线连续性")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="hwa_ledger_test_")
    # ⚠️ 必须在任何 mock 之前抓住**真函数**：D 段要用它验证过期逻辑，
    #    如果这时候才取，拿到的是 B 段换上的 lambda（实测踩过：age_h 恒为 0.4）。
    _real_bv = IL.bench_verdict
    old_data, old_bench, old_bv = IL._data_dir, IL.BENCH_SCORES, IL.bench_verdict
    old_env = os.environ.get("HWA_BENCH")
    IL._data_dir = lambda: tmp
    # 防止 commit_round 里「保护区段」真的去跑尺子（会污染真实基准）
    os.environ["HWA_BENCH"] = os.path.join(tmp, "__no_bench__.py")

    rows_b = [
        {"key": "fix_landed", "label": "失败用例翻通过", "count": 10,
         "unit": 3.0, "score": 30.0},
        {"key": "regress_pass", "label": "回归用例通过", "count": 79,
         "unit": 1.0, "score": 79.0},
    ]
    rows_a = [
        {"key": "fix_landed", "label": "失败用例翻通过", "count": 11,
         "unit": 3.0, "score": 33.0},
        {"key": "regress_pass", "label": "回归用例通过", "count": 79,
         "unit": 1.0, "score": 79.0},
    ]
    before = _score(222.0, rows_b)
    after = _score(225.0, rows_a)

    try:
        # ---------- A. 归因必须覆盖能力分项 ----------
        print("\n[A] 归因（explain_delta）")
        ex = IL.explain_delta(before, after, [])
        check("能力项变化被点名", "失败用例翻通过" in ex["summary"],
              "summary=%r" % ex["summary"][:120])
        check("写明从几条到几条", "10→11" in ex["summary"],
              "summary=%r" % ex["summary"][:120])
        check("不再谎称「没有可归因的变化」",
              "没有可归因" not in ex["summary"])
        check("ability_diff 带出前后计数",
              bool(ex.get("ability_diff")) and
              ex["ability_diff"][0]["count_before"] == 10 and
              ex["ability_diff"][0]["count_after"] == 11,
              "实得 %r" % (ex.get("ability_diff") or [])[:1])
        check("没有变化的项不刷屏",
              all(abs(float(x["delta"])) > 0 for x in ex["ability_diff"]))

        # 三项都没变、能力也没变 → 才允许说「没有可归因的变化」
        ex0 = IL.explain_delta(before, before, [])
        check("真没变化时才说没变化",
              ex0["summary"] == "没有可归因的变化",
              "实得 %r" % ex0["summary"])

        # ---------- B. 刷分判定：账本涨 + 尺子没涨 → grind ----------
        print("\n[B] 刷分判定（commit_round）")
        fake_bv_grind = {
            "ok": True, "grind": True, "ledger_sum": 22.79,
            "last": {"iso": "2026-11-24T17:45:47", "pass": 12.0,
                     "total": 15.0, "pct": 80.0},
            "prev": {"iso": "2026-11-24T17:33:08", "pass": 13.0,
                     "total": 15.0, "pct": 86.7},
            "delta": -6.7, "trend": "down", "failing": [], "infra": [],
            "stale": False, "age_h": 0.4,
        }
        IL.bench_verdict = lambda window=6: dict(fake_bv_grind)
        IL.read_bench_last = lambda: None
        r = IL.commit_round(before=before, after=after, actions=[], note="测试·刷分形态")
        check("账本照加（活确实干了）", abs(float(r["cum"]) - 3.0) < 1e-6,
              "cum=%r" % r.get("cum"))
        check("verdict 不写 improved", r["verdict"] == "grind",
              "verdict=%r" % r.get("verdict"))
        check("标了 grind 标记", bool(r.get("grind")) is True)
        check("给出刷分原因（含尺子读数）",
              "尺子未涨" in str(r.get("grind_note") or ""),
              "note=%r" % r.get("grind_note"))
        check("不会因此回滚", not r.get("should_rollback"))
        check("events 里也记成 grind",
              (IL.load_ledger().get("events") or [{}])[-1].get("verdict") == "grind")
        check("jsonl 行带 kind=score_round",
              r.get("kind") == "score_round")
        check("history.jsonl 也落了 score_round",
              any(json.loads(l).get("kind") == "score_round"
                  for l in open(os.path.join(tmp, "history.jsonl"),
                                encoding="utf-8") if l.strip()))

        # 尺子真涨了 → 正常判 improved，不能误伤
        fake_bv_up = dict(fake_bv_grind)
        fake_bv_up.update({"grind": False, "delta": 13.3, "trend": "up"})
        IL.bench_verdict = lambda window=6: dict(fake_bv_up)
        r2 = IL.commit_round(before=before, after=after, actions=[], note="测试·正常进步")
        check("尺子涨 → 仍判进步", r2["verdict"] == "improved",
              "verdict=%r" % r2.get("verdict"))
        check("尺子涨 → 不打 grind 标记", not r2.get("grind"))

        # ---------- C. 重启复核不能把曲线搞断 ----------
        print("\n[C] 重启复核（run_post_verify）与曲线连续性")
        cum_before_pv = float(IL.load_ledger().get("cum") or 0.0)
        old_score = IL.score
        old_take = IL.take_post_verify
        old_push = IL._push_post_verify_card
        IL.score = lambda full=True: {"total": 225.0}
        IL.take_post_verify = lambda: {"pid": "test-pid"}
        IL._push_post_verify_card = lambda out: None
        try:
            out = IL.run_post_verify()
        finally:
            IL.score, IL.take_post_verify = old_score, old_take
            IL._push_post_verify_card = old_push
        d = IL.load_ledger()
        last_ev = (d.get("events") or [{}])[-1]
        check("复核本身成功", bool(out and out.get("ok")),
              "out=%r" % (out,))
        check("events 里 delta 置空（不再出现「+6 却不涨分」）",
              last_ev.get("delta") is None, "delta=%r" % last_ev.get("delta"))
        check("events 标 kind=post_restart",
              last_ev.get("kind") == "post_restart")
        check("真实差值仍在文字里（不丢信息）",
              "差" in str(last_ev.get("summary") or ""),
              "summary=%r" % last_ev.get("summary"))
        check("累计分不因复核而变",
              abs(float(d.get("cum") or 0.0) - cum_before_pv) < 1e-6,
              "cum=%r 之前=%r" % (d.get("cum"), cum_before_pv))

        # ---------- D. 尺子过期保护 ----------
        print("\n[D] 尺子读数过期（stale）保护")
        p_scores = os.path.join(tmp, "scores.jsonl")
        p_hist = os.path.join(tmp, "hist_stale.jsonl")
        old_t = time.time() - 30 * 3600
        iso_old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(old_t))
        iso_older = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(old_t - 3600))
        with open(p_scores, "w", encoding="utf-8") as f:
            f.write(json.dumps({"iso": iso_older, "pass": 14.0, "total": 15.0,
                                "pct": 93.3, "detail": {}}) + "\n")
            f.write(json.dumps({"iso": iso_old, "pass": 13.0, "total": 15.0,
                                "pct": 86.7, "detail": {}}) + "\n")
        with open(p_hist, "w", encoding="utf-8") as f:
            for _ in range(6):
                f.write(json.dumps({"kind": "score_round", "delta": 4.0,
                                    "iso": iso_old}, ensure_ascii=False) + "\n")
        IL.bench_verdict = _real_bv
        old_scores2, old_hist2 = IL.BENCH_SCORES, IL._hist_path
        IL.BENCH_SCORES = p_scores
        IL._hist_path = lambda: p_hist
        try:
            v = _real_bv()
        finally:
            IL.BENCH_SCORES, IL._hist_path = old_scores2, old_hist2
        check("识别出读数过期", v.get("stale") is True, "age_h=%r" % v.get("age_h"))
        check("过期时不下刷分结论", v.get("grind") is False,
              "grind=%r why=%r" % (v.get("grind"), v.get("grind_why")))
        check("过期原因写得出来", "过期" in str(v.get("grind_why") or ""))
    finally:
        IL._data_dir, IL.BENCH_SCORES, IL.bench_verdict = old_data, old_bench, old_bv
        if old_env is None:
            os.environ.pop("HWA_BENCH", None)
        else:
            os.environ["HWA_BENCH"] = old_env

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
