# -*- coding: utf-8 -*-
"""留出集（holdout）—— 挡住「在自己练过的那套题上刷分」。

为什么必须有它（专家 2026-11-24 指出的 L3 三缺之一）
----------------------------------------------------
  「没有留出集（所有改进都在同一批对话上验证，正是技能里自己写的
    『过拟合式伪进步』）」

本机的尺子是 15 道固定题。反思提示词会把「哪几题失分、为什么失分」原样喂给
模型，然后模型针对那几题改代码 —— 下一次跑尺子当然更容易过。
问题是这**证明不了能力变强**：它只证明「这套题被针对性修过了」。
典型形态：尺子从 12/15 冲到 15/15，可用户随便换个说法问同类问题照样翻车。

留出集的做法很朴素，也是机器学习里唯一被验证有效的办法：
  把题目分成两半，**一半给模型看（可见集），一半永远不给它看（留出集）**。
  · 可见集失分 → 进反思提示词，允许针对性修；
  · 留出集失分 → 只进汇总分数，**绝不告诉模型是哪一题、为什么错**；
  · 两者的差距（gap）就是过拟合的度量：可见集 100% 而留出集 60%，
    说明它在背题，不是在变强。

设计约束（很重要）
------------------
1. **划分写死在代码里**（HOLDOUT_IDS 是唯一权威）。不读外部可改文件 ——
   否则「把留出集改小」就成了最容易的刷分手段。
2. **留出集 id 不进提示词**：连题号都不给，只给「另有 N 题没通过」。
3. 读不到尺子 / 没有 detail 时返回 ok=False 并说明原因，绝不编一个分数。
4. 本模块只读，不跑尺子、不写任何业务数据。

当前划分（15 题，每类留 1 题，共 5 题 = 33%）
-------------------------------------------
  类别：记忆 / 归因 / 工具 / 边界 / 上下文
  留出：mem-02, fail-02, tool-03, bnd-02, ctx-02
  可见：其余 10 题
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ⚠️ 唯一权威：改这里 = 改考卷划分，会改变历史分数口径。
#    选法：每个类别留 1 题，且留的是「同类里偏难、最能暴露泛化问题」的那题。
HOLDOUT_IDS = ("mem-02", "fail-02", "tool-03", "bnd-02", "ctx-02")

# 尺子题库全集（15 题）。用来识别「明细里冒出划分表外的题号」——
# 那说明题库换了/口径变了，划分必须跟着重定，不能默默算错。
ALL_IDS = ("mem-01", "mem-02", "mem-03",
           "fail-01", "fail-02", "fail-03",
           "tool-01", "tool-02", "tool-03", "tool-04",
           "bnd-01", "bnd-02", "bnd-03",
           "ctx-01", "ctx-02")

# 记录划分理由（给人看，不参与计算）
HOLDOUT_WHY = {
    "mem-02": "长期档案回忆：可见集里已有 mem-01/mem-03 覆盖同类能力",
    "fail-02": "失败归因：可见集里 fail-01/fail-03 已覆盖，留一题验泛化",
    "tool-03": "工具选择：工具类 4 题，留一题不看",
    "bnd-02": "边界处理：边界类 3 题，留一题不看",
    "ctx-02": "上下文保持：上下文类 2 题，留一题不看",
}


def holdout_ids():
    """留出集题号（排序后的 tuple，便于比对与指纹化）。"""
    return tuple(sorted(HOLDOUT_IDS))


def train_ids(all_ids=None):
    """可见集题号：给了全量就用全量减留出，否则只返回留出的补集没法算 —— 返回 None。"""
    if not all_ids:
        return None
    return tuple(sorted(str(x) for x in all_ids if str(x) not in HOLDOUT_IDS))


def is_holdout(case_id):
    return str(case_id or "") in HOLDOUT_IDS


def split_detail(detail):
    """把尺子的逐题明细切成 可见集 / 留出集 两组分数。

    detail 形如 {"mem-01": {"ok": true, "w": 1.0, "why": "", "s": 6.7}, ...}
    （本机 scores.jsonl 的 detail 字段就是这个结构）

    返回：
      ok         有没有可算的数据
      why        没数据时的原因
      train      {pass, total, pct, n}     可见集
      holdout    {pass, total, pct, n}     留出集
      gap        可见集 - 留出集 的百分点差（正数 = 留出集更差 = 疑似过拟合）
      overfit    是否触发过拟合判定（gap >= 20 且留出集有失分）
      unknown    明细里出现、但不在划分表里的题号（口径变了要能看出来）
    """
    out = {"ok": False, "why": "", "train": None, "holdout": None,
           "gap": None, "overfit": False, "unknown": []}
    if not isinstance(detail, dict) or not detail:
        out["why"] = "没有逐题明细（detail 为空）"
        return out

    def _blank():
        return {"pass": 0.0, "total": 0.0, "pct": None, "n": 0}

    tr, ho = _blank(), _blank()
    for cid, v in detail.items():
        if not isinstance(v, dict):
            continue
        try:
            w = float(v.get("w") if v.get("w") is not None else 1.0)
        except Exception:                                # noqa: BLE001
            w = 1.0
        bucket = ho if is_holdout(cid) else tr
        if str(cid) not in ALL_IDS:
            out["unknown"].append(str(cid))
        bucket["total"] += w
        bucket["n"] += 1
        if v.get("ok"):
            bucket["pass"] += w

    for b in (tr, ho):
        b["pct"] = round(b["pass"] / b["total"] * 100.0, 1) if b["total"] > 0 else None

    out["ok"] = bool(tr["n"] or ho["n"])
    out["train"], out["holdout"] = tr, ho
    if tr["pct"] is not None and ho["pct"] is not None:
        out["gap"] = round(tr["pct"] - ho["pct"], 1)
        # 阈值 20 个百分点：15 题里留出集 5 题，一题就是 20 —— 也就是说
        # 「可见集满分、留出集掉一题」就要亮灯，再宽就失去意义了。
        out["overfit"] = bool(out["gap"] >= 20 and ho["pass"] < ho["total"])
    return out


def visible_failures(failing):
    """从失败列表里只留可见集 —— 这是**唯一允许进提示词**的那部分。"""
    return [it for it in (failing or [])
            if isinstance(it, dict) and not is_holdout(it.get("id"))]


def hidden_failure_count(failing):
    """留出集里失分了几题（只给数量，不给题号，也不给原因）。"""
    return sum(1 for it in (failing or [])
               if isinstance(it, dict) and is_holdout(it.get("id")))


def leak_check(text):
    """检查一段文本里有没有泄露留出集题号（写提示词前 / 测试里都用它）。

    返回命中的题号列表；空列表 = 没泄露。
    """
    s = str(text or "")
    return [cid for cid in HOLDOUT_IDS if cid in s]


def score_split(detail=None):
    """算可见集 / 留出集分数（不跑尺子）。

    detail 显式传入时就用它 —— 保证和调用方（bench_brief）看的是**同一条**
    读数。否则两次读文件之间可能插进一次新跑分，导致「失分列表」和
    「留出集分数」对不上。
    """
    try:
        from . import iterloop
        if detail is None:
            last = iterloop.read_bench_last() or {}
            detail = last.get("detail") or {}
            iso = str(last.get("iso") or "")
        else:
            iso = ""
        sp = split_detail(detail)
        sp["iso"] = iso
        return sp
    except Exception as e:                               # noqa: BLE001
        logger.debug("读取留出集分数失败", exc_info=True)
        return {"ok": False, "why": "读不到尺子读数（%s）" % type(e).__name__,
                "train": None, "holdout": None, "gap": None,
                "overfit": False, "unknown": []}


def brief(sp=None):
    """一行中文摘要（给反思提示词 / 面板用）。留出集只给分数，不给题号。"""
    sp = sp or score_split()
    if not sp.get("ok"):
        return ""
    tr, ho = sp.get("train") or {}, sp.get("holdout") or {}
    if tr.get("pct") is None or ho.get("pct") is None:
        return ""
    txt = ("- 留出集（**不参与优化**，只用来验泛化）：%s/%s = %.1f%%；"
           "可见集 %s/%s = %.1f%%；差 %+.1f 个百分点"
           % (_f(ho.get("pass")), _f(ho.get("total")), float(ho.get("pct") or 0),
              _f(tr.get("pass")), _f(tr.get("total")), float(tr.get("pct") or 0),
              float(sp.get("gap") or 0)))
    if sp.get("overfit"):
        txt += ("\n  ⚠️ **疑似背题**：可见集明显高于留出集 —— 说明改动在迎合"
                "看得见的那几题，而不是真的变强。下一步要修的是**通用能力**"
                "（同一类问题的别的问法也得能过），不要再针对具体题目打补丁。")
    return txt


def _f(x):
    try:
        f = float(x)
        return str(int(f)) if abs(f - round(f)) < 1e-9 else ("%g" % f)
    except Exception:                                    # noqa: BLE001
        return "?"


def main():
    sp = score_split()
    print(brief(sp) or "读不到留出集分数：%s" % sp.get("why"))
    print("\n留出集题号：%s" % ", ".join(holdout_ids()))
    for cid in holdout_ids():
        print("  %s —— %s" % (cid, HOLDOUT_WHY.get(cid, "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
