"""自迭代体系「详情面板」聚合层 —— 只读，一个接口喂满前端那张卡。

用户 2026-11-22 要求：「前端能不能增加一个按钮让我查看这些东西详细内容，
包括日志评分以及迭代报告，还有反思机制我也要能够直观看到」。

设计原则（和 iterhealth 一致）：
· **只读**：不触发评分、不跑回归、不写任何文件，前端点开就能秒出；
· **分块容错**：8 个块各自 try，某块炸了只让那一块显示错误，
  绝不让整张面板空白 —— 否则用户会以为整个体系挂了；
· **不编数**：拿不到的字段返回 None / 空列表，前端显示「暂无」，
  不补默认值。空 ≠ 坏，前端要能区分（配合 iterhealth 的 idle 语义）。
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 日志/报告读多少行：够看清最近发生了什么，又不至于把响应撑到几百 KB
_LOG_LINES = 40
_JOURNAL_LINES = 14
_PLANS_SHOWN = 6
_HIST_SHOWN = 10


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _data_dir() -> str:
    """跟 iterloop / failattr 走同一个根（config.data），不硬编码源码目录。"""
    try:
        from . import config as _cfg
        return _cfg.data("data")
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 取 config.data 失败", exc_info=True)
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data")


def _tail(path: str, n: int, max_chars: int = 240) -> list:
    """读文本文件最后 n 个非空行；文件不存在返回空列表（不报错）。"""
    try:
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        out = []
        for ln in lines[-n:]:
            out.append(ln[:max_chars])
        return out
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 %s 失败", path, exc_info=True)
        return []


def _tail_jsonl(path: str, n: int) -> list:
    """读 JSONL 尾部，逐行解析；坏行跳过（不因为一行坏就让整块空掉）。"""
    out = []
    for ln in _tail(path, n, max_chars=20000):
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:                               # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------------ 各块
def _blk_score() -> dict:
    """评分：累计分 + 最近几轮进退 + score.log 原文尾部。"""
    from . import iterloop as _IL
    st = _IL.stats()
    log_path = ""
    try:
        log_path = _IL._score_log_path()
    except Exception:                                   # noqa: BLE001
        log_path = os.path.join(_data_dir(), "iterloop", "score.log")
    # ★ 2026-11-22：账本只记「进退净值」，不记「当前代码绝对值」，所以用户看到
    #   「累计 +0.00」会以为没跑过分。这里补上「上次手动量的当前版本分数」。
    manual = {}
    try:
        mp = os.path.join(_data_dir(), "iterloop", "last_score.json")
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as f:
                manual = json.load(f) or {}
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 last_score.json 失败（面板照常显示其他块）",
                     exc_info=True)
    return {
        "last_manual": manual,
        "cumulative": st.get("cumulative"),
        "rounds": st.get("rounds"),
        "improved": st.get("improved"),
        "regressed": st.get("regressed"),
        "flat": st.get("flat"),
        "rollbacks": st.get("rollbacks"),
        "avg_delta": st.get("avg_delta"),
        "best": st.get("best"),
        "worst": st.get("worst"),
        "last": st.get("last"),
        "history": _IL.history(_HIST_SHOWN),
        "log_path": log_path,
        "log": _tail(log_path, _LOG_LINES),
    }


def _blk_failattr(days: int) -> dict:
    """失败归因：统计 + 最近几条明细 + 已构思的改进方法。"""
    from . import failattr as _FA
    st = _FA.stats(days)
    rows = []
    try:
        rows = [r for r in (_FA.load() or [])][-6:]
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读归因明细失败", exc_info=True)
    recent = []
    for r in rows:
        recent.append({
            "ts": str(r.get("iso") or r.get("ts_zh") or r.get("ts") or "")[:19],
            "kind": str(r.get("kind_zh") or r.get("kind") or ""),
            "why": str(r.get("why_failed") or "")[:600],
            "rollback": str(r.get("why_rollback") or "")[:400],
            "files": (r.get("files") or [])[:5],
            "vague": bool(r.get("too_vague")),
        })
    lessons = []
    try:
        d = _FA.load_lessons() or {}
        for k, v in list(d.items())[:8]:
            if isinstance(v, dict):
                lessons.append({
                    "kind": str(v.get("kind") or k),
                    "root_cause": str(v.get("root_cause") or "")[:400],
                    "fix": str(v.get("fix") or "")[:400],
                    "verify": str(v.get("verify") or "")[:300],
                })
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 lessons 失败", exc_info=True)
    return {
        "total": st.get("total"),
        "days": st.get("days"),
        "by_kind_zh": st.get("by_kind_zh"),
        "by_stage": st.get("by_stage"),
        "repeat": st.get("repeat"),
        "rollback": st.get("rollback"),
        "recent": recent,
        "lessons": lessons,
    }


def _blk_reflect() -> dict:
    """反思与迭代报告：journal 尾部 + 最近几个计划（含动作数与完成状态）。"""
    out = {"journal": [], "plans": [], "journal_path": ""}
    try:
        jp = os.path.join(_data_dir(), "selfdev_journal.jsonl")
        out["journal_path"] = jp
        for r in _tail_jsonl(jp, _JOURNAL_LINES):
            out["journal"].append({
                "ts": str(r.get("ts") or "")[:19],
                "rel": str(r.get("rel") or "")[:40],
                "note": str(r.get("note") or "")[:220],
            })
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 journal 失败", exc_info=True)
    try:
        from . import self_iterate as _si
        plans = (_si.load_plans() or {}).get("items") or []
        for p in plans[-_PLANS_SHOWN:][::-1]:
            acts = p.get("actions") or []
            done = [a for a in acts if str(a.get("status") or "") in
                    ("done", "approved", "executed")]
            out["plans"].append({
                "id": str(p.get("id") or "")[:24],
                "ts": str(p.get("ts") or "")[:19],
                "trigger": str(p.get("trigger") or ""),
                "summary": str(p.get("summary") or "")[:300],
                "actions": len(acts),
                "done": len(done),
                "features": len(p.get("features") or []),
                "fail_lessons": len(p.get("fail_lessons") or []),
            })
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 plans 失败", exc_info=True)
    return out


def _blk_memory() -> dict:
    """记忆层：长期记忆体量 + 跨模型传承档案。"""
    out = {"long_chars": 0, "long_items": 0, "preview": "",
           "transfer": {}, "transfer_brief": ""}
    try:
        from . import memory as _mem
        s = str(_mem.get_long() or "")
        out["long_chars"] = len(s)
        out["long_items"] = len([x for x in s.split("；") if x.strip()])
        out["preview"] = s[:400]
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读长期记忆失败", exc_info=True)
    try:
        from . import memtransfer as _MT
        out["transfer"] = _MT.stats()
        # brief() 内部会先 sync()，等于顺带确认「读写链路都通」
        out["transfer_brief"] = (_MT.brief(max_chars=600) or "")[:600]
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读传承档案失败", exc_info=True)
    return out


def _blk_semantic() -> dict:
    from . import failvec as _FV
    return _FV.status()


def _blk_health(days: int) -> dict:
    from . import iterhealth as _IH
    d = _IH.check(days=days) or {}
    cnt = {"ok": 0, "degrade": 0, "idle": 0}
    for it in (d.get("items") or []):
        k = str(it.get("state") or "")
        if k in cnt:
            cnt[k] += 1
    return {"summary": cnt, "items": d.get("items") or [],
            "verdict": d.get("verdict") or ""}


def _blk_chain() -> dict:
    """协同检查：8 个模块是否都能 import（接线断了这里会立刻红）。"""
    mods = ["iterloop", "failattr", "failvec", "memtransfer", "iterhealth",
            "memory", "self_iterate", "regress"]
    ok, bad = [], []
    for m in mods:
        try:
            __import__("backend." + m)
            ok.append(m)
        except Exception as e:                          # noqa: BLE001
            bad.append("%s: %s" % (m, e))
    return {"ok": ok, "bad": bad, "total": len(mods), "alive": len(ok)}


# ------------------------------------------------------------------ 出口
def _blk_scoredrop(days: int = 30) -> dict:
    """低分历史 + 教训 + 自动修复进度（2026-11-22 用户要求）。

    用户要「点开能看到每次的扣分代码行」—— 这里把 hot（本轮改过的文件里
    的扣分点）原样带出 file/line/kind/msg，前端做折叠展开。
    """
    from . import scoredrop as _SD
    return _SD.status(n=20)


def panel(days: int = 30) -> dict:
    """一次性返回面板需要的全部数据。任何一块失败都不影响其它块。"""
    days = max(1, int(days or 30))
    out = {"ok": True, "ts": _now(), "days": days, "errors": {}}
    blocks = [
        ("score", lambda: _blk_score()),
        ("failattr", lambda: _blk_failattr(days)),
        ("scoredrop", lambda: _blk_scoredrop(days)),
        ("reflect", lambda: _blk_reflect()),
        ("memory", lambda: _blk_memory()),
        ("semantic", lambda: _blk_semantic()),
        ("health", lambda: _blk_health(days)),
        ("chain", lambda: _blk_chain()),
    ]
    for name, fn in blocks:
        try:
            out[name] = fn()
        except Exception as e:                          # noqa: BLE001
            logger.debug("iterpanel 块 %s 失败", name, exc_info=True)
            out[name] = None
            out["errors"][name] = "%s: %s" % (type(e).__name__, e)
    out["ok"] = not out["errors"]
    return out
