# -*- coding: utf-8 -*-
"""RSI 主指标 + 教训判定（2026-11-23）。

背景：用户要「RSI 那种能力」—— 反思能记住、能改进、评分斜率逐渐平滑上升。
上一轮实测发现三处真问题，本文件把它们钉住，避免改回去：

  1. judge() 原来 `if edited <= t0: pending(只说不做)` ——
     实测 2 条 pending 的探针 code_hit 全是 True（修复逻辑明明在代码里），
     只因文件 mtime 早于教训记录就被判成没修，落地率被压成 0/6，
     RSI 的「教训落地率」项恒为 0。现在以 **probe 命中** 为主证据。
  2. PROBES 里 "模型输出不可用"→`parse_fail`、"字段名/键读错"→`field_miss`
     指向的是**记录失败事件**的代码，恒命中 = 假阳性（比不检查更危险）。
     现在指向真修复逻辑（_close_truncated / safe_get）。
  3. rsi_index() 只给瞬时值，没有历史序列 → 判断不了「斜率是否逐渐变陡」。
     现在 rsi_sample() 落盘、rsi_trend() 用三段斜率判形态。

全程用 MM_DATA_DIR 隔离，不碰真实账本。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="mm_rsi_test_")
os.environ["MM_DATA_DIR"] = _TMP

from backend import lessonloop as ll  # noqa: E402

_ok = 0
_fail = 0


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


def _write_jsonl(name, rows):
    d = os.path.join(_TMP, "self_iterate")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _reset_rsi_cache():
    ll._RSI_CACHE.update({"ts": 0.0, "val": None})


def main():
    now = time.time()

    # ---------- 1. judge：探针命中就该算修了，不看 mtime ----------
    print("\n[1] judge 判定以「代码里的修复逻辑」为主证据")
    # 教训时间设在**未来**，确保 edited <= t0（旧逻辑必然判 pending）
    _write_jsonl("lessons.jsonl", [{
        "kind": "没产生实际改动", "ts": now + 3600,
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 3600)),
        "count": 2, "fix": "diff 为空判失败", "will_fix": True}])
    a = ll.audit()
    got = [r["status"] for r in a["rows"]]
    check("探针命中 + 改动早于教训 → 不再误判「只说不做」",
          len(a["pending"]) == 0 and len(a["fixed"]) + len(a["verifying"]) == 1,
          "status=%s" % got)

    # 教训比改动**新**（同一 kind 刚被重记）→ 必须进观察窗，不能立刻判已修
    # （实测漏洞：12:32 记的教训 12:33 就判 fixed，未复发只代表才过一分钟）
    _write_jsonl("lessons.jsonl", [{
        "kind": "没产生实际改动", "ts": now, "count": 5, "fix": "z"}])
    a2 = ll.audit()
    check("刚记的教训不许立刻判已修（观察窗起点取 max(改动,教训)）",
          len(a2["fixed"]) == 0 and len(a2["verifying"]) == 1,
          str([(r["kind"], r["status"]) for r in a2["rows"]]))

    # ---------- 2. judge：探针不命中 → 未落地 ----------
    print("\n[2] 修复逻辑不在代码里 → 判未落地")
    ll.PROBES["假问题_不存在探针"] = ("backend/iter_runner.py", "绝对不存在的串_ZZZ_9f3")
    _write_jsonl("lessons.jsonl", [{
        "kind": "假问题_不存在探针", "ts": now, "count": 3, "fix": "y"}])
    a = ll.audit()
    check("探针不命中 → pending", len(a["pending"]) == 1,
          str([(r["kind"], r["status"]) for r in a["rows"]]))

    # ---------- 3. 探针表里不许再出现「记录失败事件」的名字 ----------
    print("\n[3] 假探针（记账代码里的字符串）必须清掉")
    needles = [v[1] for v in ll.PROBES.values()]
    check("parse_fail 不再当修复证据", "parse_fail" not in needles, str(needles))
    check("field_miss 不再当修复证据", "field_miss" not in needles, str(needles))
    check("探针都指向可判定的代码", all(
        isinstance(v, tuple) and len(v) == 2 for v in ll.PROBES.values()))

    # ---------- 4. rsi_sample 去重 ----------
    print("\n[4] RSI 历史采样")
    _reset_rsi_cache()
    r1 = ll.rsi_sample(force=True)
    r2 = ll.rsi_sample()
    check("首次采样写入", r1.get("ok") and r1.get("n") == 1, str(r1))
    check("同分钟重复采样被去重", r2.get("skipped") is True, str(r2))
    p = os.path.join(_TMP, "self_iterate", "rsi_history.jsonl")
    check("历史落盘", os.path.isfile(p))

    # ---------- 5. rsi_trend 形态判定 ----------
    print("\n[5] 趋势形态（三段斜率）")
    inc = [40, 41, 42, 45, 48, 51, 56, 62, 68]          # 斜率 1 → 3 → 6
    _write_jsonl("rsi_history.jsonl", [
        {"ts": now - 1000 + i, "iso": "2026-11-23 10:%02d:00" % i, "score": v}
        for i, v in enumerate(inc)])
    t = ll.rsi_trend()
    check("斜率递增 → 判 RSI 形态", t["shape"].startswith("③"),
          "%s segs=%s" % (t["shape"], [s["slope"] for s in t["segs"]]))

    flat = [50] * 9
    _write_jsonl("rsi_history.jsonl", [
        {"ts": now - 1000 + i, "iso": "2026-11-23 10:%02d:00" % i, "score": v}
        for i, v in enumerate(flat)])
    check("平线 → 判平台", ll.rsi_trend()["shape"].startswith("①"),
          ll.rsi_trend()["shape"])

    down = [70, 68, 66, 62, 58, 54, 46, 38, 30]
    _write_jsonl("rsi_history.jsonl", [
        {"ts": now - 1000 + i, "iso": "2026-11-23 10:%02d:00" % i, "score": v}
        for i, v in enumerate(down)])
    check("下滑 → 判下滑", ll.rsi_trend()["shape"].startswith("④"),
          ll.rsi_trend()["shape"])

    # ---------- 6. rsi_weakest 挑最拖后腿的一项 ----------
    print("\n[6] 最弱项 = 权重 × (1 - 得分) 最大")
    _write_jsonl("actlog.jsonl", [
        {"ts": now - 100 + i, "result": "fail", "files": []} for i in range(10)])
    _write_jsonl("lessons.jsonl", [{
        "kind": "假问题_不存在探针", "ts": now, "count": 3, "fix": "y"}])
    with open(os.path.join(_TMP, "self_iterate", "audits.json"), "w",
              encoding="utf-8") as f:
        json.dump({"items": [{"counts": {"high": 0, "mid": 0}}]}, f)
    _reset_rsi_cache()
    w = ll.rsi_weakest()
    check("动作成功率 0% 时被选为最弱项",
          w.get("ok") and w.get("name") == "动作成功率", str(w))
    check("给了具体动作建议", bool(w.get("advice")), w.get("advice") or "")

    # ---------- 7. rsi_index 缓存不改变结果 ----------
    print("\n[7] 缓存")
    s1 = ll.rsi_index(cache_sec=0)["score"]
    s2 = ll.rsi_index()["score"]
    check("缓存命中值与实时计算一致", abs(s1 - s2) < 1e-6, "%.1f vs %.1f" % (s1, s2))

    # ---------- 8. 报告/hint 能带上 RSI ----------
    print("\n[8] 对外输出")
    line = ll.rsi_line()
    check("rsi_line 含分数与最弱项", "RSI" in line and "最该修" in line, line[:90])
    h = ll.hint()
    check("hint 含 RSI 最弱项（主循环能看到该修什么）",
          "RSI 最弱项" in h, h[:80].replace("\n", "|"))

    print("\n结果：通过 %d / 失败 %d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
