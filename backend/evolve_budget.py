# -*- coding: utf-8 -*-
"""进化预算（evolve budget）—— 给自动迭代装上「油表和油箱盖」。

为什么必须有它（专家 2026-11-24 指出的 L3 三缺之三）
----------------------------------------------------
  「没有预算和自动循环（进化靠用户叫一声才动，不是自己在跑）」

本机其实**已经有一半**：`selfloop` 的 `daily_plans=12` 管住了「一天生成几个计划」。
但它只管一个维度，而且散落在各处：

  · 计划数      → selfloop._quota_left（有）
  · 改动文件数  → 只在单轮里限 3 个（_over_cap），**一天总共改多少没人管**
  · 自动重启    → 完全没上限（理论上可以「改→重启→再改→再重启」整晚）
  · 花了多少    → 分散在 plans.json / actlog.jsonl / rsi_history.jsonl，没有一处能回答
                  「今天这套自动进化到底烧了多少」

于是出现两个极端：要么用户不敢开（怕失控），要么开了之后半夜把自己重启死。
这个模块把三类资源收进**一本账**，并且是「先问、再扣、扣不动就停」：

    status()          今天还剩多少（给人看 / 给面板看）
    gate(kind, n)     能不能再花 n 个（不能就返回中文原因）
    spend(kind, n)    真花了，记账

设计约束
--------
1. **手动永远豁免**：用户自己点「立即反思」不该被配额挡住（沿用 selfloop 的口径）。
2. **跨天自动重置**：按本地日期，不是 24 小时滚动窗 —— 否则「昨天 23:50 跑过」
   会让今天上午一直开不了工。
3. **坏文件不炸**：账本读坏了就当今天还没花过，并留一条 note（宁可从宽，
   也不能因为一个 json 坏了就让整套自动化瘫痪）。
4. **只写自己的文件** data/evolve_budget.json，不碰业务数据。
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(_ROOT, "data", "evolve_budget.json")

# 每日上限（默认值；可在账本的 limits 字段里覆盖，方便用户按机器调）
#   plans    一天最多生成几个改进计划（与 selfloop.daily_plans 同口径）
#   files    一天最多改几个文件（按「每轮 3 个 × 3 轮」估）
#   restarts 一天最多自动重启几次（重启会打断用户，必须最紧）
DEFAULT_LIMITS = {"plans": 12, "files": 9, "restarts": 6}

KINDS = ("plans", "files", "restarts")

_LABEL = {"plans": "改进计划", "files": "改动文件", "restarts": "自动重启"}


def _today():
    return time.strftime("%Y-%m-%d")


def _blank():
    return {"date": _today(), "limits": dict(DEFAULT_LIMITS),
            "spent": {k: 0 for k in KINDS}, "history": [], "note": ""}


def load():
    """读账本；不存在/读坏都返回一份干净的空账（并写明原因）。"""
    d = _blank()
    try:
        with open(PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            d.update({k: v for k, v in raw.items()
                      if k in ("date", "limits", "spent", "history", "note")})
    except FileNotFoundError:
        pass
    except Exception as e:                               # noqa: BLE001
        d["note"] = "账本读不出来（%s），按「今天还没花过」处理" % type(e).__name__
        logger.warning("evolve_budget 读取失败：%s", e)

    # 补齐结构（老版本 / 手工改坏时也能用）
    lim = d.get("limits") if isinstance(d.get("limits"), dict) else {}
    d["limits"] = {k: _int(lim.get(k), DEFAULT_LIMITS[k]) for k in KINDS}
    sp = d.get("spent") if isinstance(d.get("spent"), dict) else {}
    d["spent"] = {k: max(0, _int(sp.get(k), 0)) for k in KINDS}
    if not isinstance(d.get("history"), list):
        d["history"] = []

    # 跨天重置：只清「花了多少」，limits 保留（用户调过的上限不该被重置掉）
    if str(d.get("date") or "") != _today():
        d["date"] = _today()
        d["spent"] = {k: 0 for k in KINDS}
    return d


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def save(d):
    d = d or _blank()
    d["date"] = str(d.get("date") or _today())
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        try:
            from . import safeio
            safeio.atomic_write_json(PATH, d, indent=1)
        except Exception:                                # noqa: BLE001
            with open(PATH, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)
    except Exception as e:                               # noqa: BLE001
        logger.warning("evolve_budget 写盘失败：%s", e)
    return d


def status():
    """今天的三类预算还剩多少（只读）。"""
    d = load()
    left = {k: max(0, d["limits"][k] - d["spent"][k]) for k in KINDS}
    return {"date": d["date"], "limits": d["limits"], "spent": d["spent"],
            "left": left, "note": d.get("note") or "",
            "exhausted": [k for k in KINDS if left[k] <= 0]}


def left(kind):
    return int(status()["left"].get(kind, 0))


def gate(kind, n=1, manual=False):
    """能不能再花 n 个。manual=True（用户手动点）永远放行。

    返回 {"ok": bool, "why": str, "left": int, "kind": str}
    """
    kind = str(kind or "")
    if kind not in KINDS:
        return {"ok": False, "why": "未知预算类型：%s" % kind, "left": 0, "kind": kind}
    n = max(1, _int(n, 1))
    st = status()
    lf = int(st["left"].get(kind, 0))
    if manual:
        return {"ok": True, "why": "", "left": lf, "kind": kind, "manual": True}
    if lf < n:
        return {"ok": False, "kind": kind, "left": lf,
                "why": ("今日「%s」预算不够了（还剩 %d，本次要 %d；上限 %d/天）。"
                        "明天 0 点自动重置；确实要现在做，请手动点一次。"
                        % (_LABEL.get(kind, kind), lf, n, int(st["limits"][kind])))}
    return {"ok": True, "why": "", "left": lf, "kind": kind}


def spend(kind, n=1, why="", manual=False):
    """记一笔消耗。manual=True 照记，但不受上限限制。"""
    kind = str(kind or "")
    if kind not in KINDS:
        return status()
    n = max(0, _int(n, 0))
    if n == 0:
        return status()
    d = load()
    d["spent"][kind] = d["spent"].get(kind, 0) + n
    d["history"].append({"ts": round(time.time(), 1), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "kind": kind, "n": n, "why": str(why or "")[:160],
                         "manual": bool(manual)})
    d["history"] = d["history"][-200:]
    save(d)
    return status()


def set_limits(**kw):
    """调上限（前端/命令行用）。只认已知键，负数当 0。"""
    d = load()
    for k, v in (kw or {}).items():
        if k in KINDS:
            d["limits"][k] = max(0, _int(v, d["limits"][k]))
    save(d)
    return status()


def brief():
    """一行中文摘要（面板 / 日志用）。"""
    st = status()
    parts = []
    for k in KINDS:
        parts.append("%s %d/%d" % (_LABEL[k], st["spent"][k], st["limits"][k]))
    txt = "今日进化预算（%s）：%s" % (st["date"], " · ".join(parts))
    if st["exhausted"]:
        txt += "\n⚠️ 已用完：%s —— 自动迭代会停在这里，等明天重置或你手动点。" % (
            "、".join(_LABEL[k] for k in st["exhausted"]))
    return txt


def main():
    print(brief())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
