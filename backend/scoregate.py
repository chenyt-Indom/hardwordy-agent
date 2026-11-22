# -*- coding: utf-8 -*-
"""改动后自动评分闸门（2026-11-22 用户要求）。

用户原话：
    「以后每次有修改代码的任务完成之前都得自动进行评分然后比对，
      确保每次迭代评分都比上次高」

为什么单独一个模块，而不是塞进 self_iterate：
    评分闸门要在**工具层**生效（写入成功 → 打脏标记；重启前 → 强制评分），
    而 self_iterate.py 有 9000 行、改一次风险太大。这里只做三件事：
    打标记、评分比对、给结论。

流程：
    ① 四个写入工具（write / patch / insert_after / apply）成功后
       调 mark_dirty(rel) —— 只写一个小 json，**不做评分**，零开销；
    ② 任务要「完成」（= 调 selfdev_restart 让新代码生效）时
       调 gate() —— 全量评分 → 与上一次分数比对 → 写进累计分账本；
    ③ 比上次**低** → 闸门拦住，不让你重启，并把「哪一块掉的、可疑代码在哪」
       原样回给你，要求先修好或回滚；持平或更高 → 放行。

⚠️ 逃生口（刻意留的）：
    用户在界面上点「立即重启」走的是 /api/selfdev/apply_restart，
    **不经过**这道闸门 —— 那是人的决定，工具层不该替用户拦死。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCK = threading.RLock()

# 全量评分要几十秒，同一时刻只允许一个在跑
_RUNNING = False


def _data_dir() -> str:
    """与累计分账本同一个目录（config.data("data")/iterloop）。

    ⚠️ 必须走 config，不能硬编码项目根 —— 设了 MM_DATA_DIR（Docker / 便携包
    换目录）时写回源码目录会失败，闸门状态就永远存不下来。
    """
    d = ""
    try:
        from . import config as _cfg
        d = os.path.join(_cfg.data("data"), "iterloop")
    except Exception:                                   # noqa: BLE001
        logger.debug("scoregate 取 config.data 失败，退回项目根", exc_info=True)
    if not d:
        d = os.path.join(_ROOT, "data", "iterloop")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


def _path() -> str:
    return os.path.join(_data_dir(), "scoregate.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load() -> Dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            d.setdefault("dirty", False)
            d.setdefault("files", [])
            d.setdefault("last_score", None)
            d.setdefault("last_gate", None)
            d.setdefault("gates", [])
            return d
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"dirty": False, "files": [], "last_score": None,
            "last_gate": None, "gates": []}


def _save(d: Dict[str, Any]) -> None:
    try:
        p = _path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


# ---------------------------------------------------------------- 打脏标记

def mark_dirty(rel: str = "", note: str = "") -> None:
    """任何代码写入成功后调它。只打标记，**不评分**（零开销）。"""
    try:
        with _LOCK:
            d = _load()
            d["dirty"] = True
            d["dirty_iso"] = _now()
            d["dirty_ts"] = time.time()
            fs = list(d.get("files") or [])
            r = str(rel or "").replace("\\", "/").lstrip("./")
            if r and r not in fs:
                fs.append(r)
            d["files"] = fs[-40:]
            if note:
                d["note"] = str(note)[:200]
            _save(d)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def pending() -> Dict[str, Any]:
    """本轮有没有还没评分的改动。"""
    d = _load()
    return {"dirty": bool(d.get("dirty")),
            "files": list(d.get("files") or []),
            "since": d.get("dirty_iso") or ""}


# ---------------------------------------------------------------- 比对逻辑

def _prev_score(d: Dict[str, Any]):
    """上一次的**完整**评分（分项都在，才能算清是哪一块掉的）。"""
    s = d.get("last_score")
    if isinstance(s, dict) and s.get("total") is not None:
        return s
    try:
        from . import iterloop as _il
        led = _il.load_ledger()
        base = (led.get("base") or {}).get("total")
        if base is not None:
            return {"total": base, "static": None, "regress": None, "func": None,
                    "note": "上一次分数来自账本基准分（没有分项快照）"}
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return None


def _msg(rec: Dict[str, Any]) -> str:
    """给模型看的结论。拦不拦都返回一段可读文本。"""
    pv, tv, d = rec.get("prev"), rec.get("total"), rec.get("delta")
    if d is None:
        head = ("评分闸门：这次 %s 分，上一次拿不到分数，无法比对（不拦）。"
                % tv)
    else:
        head = ("评分闸门：上一次 %s → 这次 %s（%+.2f，%s）"
                % (pv, tv, d, {"improved": "进步", "regressed": "退步",
                               "flat": "持平"}.get(rec.get("verdict"), "未知")))
    lines = [head]
    per = rec.get("per_part") or {}
    bits = []
    for k, zh in (("static", "静态"), ("regress", "回归"), ("func", "功能")):
        s = per.get(k)
        if s:
            bits.append("%s %s" % (zh, s))
    if bits:
        lines.append("分项：" + "；".join(bits))
    sus = rec.get("suspects") or []
    if sus:
        lines.append("可疑代码：")
        for s in sus[:5]:
            if isinstance(s, dict):
                lines.append("  · %s %s（%s）%s"
                             % (s.get("file") or "?", s.get("line") or "?",
                                s.get("code") or s.get("kind") or "?",
                                "★本轮改过" if s.get("in_changed") else ""))
            else:
                lines.append("  · %s" % s)
    if rec.get("blocked"):
        lines.append("")
        lines.append("⛔ 这次比上次低，**先别重启**。要么把掉分的地方修好，"
                     "要么回滚这次改动，然后重新调 selfdev_restart 再评一次。")
        lines.append("（如果用户明确要求「就这样重启」，请告诉用户自己点界面上的"
                     "「立即重启」—— 那条路不经过这道闸门，是人的决定。）")
    else:
        lines.append("✅ 评分不低于上次，可以收尾。")
    return "\n".join(lines)


def gate(force: bool = False, note: str = "",
         block_on_drop: bool = True) -> Dict[str, Any]:
    """跑一次评分并和上次比对。

    force=True      本轮没改动也评（用于基线）
    block_on_drop   比上次低时是否拦住（重启闸门用 True；纯记录用 False）
    """
    global _RUNNING
    d = _load()
    if not d.get("dirty") and not force:
        return {"ran": False, "ok": True, "blocked": False, "verdict": "skip",
                "msg": "本轮没有代码改动，跳过评分（没有可比的东西）"}
    if _RUNNING:
        return {"ran": False, "ok": True, "blocked": False, "verdict": "busy",
                "msg": "上一次评分还在跑，本次跳过（不重复评）"}
    _RUNNING = True
    try:
        from . import iterloop as _il
        prev = _prev_score(d)
        t0 = time.time()
        now = _il.score(full=True)
        cost = round(time.time() - t0, 1)
        total = now.get("total")
        pv = (prev or {}).get("total")
        if pv is None or total is None:
            delta = None
            verdict = "unknown"
        else:
            delta = round(float(total) - float(pv), 2)
            verdict = ("improved" if delta > 0
                       else ("regressed" if delta < 0 else "flat"))
        # 写进累计分账本（累加 + 归因 + history 一条）
        res = {}
        try:
            res = _il.commit_round(before=prev, after=now,
                                   note=(note or "自动评分闸门")[:200]) or {}
        except Exception:                               # noqa: BLE001
            logger.debug("scoregate commit_round 失败", exc_info=True)
        jd = res if isinstance(res, dict) else {}
        ex = jd.get("explain") or {}
        rec = {"iso": _now(), "ts": time.time(), "prev": pv, "total": total,
               "delta": delta, "verdict": verdict, "cost_s": cost,
               "blocked": False,
               "files": list(d.get("files") or []),
               "note": str(note or "")[:200],
               "per_part": (jd.get("per_part") or {}),
               "suspects": (ex.get("suspects") or [])[:8],
               "summary": str(ex.get("summary") or "")[:400]}
        # 分项进退：优先用账本算的，没有就自己算
        if not rec.get("per_part") and prev:
            pp = {}
            for k in ("static", "regress", "func"):
                a = ((prev or {}).get(k) or {}).get("score")
                b = (now or {}).get(k) or {}
                if a is not None and b.get("score") is not None:
                    pp[k] = round(float(b["score"]) - float(a), 2)
            rec["per_part"] = pp
        blocked = bool(block_on_drop and verdict == "regressed")
        rec["blocked"] = blocked
        d["dirty"] = False
        d["files"] = []
        d["last_score"] = now
        d["last_gate"] = rec
        gs = list(d.get("gates") or [])
        gs.append({"iso": rec["iso"], "total": total, "delta": delta,
                   "verdict": verdict, "blocked": blocked})
        d["gates"] = gs[-50:]
        _save(d)
        return {"ran": True, "ok": not blocked, "blocked": blocked,
                "prev": pv, "total": total, "delta": delta,
                "verdict": verdict, "rec": rec, "msg": _msg(rec),
                "cum": jd.get("cum")}
    except Exception as e:                              # noqa: BLE001
        logger.debug("scoregate 评分失败", exc_info=True)
        return {"ran": False, "ok": True, "blocked": False, "verdict": "error",
                "msg": "评分闸门自己出错，不拦（%s: %s）" % (type(e).__name__, e)}
    finally:
        _RUNNING = False


# ---------------------------------------------------------------- 只读状态

def status() -> Dict[str, Any]:
    """给体检 / 面板看的只读快照。"""
    d = _load()
    lg = d.get("last_gate") or {}
    return {"dirty": bool(d.get("dirty")),
            "files": list(d.get("files") or []),
            "since": d.get("dirty_iso") or "",
            "last": {"iso": lg.get("iso"), "prev": lg.get("prev"),
                     "total": lg.get("total"), "delta": lg.get("delta"),
                     "verdict": lg.get("verdict"),
                     "blocked": lg.get("blocked")},
            "gates": len(d.get("gates") or [])}
