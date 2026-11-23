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

try:                                    # 包内导入（正常路径）
    from .jsonfix import safe_get as _get, safe_list as _get_list
except ImportError:                     # 允许作为顶层模块被直接导入（如验收断言）
    from jsonfix import safe_get as _get, safe_list as _get_list

logger = logging.getLogger(__name__)

# ★ 2026-11-22 用户要求：「safe_get 兜底取字段，避免 KeyError」。
#   本模块是**只读聚合层**，数据全来自七八个模块的 stats()/load()/brief()。
#   上游少给一个键 → 原来的 st.get(...) 在 None 上直接 AttributeError，
#   整块显示错误（用户以为体系挂了，其实只是某个字段没给）。
#   现在统一走 jsonfix.safe_get：取不到返回默认值、并把「实际可用的 key 列表」
#   记进 actlog 的 field_miss 事件，排查时不用再猜上游给了什么。
#   下面的 where="块名.字段" 就是给 field_miss 事件用的调用点标注。

# 日志/报告读多少行：够看清最近发生了什么，又不至于把响应撑到几百 KB
_LOG_LINES = 40
_JOURNAL_LINES = 14
_PLANS_SHOWN = 6
_HIST_SHOWN = 10
# 历史声称回扫看多少条（2026-11-24）：和 actlog 的 COUNT_WINDOW 同口径，
# 单会话账本只有 120 条，300 条必须跨会话拼（actlog._recent_session_rows 负责）。
_CLAIM_WINDOW = 300


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
    if not isinstance(manual, dict):
        manual = {}
    return {
        "last_manual": manual,
        "cumulative": _get(st, "cumulative", where="score.cumulative"),
        "rounds": _get(st, "rounds", where="score.rounds"),
        "improved": _get(st, "improved", where="score.improved"),
        "regressed": _get(st, "regressed", where="score.regressed"),
        "flat": _get(st, "flat", where="score.flat"),
        "rollbacks": _get(st, "rollbacks", where="score.rollbacks"),
        "avg_delta": _get(st, "avg_delta", where="score.avg_delta"),
        "best": _get(st, "best", where="score.best"),
        "worst": _get(st, "worst", where="score.worst"),
        "last": _get(st, "last", where="score.last"),
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
        loaded = _FA.load()
        # 只留 dict 行：明细文件坏一半时，混进来的字符串/None 不该让整块塌掉
        rows = [r for r in (loaded if isinstance(loaded, list) else [])
                if isinstance(r, dict)][-6:]
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读归因明细失败", exc_info=True)
    recent = []
    for r in rows:
        w = "failattr.recent"
        recent.append({
            "ts": str(_get(r, "iso", where=w) or _get(r, "ts_zh", where=w)
                      or _get(r, "ts", where=w) or "")[:19],
            "kind": str(_get(r, "kind_zh", where=w) or _get(r, "kind", where=w) or ""),
            "why": str(_get(r, "why_failed", where=w) or "")[:600],
            "rollback": str(_get(r, "why_rollback", where=w) or "")[:400],
            # safe_list 一次到位：取不到/类型不对都给 []，且只记一次 field_miss
            "files": _get_list(r, "files", where=w)[:5],
            "vague": bool(_get(r, "too_vague", where=w)),
        })
    lessons = []
    try:
        d = _FA.load_lessons() or {}
        if not isinstance(d, dict):
            d = {}
        for k, v in list(d.items())[:8]:
            if isinstance(v, dict):
                lessons.append({
                    "kind": str(_get(v, "kind") or k),
                    "root_cause": str(_get(v, "root_cause") or "")[:400],
                    "fix": str(_get(v, "fix") or "")[:400],
                    "verify": str(_get(v, "verify") or "")[:300],
                })
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 lessons 失败", exc_info=True)
    return {
        "total": _get(st, "total", where="failattr.total"),
        "days": _get(st, "days", where="failattr.days"),
        "by_kind_zh": _get(st, "by_kind_zh", where="failattr.by_kind_zh"),
        "by_stage": _get(st, "by_stage", where="failattr.by_stage"),
        "repeat": _get(st, "repeat", where="failattr.repeat"),
        "rollback": _get(st, "rollback", where="failattr.rollback"),
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
                "ts": str(_get(r, "ts", where="reflect.journal") or "")[:19],
                "rel": str(_get(r, "rel", where="reflect.journal") or "")[:40],
                "note": str(_get(r, "note", where="reflect.journal") or "")[:220],
            })
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读 journal 失败", exc_info=True)
    try:
        from . import self_iterate as _si
        plans = _get_list(_si.load_plans(), "items", where="reflect.plans")
        for p in plans[-_PLANS_SHOWN:][::-1]:
            acts = _get_list(p, "actions", where="reflect.plan.actions")
            done = [a for a in acts if str(_get(a, "status") or "") in
                    ("done", "approved", "executed")]
            out["plans"].append({
                "id": str(_get(p, "id") or "")[:24],
                "ts": str(_get(p, "ts") or "")[:19],
                "trigger": str(_get(p, "trigger") or ""),
                "summary": str(_get(p, "summary") or "")[:300],
                "actions": len(acts),
                "done": len(done),
                "features": len(_get(p, "features") or []),
                "fail_lessons": len(_get(p, "fail_lessons") or []),
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
        _tr = _MT.stats()
        out["transfer"] = _tr if isinstance(_tr, dict) else {}
        # brief() 内部会先 sync()，等于顺带确认「读写链路都通」
        out["transfer_brief"] = (_MT.brief(max_chars=600) or "")[:600]
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 读传承档案失败", exc_info=True)
    return out


def _blk_semantic() -> dict:
    from . import failvec as _FV
    st = _FV.status()
    # 语义检索状态整体缺失时给空 dict（前端显示「暂无」），而不是把 None 丢下去
    return st if isinstance(st, dict) else {}


def _blk_history_claims() -> dict:
    """历史「新建/新增 <path>」声称回扫（2026-11-24 用户要求）。

    账本 detail 里写着「新建 xxx.py」不代表文件真的存在 —— 下一轮拿旧记录
    当证据就会自己骗自己。这里调 actlog.audit_history_claims() 只读核对磁盘，
    把「不一致条数 + 前几条明细」暴露给体检卡，前端显示「历史记录不一致：N 条」。

    只读磁盘、不触发评分/回归；任何异常都退化成空结果（计数 0），
    绝不让这一块把整张面板拖成空白。
    """
    empty = {"count": 0, "mismatch": 0, "claims": 0, "scanned": 0,
             "window": _CLAIM_WINDOW, "items": []}
    try:
        from . import actlog as _AL
        d = _AL.audit_history_claims(window=_CLAIM_WINDOW)
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 历史声称回扫失败", exc_info=True)
        return empty
    if not isinstance(d, dict):
        return empty
    w = "history_claims"
    try:
        cnt = int(_get(d, "count", 0, where=w + ".count") or 0)
    except (TypeError, ValueError):
        cnt = 0
    try:
        claims = int(_get(d, "claims", 0, where=w + ".claims") or 0)
    except (TypeError, ValueError):
        claims = 0
    try:
        scanned = int(_get(d, "scanned", 0, where=w + ".scanned") or 0)
    except (TypeError, ValueError):
        scanned = 0
    items = []
    for it in _get_list(d, "items", where=w + ".items")[:10]:
        items.append({
            "path": str(_get(it, "path", where=w + ".item.path") or "")[:200],
            "t": str(_get(it, "t", where=w + ".item.t") or "")[:19],
        })
    return {"count": cnt, "mismatch": cnt, "claims": claims,
            "scanned": scanned, "window": _CLAIM_WINDOW, "items": items}


def _blk_health(days: int) -> dict:
    from . import iterhealth as _IH
    d = _IH.check(days=days) or {}
    cnt = {"ok": 0, "degrade": 0, "idle": 0}
    items = _get_list(d, "items", where="health.items")
    for it in items:
        k = str(_get(it, "state") or "")
        if k in cnt:
            cnt[k] += 1
    # ★ 2026-11-24：体检块顺带带上「历史记录不一致」计数 —— main.py 的
    #   /api/iter/panel 会把整个 health 块原样透出，前端据此显示告警行。
    try:
        claims = _blk_history_claims()
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 体检取历史声称失败", exc_info=True)
        claims = {"count": 0, "mismatch": 0, "claims": 0, "scanned": 0,
                  "window": _CLAIM_WINDOW, "items": []}
    return {"summary": cnt, "items": items,
            "verdict": _get(d, "verdict", "", where="health.verdict") or "",
            "history_claims": claims}


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
    st = _SD.status(n=20)
    return st if isinstance(st, dict) else {}


def _blk_press() -> dict:
    """当前压分报告（2026-11-22 用户要求）。

    「评分机制能够汇报压低评分的部分是哪些部分然后详细记录然后上报」——
    这一块就是「上报」：分项失分 + 逐条压分点（file:line:kind:msg + 可挽回分）
    + 最近几次趋势。只读，不触发评分。
    """
    from . import scorepress as _SP
    st = _SP.stats()
    return st if isinstance(st, dict) else {}


def panel(days: int = 30) -> dict:
    """一次性返回面板需要的全部数据。任何一块失败都不影响其它块。"""
    days = max(1, int(days or 30))
    out = {"ok": True, "ts": _now(), "days": days, "errors": {}}
    blocks = [
        ("score", lambda: _blk_score()),
        ("press", lambda: _blk_press()),
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
            v = fn()
            # 块返回的不是 dict（上游改了返回类型 / 给了 None）→ 按「暂无」处理，
            # 绝不让前端拿到字符串或列表去当对象读（那是 TypeError 不是空面板）。
            out[name] = v if isinstance(v, dict) else None
            if out[name] is None:
                out["errors"][name] = ("块 %s 返回的不是 dict（%s）"
                                      % (name, type(v).__name__))
        except Exception as e:                          # noqa: BLE001
            logger.debug("iterpanel 块 %s 失败", name, exc_info=True)
            out[name] = None
            out["errors"][name] = "%s: %s" % (type(e).__name__, e)
    out["ok"] = not out["errors"]
    # ★ 2026-11-24：把「历史记录不一致」提到顶层，调用方（体检卡/看板）
    #   不必知道它藏在 health 块里；health 块里那份原样保留，两处同源不重扫。
    try:
        hc = out.get("health")
        if isinstance(hc, dict) and isinstance(hc.get("history_claims"), dict):
            out["history_claims"] = hc["history_claims"]
        else:
            out["history_claims"] = {"count": 0, "mismatch": 0, "claims": 0,
                                     "scanned": 0, "window": _CLAIM_WINDOW,
                                     "items": []}
    except Exception:                                   # noqa: BLE001
        logger.debug("iterpanel 汇总历史声称失败", exc_info=True)
        out["history_claims"] = {"count": 0, "mismatch": 0, "claims": 0,
                                 "scanned": 0, "window": _CLAIM_WINDOW,
                                 "items": []}
    # 面板出口处再兜一层：顶层字段用 safe_get 取，缺了也只降级成默认值
    out["ok"] = bool(_get(out, "ok", True, where="panel.ok"))
    out["ts"] = _get(out, "ts", "", where="panel.ts") or ""
    out["days"] = _get(out, "days", days, where="panel.days") or days
    out["errors"] = _get(out, "errors", {}, where="panel.errors") or {}
    return out
