# -*- coding: utf-8 -*-
"""LLM token 用量记账（按天落盘，进程内线程安全）。

为什么要有这个模块
------------------
用户 2026-11-17 要求「帮我节省 API 开支」。但在此之前，全项目**一处 token
统计都没有** —— 省了多少、质量有没有变，只能靠猜。这个模块只做两件事：

1. ``record()``：每次 LLM 调用把 usage 追加一行到
   ``data/llm_usage/YYYY-MM-DD.jsonl``；
2. ``summary()``：按天 / 按标签汇总，回答「钱到底花在哪了」。

为什么不改 ``chat_stream`` 的 yield 协议
---------------------------------------
它被对话 / 反思 / 打磨 / 迭代 / 自检十几处调用，多一种 kind 要逐个确认
调用方能容忍未知类型 —— 风险远大于收益。记账对调用方完全透明。

标签（tag）为什么放 thread_local
-------------------------------
每次 LLM 调用都在某一个线程里同步跑完，用线程局部变量存「当前阶段」，
就不必给十几个调用点加参数。``self_iterate.set_stage`` 会把阶段名写进来，
迭代入口写 ``iter``，对话入口写 ``chat``。

⚠️ 记账是**旁路**：任何异常都吞掉。记账失败绝不能影响主流程 ——
   磁盘满、权限不足、文件损坏，都不该让对话或迭代中断。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_TAG = threading.local()
_LOCK = threading.Lock()
_KEEP_DAYS = 60          # 只保留最近 60 天的账，避免无限增长


def _dir() -> Path:
    """账本目录（**确保存在**）。取不到可写目录时退回项目内 data/llm_usage。

    ⚠️ 2026-11-17 实测踩到：原来只返回路径、不建目录。而 ``config.data()``
    里的 makedirs 建的是**父目录**（data/），不是 llm_usage 本身 —— 于是
    ``open(path, "a")`` 抛 FileNotFoundError，被 record 的 except 静默吞掉，
    表现就是「真调了一次模型、正文正常返回，但账本增量 0」。
    这种静默失效最难查：功能看着在跑，数据永远是空的。
    """
    try:
        from . import config
        d = Path(config.data("data", "llm_usage"))
    except Exception:                                   # noqa: BLE001
        d = Path(__file__).resolve().parent.parent / "data" / "llm_usage"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("账本目录创建失败: %s", d, exc_info=True)
    return d


def _day() -> str:
    return time.strftime("%Y-%m-%d")


def set_tag(name):
    """设置本线程的记账标签（如 reflect / polish / iter / chat）。"""
    try:
        _TAG.name = str(name or "")[:24]
    except Exception:                                   # noqa: BLE001
        logger.debug("set_tag failed", exc_info=True)


def get_tag() -> str:
    try:
        return str(getattr(_TAG, "name", "") or "")
    except Exception:                                   # noqa: BLE001
        return ""


def record(prompt, completion, total, model="", tag=None):
    """记一次调用。参数全是数字或字符串，任何脏值都不该抛异常。"""
    try:
        p = int(prompt or 0)
        c = int(completion or 0)
        t = int(total or (p + c))
    except Exception:                                   # noqa: BLE001
        return
    if p <= 0 and c <= 0:
        return
    rec = {
        "ts": round(time.time(), 3),
        "at": time.strftime("%H:%M:%S"),
        "model": str(model or "")[:40],
        "tag": str(tag if tag is not None else get_tag())[:24],
        "p": max(0, p),
        "c": max(0, c),
        "t": max(0, t),
    }
    try:
        path = _dir() / ("%s.jsonl" % _day())
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("usage record failed", exc_info=True)


def _read_day(path: Path) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except Exception:                                   # noqa: BLE001
        logger.debug("usage read failed: %s", path)
    return out


def summary(days: int = 7) -> dict:
    """汇总最近 N 天用量。返回按天 + 按标签两份视图，永不抛异常。"""
    try:
        days = max(1, min(int(days or 7), _KEEP_DAYS))
    except Exception:                                   # noqa: BLE001
        days = 7
    root = _dir()
    by_day, by_tag = [], {}
    tot = {"p": 0, "c": 0, "t": 0, "calls": 0}
    today = _day()
    today_rec = {"p": 0, "c": 0, "t": 0, "calls": 0}
    try:
        names = sorted([p.name for p in root.glob("*.jsonl")])[-days:]
    except Exception:                                   # noqa: BLE001
        names = []
    for name in names:
        recs = _read_day(root / name)
        agg = {"p": 0, "c": 0, "t": 0, "calls": 0}
        for r in recs:
            p = int(r.get("p") or 0)
            c = int(r.get("c") or 0)
            t = int(r.get("t") or (p + c))
            tag = str(r.get("tag") or "未标注")
            agg["p"] += p
            agg["c"] += c
            agg["t"] += t
            agg["calls"] += 1
            slot = by_tag.setdefault(tag, {"p": 0, "c": 0, "t": 0, "calls": 0})
            slot["p"] += p
            slot["c"] += c
            slot["t"] += t
            slot["calls"] += 1
            if name == "%s.jsonl" % today:
                today_rec["p"] += p
                today_rec["c"] += c
                today_rec["t"] += t
                today_rec["calls"] += 1
        by_day.append({"date": name.replace(".jsonl", ""), **agg})
        for k in tot:
            tot[k] += agg[k]
    tags = [{"tag": k, **v} for k, v in by_tag.items()]
    tags.sort(key=lambda x: x["t"], reverse=True)
    return {"ok": True, "days": by_day, "by_tag": tags,
            "total": tot, "today": today_rec}


# --------------------------------------------------------------- 账单速览卡
# 单价（元 / 百万 token）。DeepSeek-chat 公开价：输入 ¥2（缓存未命中）/ 输出 ¥8。
# 推理 token 按输出价计（厂商计费口径一致），所以 reason 和 out 同价。
_PRICE = {"in": 2.0, "out": 8.0}


def _blank() -> dict:
    """一格空的聚合桶（按天 / 按标签 / 按时段都用它，字段名保持一致）。"""
    return {"calls": 0, "p": 0, "c": 0, "t": 0, "reason": 0, "cost": 0.0}


def _cost(p, c, reason) -> float:
    """按价格加权后的花销（元）—— TEI 的做法：三类 token 不按个数相加。

    为什么不能直接拿 token 数当"省了多少"
    ------------------------------------
    输入 token 单价只有输出的 1/4（¥2 vs ¥8），一次"长输入短输出"的调用
    和一次"短输入长输出"的调用 token 数可能一样，钱却差 4 倍。用户问
    「能省多少」时想比的是**钱**，所以这里给一个价格加权的复合指标。
    """
    try:
        return (max(0, int(p)) * _PRICE["in"]
                + (max(0, int(c)) + max(0, int(reason))) * _PRICE["out"]) / 1000000.0
    except Exception:                                   # noqa: BLE001
        return 0.0


def _delta(now, prev) -> float:
    """相对变化 (now - prev) / prev，四舍五入到 6 位；prev 为 0 时退化成 0/1。"""
    try:
        now = float(now or 0)
        prev = float(prev or 0)
    except Exception:                                   # noqa: BLE001
        return 0.0
    if prev <= 0:
        return 0.0 if now <= 0 else 1.0
    return round((now - prev) / prev, 6)


def _hour_of(rec) -> int:
    """这条记录落在几点（0~23）。取不到返回 -1，调用方直接跳过，不塞进假数据。"""
    try:
        ts = float(rec.get("ts") or 0)
        if ts > 0:
            return int(time.localtime(ts).tm_hour)
    except Exception:                                   # noqa: BLE001
        logger.debug("usage hour from ts failed", exc_info=True)
    at = str(rec.get("at") or "")
    if len(at) >= 2 and at[:2].isdigit():
        h = int(at[:2])
        if 0 <= h <= 23:
            return h
    return -1


def _human_tokens(n) -> str:
    """token 数 → 一眼能读的量级（10.36M / 12.5k / 940）。"""
    try:
        n = int(n or 0)
    except Exception:                                   # noqa: BLE001
        return "0"
    if n >= 1000000:
        return "%.2fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def summarize(days: int = 7) -> dict:
    """账单速览卡要的那一份账：一句话结论 + 折叠明细，**纯本地计算**。

    和 ``summary()`` 的分工
    ----------------------
    ``summary()`` 给内部人看：按天一份、按标签一份，字段是 p / c / t 这种缩写。
    ``summarize()`` 给用户看：字段直接可读（total_calls / total_tokens /
    by_tag / by_hour / delta_vs_prev），再多给一个 ``weighted_cost`` ——
    参考 Token 效率指数（TEI），把输入 / 输出 / 推理三类 token **按价格加权**
    成一个能横向对比的复合指标。

    ``delta_vs_prev`` 的口径
    -----------------------
    调用次数的相对变化 ``(最新一天 - 前一日) / 前一日``：0.195 就是「较昨日 +19.5%」。
    用次数而不是 token，是因为次数不受单轮长短干扰 —— 涨了就是用户用得更勤。
    另外还给了 ``delta_tokens_vs_prev`` / ``delta_cost_vs_prev`` 备用。

    ``by_tag`` / ``by_hour`` 的值是**调用次数**（和结论行的 total_calls 对得上：
    ``sum(by_hour.values()) == total_calls``）；要 token / 花销的细节看
    ``by_tag_detail`` / ``by_hour_detail``。

    账本可能不存在、为空、半行损坏 —— 一律按 0 处理，本函数永不抛异常。
    """
    try:
        days = max(1, min(int(days or 7), _KEEP_DAYS))
    except Exception:                                   # noqa: BLE001
        days = 7
    root = _dir()
    try:
        names = sorted([p.name for p in root.glob("*.jsonl")])[-days:]
    except Exception:                                   # noqa: BLE001
        names = []

    total = _blank()
    by_tag, by_hour, by_day = {}, {}, []
    for name in names:
        day = _blank()
        per_tag, per_hour = {}, {}
        for r in _read_day(root / name):
            try:
                p = int(r.get("p") or 0)
                c = int(r.get("c") or 0)
                t = int(r.get("t") or (p + c))
            except Exception:                           # noqa: BLE001
                continue
            if p <= 0 and c <= 0:
                continue
            reason = max(0, t - p - c)      # 推理 token：总量里扣掉进出两份的余量
            cost = _cost(p, c, reason)
            tag = str(r.get("tag") or "未标注")
            for slot in (day, total,
                         per_tag.setdefault(tag, _blank()),
                         by_tag.setdefault(tag, _blank())):
                slot["calls"] += 1
                slot["p"] += p
                slot["c"] += c
                slot["t"] += t
                slot["reason"] += reason
                slot["cost"] += cost
            h = _hour_of(r)
            if 0 <= h <= 23:
                for slot in (per_hour.setdefault(h, _blank()),
                             by_hour.setdefault(h, _blank())):
                    slot["calls"] += 1
                    slot["p"] += p
                    slot["c"] += c
                    slot["t"] += t
                    slot["reason"] += reason
                    slot["cost"] += cost
        by_day.append({"date": name.replace(".jsonl", ""), **day,
                       "by_tag": per_tag, "by_hour": per_hour})

    # 结论行比的是「最新一天」和「它的前一日」
    today = by_day[-1] if by_day else {"date": "", **_blank()}
    prev = by_day[-2] if len(by_day) >= 2 else None
    prev_calls = int((prev or {}).get("calls") or 0)
    delta = _delta(today["calls"], prev_calls)

    def _detail(bucket, key_name):
        rows = []
        for k, v in bucket.items():
            rows.append({key_name: k, **v})
        rows.sort(key=lambda x: (-x["cost"], -x["calls"]))
        return rows

    headline = "今日 %d 次调用 / %s token / 较昨日 %+.0f%%" % (
        int(today["calls"]), _human_tokens(today["t"]), delta * 100)

    return {
        "ok": True,
        "days": days,
        # —— 结论行要的三个数（不含日期口径，跨天也能直接显示）
        "total_calls": int(total["calls"]),
        "total_tokens": int(total["t"]),
        "total_prompt_tokens": int(total["p"]),
        "total_completion_tokens": int(total["c"]),
        "total_reasoning_tokens": int(total["reason"]),
        # 价格加权的复合指标（元）：比裸 token 数更能回答「省了多少」
        "weighted_cost": round(total["cost"], 6),
        # —— 折叠明细：值是调用次数，明细表在 *_detail 里
        "by_tag": {k: int(v["calls"]) for k, v in by_tag.items()},
        "by_tag_detail": _detail(by_tag, "tag"),
        "by_hour": {h: int(v["calls"]) for h, v in sorted(by_hour.items())},
        "by_hour_detail": _detail(by_hour, "hour"),
        "by_day": by_day,
        # —— 对比口径
        "today": {"date": today.get("date", ""), "calls": int(today["calls"]),
                  "tokens": int(today["t"]), "cost": round(today["cost"], 6)},
        "prev": {"date": (prev or {}).get("date", ""), "calls": prev_calls,
                 "tokens": int((prev or {}).get("t") or 0),
                 "cost": round((prev or {}).get("cost") or 0.0, 6)},
        "delta_vs_prev": delta,
        "delta_calls_vs_prev": delta,
        "delta_tokens_vs_prev": _delta(today["t"], (prev or {}).get("t") or 0),
        "delta_cost_vs_prev": _delta(today["cost"], (prev or {}).get("cost") or 0.0),
        "headline": headline,
    }


def prune(keep_days: int = _KEEP_DAYS):
    """删掉过期账本（默认保留 60 天）。失败无副作用。"""
    try:
        root = _dir()
        files = sorted(root.glob("*.jsonl"))
        for p in files[:-max(1, int(keep_days))]:
            try:
                os.remove(p)
            except OSError:                             # noqa: BLE001 单个过期账本删不掉不影响其余清理
                pass
    except Exception:                                   # noqa: BLE001
        logger.debug("prune failed", exc_info=True)
