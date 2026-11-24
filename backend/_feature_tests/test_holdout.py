# -*- coding: utf-8 -*-
"""留出集（holdout）的离线回归 —— 不跑尺子、不联网、不改代码。

用户 2026-11-24 转述的专家意见（L3 三缺之二）：
  「没有留出集（所有改进都在同一批对话上验证，正是技能里自己写的
    『过拟合式伪进步』）」

这里验四类事：
  1) **划分本身合法**：题号都在题库里、不重复、每个类别都留到、可见/留出不相交；
  2) **算分正确**：分组分数、gap、过拟合判定；
  3) **真的不泄露**：失分列表过滤掉留出集；bench_brief 生成的真实提示词里
     一个字都不许出现留出集题号（这是「留出」二字的全部意义）；
  4) **口径漂移能看出来**：明细里冒出题库外的题号要报 unknown，不能默默算错。

跑法：python backend/_feature_tests/test_holdout.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import holdout as HO                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _detail(fail_ids=(), extra=None):
    d = {}
    for cid in HO.ALL_IDS:
        d[cid] = {"ok": cid not in fail_ids, "w": 1.0,
                  "why": "" if cid not in fail_ids else "答偏了", "s": 5.0}
    if extra:
        d.update(extra)
    return d


def main():
    print("=" * 62)
    print("留出集 holdout")
    print("=" * 62)

    # ---- 1. 划分合法 ----
    ho = HO.holdout_ids()
    check("留出集非空", len(ho) > 0, "实际 %d" % len(ho))
    check("留出集都在题库里", all(c in HO.ALL_IDS for c in ho),
          "越界：%r" % [c for c in ho if c not in HO.ALL_IDS])
    check("留出集无重复", len(ho) == len(set(ho)))
    check("留出比例在 20%~50% 之间（太小等于没留，太大没题可练）",
          0.2 <= len(ho) / float(len(HO.ALL_IDS)) <= 0.5,
          "%d/%d" % (len(ho), len(HO.ALL_IDS)))
    # 每个类别（id 前缀）至少留一题
    cats = {}
    for c in HO.ALL_IDS:
        cats.setdefault(c.split("-")[0], []).append(c)
    ho_cats = set(c.split("-")[0] for c in ho)
    check("每个类别都留到了题", set(cats) == ho_cats,
          "没留到的类别：%r" % sorted(set(cats) - ho_cats))
    check("可见集与留出集不相交",
          not (set(ho) & set(HO.train_ids(HO.ALL_IDS) or ())))
    check("可见集 + 留出集 = 题库全集",
          set(HO.train_ids(HO.ALL_IDS) or ()) | set(ho) == set(HO.ALL_IDS))

    # ---- 2. 算分 ----
    d0 = _detail()
    sp = HO.split_detail(d0)
    check("全过时：可见集 100%", sp["train"]["pct"] == 100.0, repr(sp["train"]))
    check("全过时：留出集 100%", sp["holdout"]["pct"] == 100.0, repr(sp["holdout"]))
    check("全过时：gap=0 且不过拟合", sp["gap"] == 0.0 and sp["overfit"] is False)

    # 只在可见集失一题 → 留出集仍 100%，gap 为负，不算过拟合
    sp1 = HO.split_detail(_detail(["fail-03"]))
    check("只可见集失分 → 留出集仍 100%", sp1["holdout"]["pct"] == 100.0, repr(sp1["holdout"]))
    check("只可见集失分 → gap<0 且不算过拟合", sp1["gap"] < 0 and sp1["overfit"] is False,
          "gap=%r overfit=%r" % (sp1["gap"], sp1["overfit"]))

    # 可见集 90% / 留出集 60% → gap=30，必须判过拟合
    sp2 = HO.split_detail(_detail(["fail-03", "bnd-02", "ctx-02"]))
    check("可见 90% / 留出 60% → gap=30", sp2["gap"] == 30.0, "gap=%r" % sp2["gap"])
    check("gap 到 30 → 判过拟合", sp2["overfit"] is True)
    check("过拟合时提示语出现「背题」", "背题" in HO.brief(sp2), HO.brief(sp2)[:120])

    # 留出集全过、可见集差 → 不能反过来说过拟合
    sp3 = HO.split_detail(_detail(["mem-01", "mem-03", "tool-01", "tool-02"]))
    check("可见集差、留出集满分 → 不算过拟合", sp3["overfit"] is False,
          "gap=%r" % sp3["gap"])

    # 空 / 坏输入
    sp4 = HO.split_detail({})
    check("空明细 → ok=False 并给原因", sp4["ok"] is False and sp4["why"], repr(sp4["why"]))
    sp5 = HO.split_detail(None)
    check("None 明细不崩", sp5["ok"] is False)

    # 口径漂移：题库外的题号
    sp6 = HO.split_detail(_detail(extra={"zzz-99": {"ok": True, "w": 1.0}}))
    check("题库外题号 → 报 unknown", "zzz-99" in (sp6.get("unknown") or []),
          repr(sp6.get("unknown")))

    # ---- 3. 不泄露 ----
    fails = [{"id": "fail-03", "why": "可见集失分"},
             {"id": "bnd-02", "why": "留出集失分"},
             {"id": "ctx-02", "why": "留出集失分"}]
    vis = HO.visible_failures(fails)
    check("visible_failures 只留可见集", [x["id"] for x in vis] == ["fail-03"],
          repr([x["id"] for x in vis]))
    check("hidden_failure_count = 2", HO.hidden_failure_count(fails) == 2)
    check("leak_check 能抓出题号", HO.leak_check("失分：bnd-02") == ["bnd-02"])
    check("leak_check 对干净文本返回空", HO.leak_check("今天尺子 15/15") == [])

    # ★ 最关键的一条：拿真实提示词生成函数跑一遍，一个字都不许出现留出集题号
    try:
        from backend import iterloop as IL
        fake = {"iso": "2026-11-24T20:00:00", "pass": 9.0, "total": 15.0, "pct": 60.0,
                "detail": _detail(["fail-03", "bnd-02", "ctx-02"])}
        _orig = IL._bench_rows
        IL._bench_rows = lambda: [fake]
        try:
            txt = IL.bench_brief()
        finally:
            IL._bench_rows = _orig
        leaked = HO.leak_check(txt)
        check("bench_brief 生成的提示词零泄露", not leaked, "泄露：%r" % leaked)
        check("可见集失分仍然照常列出（不是一刀切全删）", "fail-03" in txt)
        check("留出集只报数量", "另有 2 题" in txt, txt[:200])
        check("提示词里带留出集分数", "留出集" in txt and "%" in txt)
    except Exception as e:                               # noqa: BLE001
        check("能跑通 bench_brief", False, "%s: %s" % (type(e).__name__, e))

    # ---- 4. 接线：反思提示词必须真的走这套过滤 ----
    src = ""
    try:
        with open(os.path.join(_ROOT, "backend", "iterloop.py"), encoding="utf-8") as fh:
            src = fh.read()
    except Exception as e:                               # noqa: BLE001
        check("能读 iterloop.py", False, str(e)[:120])
    check("bench_brief 调用了 visible_failures",
          "visible_failures" in src)
    check("bench_brief 调用了 hidden_failure_count",
          "hidden_failure_count" in src)
    check("bench_brief 注入了留出集分数（score_split）",
          "score_split(" in src)
    check("bench_verdict 把原始 detail 带出去了",
          'out["detail"] = detail' in src)

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
