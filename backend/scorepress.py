# -*- coding: utf-8 -*-
"""压分报告（2026-11-22 用户要求）。

用户原话：
    「评分机制能够汇报压低评分的部分是哪些部分然后详细记录然后上报，
      反思机制需要重点参考然后构建改进计划然后正常推送」

与相邻模块的分工（四个模块各管一段，别互相塞）
--------------------------------------------
    iterloop.score()   只管「算出一个分」= 质量基线(0~100，有上限) +
                       能力跑分(无上限) 两段相加，明细散在 static.findings 里；
                       **压分点只属于质量段**（能力段只会往上加，不存在失分）；
    scoregate.gate()   只管「比上次高还是低、拦不拦」；
    scoredrop          只管「**变化**」：这一轮我改坏了什么、掉了多少、起自动修复；
    scorepress（本模块）只管「**存量**」：当前这一版到底哪些地方在压分、
                       每条压掉多少、修哪条最划算。

为什么「存量」必须单独一档（真实理由，不是凑数）
----------------------------------------------
scoredrop 记的是 delta —— 它回答「这一轮我改坏了什么」。
但用户这次问的是「压低评分的部分是哪些」——那是**存量**问题：
即使连续几轮都持平（比如一直 86.32），仍然有 13.68 分被扣着，
不知道这 13.68 分分别扣在哪一行，反思就只能瞎猜。
所以本模块**每次评分都出一份报告**（不管进步、持平还是退步），
并按「能挽回多少分」排序，直接告诉反思：先改哪一条最划算。

落盘
----
    data/self_iterate/press.jsonl   每次评分一条，保留最近 60 条
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# press.jsonl 最多保留多少条（每条约 2~6 KB，60 条足够看趋势）
_KEEP = 60

# 报告里最多列多少条压分点（再多模型也读不完，反而稀释重点）
_TOP_N = 15

# 与 iterloop.static_part 的扣分口径必须完全一致，否则「能挽回多少分」是错的
_LEVEL_COST = {"high": 8.0, "mid": 3.0, "info": 0.5}

# 针对真实 kind 的修法提示（kind 取自 code_audit 的实际输出，不是编的）
_FIX_HINTS = {
    "silent-except": "`except ...: pass` 要么改成具体异常类型并处理，"
                     "要么确实要吞就标 `# noqa: BLE001`",
    "subprocess-global-patched": "已由 procwin 全局兜住（实测窗口数 0），"
                                 "属于已知无害项，可忽略",
    "bare-except": "把裸 `except:` 改成具体异常类型，或标 `# noqa: BLE001`",
    "dead-code": "删掉没有被调用的函数/变量，或补上调用点",
    "unused-import": "删掉没用到的 import",
    "undefined-symbol": "补上定义，或把名字改对",
    "dup-def": "同名函数只留一个",
    "route-missing": "前端调的路由后端不存在：补路由，或改前端调用",
    "long-func": "拆成几个小函数，单函数别超 80 行",
    "todo": "补完或删掉 TODO",
    "test_failed": "跑一下这条用例看真实报错，改到通过为止",
    "syntax_error": "编译不过 → 先修语法，否则整个包都跑不起来",
    "symbol_missing": "backend.main 缺关键符号 → 补上定义或改对名字",
    "smoke_fail": "接口冒烟不通 → 看该路由的真实异常",
}


def _hint(kind: str) -> str:
    """按 kind 给一句能直接照做的修法。"""
    k = str(kind or "").strip()
    if k in _FIX_HINTS:
        return _FIX_HINTS[k]
    kl = k.lower().replace("_", "-")
    for key, val in _FIX_HINTS.items():
        if key in kl or kl in key:
            return val
    return "看这一行的具体报错，按提示修"


# ---------------------------------------------------------------- 路径

def _data_dir() -> str:
    """与累计分账本、归因表、低分日志同一个目录。"""
    d = ""
    try:
        from . import config as _cfg
        d = os.path.join(_cfg.data("data"), "self_iterate")
    except Exception:                                   # noqa: BLE001
        logger.debug("scorepress 取 config.data 失败，退回项目根", exc_info=True)
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "self_iterate")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


def _path() -> str:
    return os.path.join(_data_dir(), "press.jsonl")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 核心：算压分

def build(sc: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    """从 `iterloop.score()` 的完整结果里算出「谁在压低分数」。

    返回的每一分项都带 `lost`（这一项失掉多少分）和 `items`（逐条压分点，
    含 文件:行 + 种类 + 能挽回多少分 + 修法）。
    """
    sc = sc if isinstance(sc, dict) else {}
    total = sc.get("total")
    parts: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []

    # ---- 静态 40：按 high/mid/info 逐条算扣分
    st = sc.get("static") or {}
    if isinstance(st, dict) and st:
        smax = float(st.get("max") or 40.0)
        ssc = float(st.get("score") or 0.0)
        fit: List[Dict[str, Any]] = []
        for f in (st.get("findings") or []):
            if not isinstance(f, dict):
                continue
            lv = str(f.get("level") or "").lower()
            kind = str(f.get("kind") or "")
            fit.append({
                "part": "static",
                "file": str(f.get("file") or ""),
                "line": int(f.get("line") or 0),
                "kind": kind,
                "level": lv,
                "msg": str(f.get("msg") or "")[:180],
                "cost": _LEVEL_COST.get(lv, 0.5),
                "fix": _hint(kind),
            })
        items.extend(fit)
        parts.append({
            "key": "static", "label": "静态", "score": round(ssc, 2),
            "max": round(smax, 2), "lost": round(max(0.0, smax - ssc), 2),
            "count": int(st.get("finding_total") or len(fit)),
            "shown": len(fit), "items": fit,
        })

    # ---- 回归 30：每个用例值多少分，失败一条就是丢那么多
    rg = sc.get("regress") or {}
    if isinstance(rg, dict) and rg:
        rmax = float(rg.get("max") or 30.0)
        rsc = float(rg.get("score") or 0.0)
        passed = int(rg.get("passed") or 0)
        failed = int(rg.get("failed") or 0)
        per = round(rmax / float(max(1, passed + failed)), 3)
        rit: List[Dict[str, Any]] = []
        for name in (rg.get("failed_items") or []):
            rit.append({
                "part": "regress", "file": "<回归用例>", "line": 0,
                "kind": "test_failed", "level": "mid",
                "msg": str(name)[:180], "cost": per,
                "fix": _hint("test_failed"),
            })
        items.extend(rit)
        parts.append({
            "key": "regress", "label": "回归", "score": round(rsc, 2),
            "max": round(rmax, 2), "lost": round(max(0.0, rmax - rsc), 2),
            "count": failed, "shown": len(rit), "items": rit,
            "passed": passed, "failed": failed,
        })

    # ---- 功能 30 = 语法 10 + 符号 10 + 冒烟 10
    fn = sc.get("func") or {}
    if isinstance(fn, dict) and fn:
        fmax = float(fn.get("max") or 30.0)
        fsc = float(fn.get("score") or 0.0)
        nit: List[Dict[str, Any]] = []
        if fn.get("syntax_ok") is False:
            bad = fn.get("syntax_bad") or ["<语法编译失败>"]
            for b in bad:
                s = str(b)
                nit.append({
                    "part": "func", "file": s.split(":")[0], "line": 0,
                    "kind": "syntax_error", "level": "high", "msg": s[:180],
                    "cost": round(10.0 / float(max(1, len(bad))), 3),
                    "fix": _hint("syntax_error"),
                })
        if fn.get("symbols_ok") is False:
            bad = fn.get("symbols_bad") or ["<关键符号缺失>"]
            for b in bad:
                nit.append({
                    "part": "func", "file": "<backend.main>", "line": 0,
                    "kind": "symbol_missing", "level": "high",
                    "msg": str(b)[:180],
                    "cost": round(10.0 / float(max(1, len(bad))), 3),
                    "fix": _hint("symbol_missing"),
                })
        smoke = fn.get("smoke") or []
        sbad = [s for s in smoke if isinstance(s, dict) and not s.get("ok")]
        if sbad:
            per_s = round(10.0 * len(sbad) / float(max(1, len(smoke))), 3)
            for s in sbad:
                nit.append({
                    "part": "func", "file": "<接口>", "line": 0,
                    "kind": "smoke_fail", "level": "mid",
                    "msg": str(s.get("path") or "")[:180],
                    "cost": per_s, "fix": _hint("smoke_fail"),
                })
        items.extend(nit)
        parts.append({
            "key": "func", "label": "功能", "score": round(fsc, 2),
            "max": round(fmax, 2), "lost": round(max(0.0, fmax - fsc), 2),
            "count": len(nit), "shown": len(nit), "items": nit,
        })

    # ---- 排序：按能挽回多少分从大到小（反思要的就是这个顺序）
    items.sort(key=lambda x: (-float(x.get("cost") or 0.0),
                              str(x.get("file") or ""),
                              int(x.get("line") or 0)))
    top = items[:_TOP_N]
    lost = round(sum(float(p.get("lost") or 0.0) for p in parts), 2)
    # 理论可挽回：不能超过实际失分（扣到 0 封顶时两者会不等）
    recoverable = round(min(lost, sum(float(x.get("cost") or 0.0) for x in top)), 2)
    # ★ 2026-11-22 总分改「无上限」后，报告必须把两段分开说清：
    #   quality 质量基线（0~100，才有「被压掉多少分」）
    #   ability 能力跑分（无上限，只会往上加，不存在「失分」）
    quality = sc.get("quality")
    ab = sc.get("ability") if isinstance(sc.get("ability"), dict) else {}
    return {
        "iso": _now(), "ts": time.time(),
        "total": total, "quality": quality, "ability": ab,
        "lost": lost,
        "recoverable": recoverable,
        "parts": parts, "top": top,
        "items_total": len(items),
        "note": str(note or "")[:200],
    }


# ---------------------------------------------------------------- 落盘

def _load_all() -> List[Dict[str, Any]]:
    p = _path()
    out: List[Dict[str, Any]] = []
    try:
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:                       # noqa: BLE001
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except FileNotFoundError:
        return []
    except Exception:                                   # noqa: BLE001
        logger.debug("读 press.jsonl 失败", exc_info=True)
        return []
    return out


def record(sc: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    """算一份压分报告并落盘。**每次评分都该调**（不管进步/持平/退步）。"""
    rep = build(sc, note=note)
    try:
        with _LOCK:
            rows = _load_all()
            rows.append(rep)
            rows = rows[-_KEEP:]
            with open(_path(), "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("写 press.jsonl 失败", exc_info=True)
    return rep


def latest() -> Dict[str, Any]:
    """最近一份压分报告（没有就返回空 dict）。"""
    rows = _load_all()
    return rows[-1] if rows else {}


def history(n: int = 20) -> List[Dict[str, Any]]:
    """最近 n 份报告的精简版（只留分数与失分，用于看趋势）。"""
    rows = _load_all()[-max(1, int(n or 20)):]
    return [{"iso": r.get("iso"), "total": r.get("total"),
             "lost": r.get("lost"), "recoverable": r.get("recoverable"),
             "items": r.get("items_total")} for r in rows]


def stats() -> Dict[str, Any]:
    """给面板 / 体检看的汇总。"""
    rep = latest()
    rows = _load_all()
    trend = []
    for r in rows[-6:]:
        trend.append({"iso": r.get("iso"), "total": r.get("total"),
                      "lost": r.get("lost")})
    ab = rep.get("ability") if isinstance(rep.get("ability"), dict) else {}
    return {
        "has": bool(rep),
        "iso": rep.get("iso", ""),
        "total": rep.get("total"),
        "quality": rep.get("quality"),
        "ability": ab.get("score"),
        "ability_rows": ab.get("rows") or [],
        "unbounded": True,
        "lost": rep.get("lost"),
        "recoverable": rep.get("recoverable"),
        "parts": [{k: p.get(k) for k in ("key", "label", "score", "max", "lost", "count")}
                  for p in (rep.get("parts") or [])],
        "top": (rep.get("top") or [])[:8],
        "items_total": rep.get("items_total"),
        "reports": len(rows),
        "trend": trend,
        "path": _path(),
    }


# ---------------------------------------------------------------- 给反思用

def _relevance(it: Dict[str, Any], q: str) -> int:
    """query 关键词命中数（用于把「和这次任务相关」的压分点往前排）。"""
    if not q:
        return 0
    blob = ("%s %s %s" % (it.get("file") or "", it.get("kind") or "",
                          it.get("msg") or "")).lower()
    n = 0
    for w in str(q).lower().replace("\n", " ").split():
        w = w.strip("，,。.：:；;、()（）[]【】")
        if len(w) >= 2 and w in blob:
            n += 1
    return n


def brief(n: int = 3, query: str = "") -> str:
    """压分清单摘要 —— 供反思提示词「重点参考」。

    格式刻意做成「先结论、再排序、最后硬要求」：
    反思模型看到的第一行就是「共失多少分」，避免它忽略这一段。
    """
    rep = latest()
    if not rep:
        return "（还没有压分报告 —— 下一次评分会自动生成）"
    total = rep.get("total")
    lost = rep.get("lost")
    q = rep.get("quality")
    ab = rep.get("ability") if isinstance(rep.get("ability"), dict) else {}
    lines = ["## 📊 当前压分清单（每次评分都出，本轮必须重点参考）"]
    lines.append("当前跑分 %s（= 质量基线 %s/100 + 能力跑分 %s，**总分无上限**），"
                 "质量段共被压掉 %s 分；报告时间 %s。"
                 % (total, q, ab.get("score"), lost, rep.get("iso") or "?"))
    for r in (ab.get("rows") or []):
        if isinstance(r, dict) and int(r.get("count") or 0):
            lines.append("  · 能力加分：%s %s 个 × %s = +%s"
                         % (r.get("label"), r.get("count"), r.get("unit"),
                            r.get("score")))
    for p in (rep.get("parts") or []):
        lines.append("  · %s %s / %s（失 %s 分，%s 处）"
                     % (p.get("label"), p.get("score"), p.get("max"),
                        p.get("lost"), p.get("count")))
    top = list(rep.get("top") or [])
    if query:
        top.sort(key=lambda x: (-_relevance(x, query),
                                -float(x.get("cost") or 0.0)))
    top = top[:max(1, int(n or 3))]
    if top:
        lines.append("按「修了能挽回多少分」排序：")
        for i, it in enumerate(top, 1):
            loc = str(it.get("file") or "?")
            if int(it.get("line") or 0):
                loc += ":%d" % int(it.get("line"))
            lines.append("  %d. %s [%s/%s] %s → 可挽回 %s 分｜修法：%s"
                         % (i, loc, it.get("kind"), it.get("level"),
                            str(it.get("msg") or "")[:100],
                            it.get("cost"), it.get("fix")))
        lines.append("")
        lines.append("★ 硬要求：本轮计划里**必须**至少有一条动作直接针对上面第 1 条"
                     "（最大压分点）；如果这一条不适合改（例如是刻意设计、"
                     "或改它风险大于收益），必须在动作里写清「为什么不改」。"
                     "只提新功能、不动压分点的计划视为不合格。")
    else:
        lines.append("★ 质量段当前没有任何压分点（满分）—— 计划应聚焦新功能与健壮性；"
                     "注意总分无上限，新增**被验证的能力**（测试用例、只读端点）会继续涨分。")
    hist = history(5)
    if len(hist) >= 2:
        seq = " → ".join("%s" % h.get("total") for h in hist)
        lines.append("最近 %d 次评分趋势：%s" % (len(hist), seq))
    return "\n".join(lines)
