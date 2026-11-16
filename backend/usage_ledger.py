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


def prune(keep_days: int = _KEEP_DAYS):
    """删掉过期账本（默认保留 60 天）。失败无副作用。"""
    try:
        root = _dir()
        files = sorted(root.glob("*.jsonl"))
        for p in files[:-max(1, int(keep_days))]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:                                   # noqa: BLE001
        logger.debug("prune failed", exc_info=True)
