# -*- coding: utf-8 -*-
"""低分归因、教训沉淀、自动修复（2026-11-22 用户要求）。

用户原话：
    「如果每次迭代之后发现评分比迭代前低，日志能不能自动记录低的原因，
      重新改进能不能触发？反思时以及重新改进时有没有结合这个日志并引以为戒
      重新思考如何进行改进，导致低分的原因能不能被精准锁定并进行改进？」
    「自动直接改代码修好再重评，反复到分数涨上去」
    「两边都记：归因表管反思，独立文件管长期历史」
    「最近 20 次」
    「只要比上次低就算（-0.01 也拦）」
    「沉淀成长期教训，用向量检索自动找出需要用到的类似的教训」

职责边界（刻意拆出来，不塞进 scoregate / iterloop）
--------------------------------------------------
scoregate 只管「评分 + 比对 + 拦不拦」；
本模块管「记下来 → 沉淀成教训 → 语义检索 → 自动修」。

三个落盘文件（都在 data/self_iterate/ 下）
    score_drops.jsonl   每次退步一条，含扣分代码行、分项进退、修复结果
    drop_lessons.jsonl  从低分沉淀出的长期教训（按扣分特征去重、累积次数）
    dropvec.json        教训的向量缓存（增量，不重算）

为什么要独立文件而不是复用 failattr
    用户明确要「两边都记」。failattr 是按「动作失败」组织的（patch 没命中、
    语法错…），而低分是「代码质量退步」——同一个 kind 可能在两边含义不同，
    混在一张表里会让反思的聚类统计失真。所以：failattr 记失败动作，
    本模块记低分，两边都写、各自检索。

自动修复为什么是异步
    反思一次 200 秒上下（要抓技术动态 + 调模型 + 执行改动），
    而评分闸门是在 selfdev_restart 里同步调的（工具 60 秒上限）。
    同步跑必然超时，所以闸门只负责「记下来 + 起一个后台线程 + 立刻返回」，
    修复进度写进状态文件，前端和下一次反思都能看到。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 自动修复最多几轮（每轮 = 反思 + 执行 + 重评，约 4 分钟）
_MAX_ROUNDS = 3

# 语义检索混合权重：与 failvec 保持一致（向量为主、字面兜底）
_W_VEC = 0.72
_W_COV = 0.28
_MIN_SCORE = 0.34

# 一次最多比较多少条教训
_SCAN_MAX = 400

# 后台修复线程是否在跑
_REPAIRING = False


# ---------------------------------------------------------------- 路径

def _dir() -> str:
    """与累计分账本、归因表同一个目录。"""
    d = ""
    try:
        from . import config as _cfg
        d = os.path.join(_cfg.data("data"), "self_iterate")
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop 取 config.data 失败", exc_info=True)
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "self_iterate")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


def _drops_path() -> str:
    return os.path.join(_dir(), "score_drops.jsonl")


def _lessons_path() -> str:
    return os.path.join(_dir(), "drop_lessons.jsonl")


def _vec_path() -> str:
    return os.path.join(_dir(), "dropvec.json")


def _state_path() -> str:
    return os.path.join(_dir(), "drop_repair.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_jsonl(path: str) -> List[dict]:
    out: List[dict] = []
    try:
        if not os.path.isfile(path):
            return out
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:                       # noqa: BLE001
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop 读 %s 失败", path, exc_info=True)
    return out


def _append_jsonl(path: str, rec: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop 写 %s 失败", path, exc_info=True)
        return False


# ---------------------------------------------------------------- 记录低分

# 扣分类型的「怎么修」建议。查不到的类型走通用话术，
# 绝不硬编一堆猜的规则（宁可说「看具体 msg」，也不要给错方向）。
_FIX_HINT = {
    "bare_except": "别用裸 except，改成具体异常类型；确实要吞就补 `# noqa: BLE001`",
    "dead_code": "删掉没有引用的函数/变量，或者确认真的要用就接上调用点",
    "undefined_name": "函数名/变量名写错或没 import —— 先确认真实接口名再写",
    "unused_import": "删掉没用到的 import",
    "syntax_error": "语法错，先跑一遍 py_compile 定位",
    "duplicate_def": "同名函数定义了两次，删掉旧的那个",
    "long_line": "拆行",
    "todo": "把 TODO 落成实现或删掉",
}

_KIND_ZH = {
    "bare_except": "裸 except 吞异常",
    "dead_code": "死代码",
    "undefined_name": "名字不存在",
    "unused_import": "多余 import",
    "syntax_error": "语法错误",
    "duplicate_def": "函数重复定义",
    "long_line": "超长行",
    "todo": "遗留 TODO",
}


def _clean_suspect(s: Any) -> Dict[str, Any]:
    """把扣分点规整成可长期存的形状。

    要兼容两种来源（这是 2026-11-22 实测踩出来的）：
      · scoregate 的 `codes` —— 逐条明细 {file,line,kind,level,msg,in_changed}
      · scoregate 的 `suspects` —— **按文件归并** {file,count,lines[],kinds[],levels[]}
        这种没有单条 line/kind/msg，早期只读它会让低分日志全变成
        「file:0 空类型」，等于定位不到行 —— 用户要的「精准锁定」就落空了。
    """
    if not isinstance(s, dict):
        return {"file": "", "line": 0, "kind": "", "msg": str(s)[:200]}
    line = s.get("line")
    if line is None:
        ls = s.get("lines")
        if isinstance(ls, list) and ls:
            line = ls[0]
    kind = s.get("kind")
    if not kind:
        ks = s.get("kinds")
        if isinstance(ks, list) and ks:
            kind = ks[0]
    msg = s.get("msg")
    if not msg:
        ks = s.get("kinds")
        if isinstance(ks, list) and ks:
            msg = "、".join(str(k) for k in ks[:4])
    lv = s.get("level")
    if not lv:
        lvs = s.get("levels")
        if isinstance(lvs, list) and lvs:
            lv = lvs[0]
    try:
        line_n = int(line or 0)
    except (TypeError, ValueError):
        line_n = 0
    return {
        "file": str(s.get("file") or "").replace("\\", "/").lstrip("./")[:160],
        "line": line_n,
        "kind": str(kind or "")[:60],
        "level": str(lv or "")[:20],
        "msg": str(msg or "")[:300],
        "in_changed": bool(s.get("in_changed")),
    }


def record(rec: Dict[str, Any], plan_id: str = "") -> Dict[str, Any]:
    """记一条低分。rec 就是 scoregate 里那条完整记录。

    只在 **delta < 0** 时被调用（用户口径：只要比上次低就算，-0.01 也记）。
    """
    try:
        rec = rec or {}
        delta = rec.get("delta")
        try:
            delta = float(delta) if delta is not None else None
        except (TypeError, ValueError):
            delta = None
        if delta is None or delta >= 0:
            return {"ok": True, "saved": False, "why": "不是退步，不记"}

        # 优先逐条明细（codes），退回归并版（suspects）
        raw = rec.get("codes") or rec.get("suspects") or []
        sus = [_clean_suspect(s) for s in raw]
        # 扣分点优先用「本轮改过的文件」，那才是能改的地方
        hot = [s for s in sus if s.get("in_changed")] or sus
        row = {
            "iso": str(rec.get("iso") or _now()),
            "ts": float(rec.get("ts") or time.time()),
            "prev": rec.get("prev"),
            "total": rec.get("total"),
            "delta": round(delta, 2),
            "per_part": rec.get("per_part") or {},
            "files": list(rec.get("files") or [])[:40],
            "suspects": sus[:20],
            "hot": hot[:10],
            "summary": str(rec.get("summary") or "")[:400],
            "note": str(rec.get("note") or "")[:200],
            "plan_id": str(plan_id or "")[:80],
            "repair": {"state": "pending"},
        }
        with _LOCK:
            ok = _append_jsonl(_drops_path(), row)
        if not ok:
            return {"ok": False, "error": "写 score_drops.jsonl 失败"}
        # 沉淀成长期教训
        lesson = _learn_from(row)
        # 同步写一份到失败归因表（用户要「两边都记」）
        _mirror_to_failattr(row)
        return {"ok": True, "saved": True, "lesson": lesson, "row": row}
    except Exception as e:                              # noqa: BLE001
        logger.debug("scoredrop.record 失败", exc_info=True)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _mirror_to_failattr(row: Dict[str, Any]) -> None:
    """低分也写一条进归因表，让 failattr.brief / failvec 天然能检索到。"""
    try:
        from . import failattr as _FA
        hot = row.get("hot") or []
        kinds = sorted({str(s.get("kind")) for s in hot if s.get("kind")})
        files = []
        for s in hot[:6]:
            f = str(s.get("file") or "").replace("\\", "/").lstrip("./")
            if f and f not in files:
                files.append(f)
        spots = ", ".join("%s:%s" % (s.get("file") or "?", s.get("line") or "?")
                          for s in hot[:5])
        _FA.record(
            kind="score_regress",
            stage="score",
            title="评分退步 %+.2f 分" % float(row.get("delta") or 0),
            why_failed=("评分从 %s 掉到 %s（%+.2f 分）。新增扣分类型：%s。"
                        "扣分位置：%s"
                        % (row.get("prev"), row.get("total"),
                           float(row.get("delta") or 0),
                           "、".join(kinds) or "未分类", spots or "未定位")),
            files=files,
            plan_id=str(row.get("plan_id") or ""),
            lesson="改完先看静态分项：新增扣分点优先修，别急着加新功能",
            context="本轮改动后评分下降；涉及文件：%s"
                    % (", ".join(row.get("files") or []) or "未知"),
        )
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop 镜像到 failattr 失败", exc_info=True)


# ---------------------------------------------------------------- 教训沉淀

def _lesson_key(row: Dict[str, Any]) -> str:
    """教训去重键：扣分类型组合 + 主要文件。

    同一类扣分在同一个文件反复出现 → 次数累加，而不是刷一堆一样的教训。
    """
    hot = row.get("hot") or []
    kinds = sorted({str(s.get("kind") or "other") for s in hot})
    f = ""
    if hot:
        f = os.path.basename(str(hot[0].get("file") or ""))
    return ("%s|%s" % ("+".join(kinds) or "other", f))[:120]


def _learn_from(row: Dict[str, Any]) -> Dict[str, Any]:
    """把一条低分沉淀成长期教训（按 key 累积次数，保留最新的修法）。"""
    try:
        hot = row.get("hot") or []
        kinds = sorted({str(s.get("kind") or "other") for s in hot})
        key = _lesson_key(row)
        spots = []
        for s in hot[:5]:
            spots.append("%s:%s" % (s.get("file") or "?", s.get("line") or "?"))
        fixes = []
        for k in kinds:
            h = _FIX_HINT.get(k)
            if h:
                fixes.append("%s → %s" % (_KIND_ZH.get(k, k), h))
        if not fixes:
            fixes.append("看扣分点各自的 msg，按提示逐条修")
        lesson = {
            "key": key,
            "kinds": kinds,
            "kinds_zh": [_KIND_ZH.get(k, k) for k in kinds],
            "count": 1,
            "root_cause": "改动引入了 %d 个新的静态问题（%s），位置：%s"
                          % (len(hot), "、".join(_KIND_ZH.get(k, k) for k in kinds)
                             or "未分类", "、".join(spots) or "未定位"),
            "fix": "；".join(fixes)[:800],
            "verify": "改完重新调 selfdev_restart，闸门会自动重评并比对",
            "delta": row.get("delta"),
            "last_iso": row.get("iso") or _now(),
            "ts": time.time(),
        }
        with _LOCK:
            p = _lessons_path()
            old: Dict[str, dict] = {}
            for r in _read_jsonl(p):
                k = str(r.get("key") or "")
                if k:
                    old[k] = r
            prev = old.get(key) or {}
            if prev:
                lesson["count"] = int(prev.get("count") or 0) + 1
                lesson["first_iso"] = prev.get("first_iso") or prev.get("last_iso")
            else:
                lesson["first_iso"] = lesson["last_iso"]
            old[key] = lesson
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in old.values():
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
        return lesson
    except Exception as e:                              # noqa: BLE001
        logger.debug("scoredrop._learn_from 失败", exc_info=True)
        return {"error": "%s: %s" % (type(e).__name__, e)}


def load_drops(n: int = 20) -> List[dict]:
    """最近 n 条低分，**新的在前**。"""
    rows = _read_jsonl(_drops_path())
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows[:max(1, int(n))]


def load_lessons() -> List[dict]:
    rows = _read_jsonl(_lessons_path())
    rows.sort(key=lambda r: (int(r.get("count") or 0), float(r.get("ts") or 0)),
              reverse=True)
    return rows


# ---------------------------------------------------------------- 语义检索

def _rec_text(r: Dict[str, Any]) -> str:
    """把一条教训拼成可检索文本。"""
    parts = [
        " ".join(r.get("kinds_zh") or []),
        " ".join(r.get("kinds") or []),
        str(r.get("root_cause") or ""),
        str(r.get("fix") or ""),
    ]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()[:1200]


def _ngrams(s: str, n: int = 2) -> set:
    s = re.sub(r"\s+", "", (s or "").lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _coverage(query: str, text: str) -> float:
    q = _ngrams(query)
    if not q:
        return 0.0
    t = _ngrams(text)
    if not t:
        return 0.0
    return len(q & t) / float(len(q))


def _cos(a, b) -> float:
    try:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a))
        db = math.sqrt(sum(y * y for y in b))
        if da <= 0 or db <= 0:
            return 0.0
        return float(num) / (da * db)
    except Exception:                                   # noqa: BLE001
        return 0.0


def _load_vec_cache() -> dict:
    try:
        if os.path.isfile(_vec_path()):
            with open(_vec_path(), encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                d.setdefault("backend", "")
                d.setdefault("dim", 0)
                d.setdefault("items", {})
                return d
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"backend": "", "dim": 0, "items": {}}


def _save_vec_cache(d: dict) -> None:
    try:
        p = _vec_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _embed(texts: List[str], is_query: bool = False):
    """返回 (vectors, sig)；不可用返回 ([], "")。

    ⚠️ 2026-11-22 实测踩到两个坑，都写在这里免得再犯：
      · `embed.embed_texts` 失败时返回 **None**（不是空列表）；
      · 成功时返回 **numpy.ndarray**，对它写 `if not vs` 会抛
        `ValueError: truth value of an array ... is ambiguous` ——
        被外层 except 吞掉后表现为「永远降级成纯字面检索」，而且不报错，
        最难查。所以这里必须用 `is None` / `len()` 判断，不能靠真假值。
      · 后端标识用 `embed.signature()`（形如 `onnx:bge-small-zh-v1.5:512`），
        它把模型名与维度都编进去了 —— 换模型时缓存会自动失配重建，
        而不是拿旧维度的向量算余弦（那会静默算错）。
    """
    try:
        from . import embed as _emb
        if not _emb.available():
            return [], ""
        vs = _emb.embed_texts(texts, is_query=is_query)
        if vs is None or len(vs) == 0:
            return [], ""
        sig = ""
        try:
            sig = str(_emb.signature() or "")
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
        return [[float(x) for x in v] for v in vs], sig
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop 向量化失败（降级为字面检索）", exc_info=True)
        return [], ""


def find_similar(text: str, top_k: int = 4) -> List[dict]:
    """按语义找类似的低分教训（向量 + 字面覆盖度混合）。"""
    try:
        text = str(text or "").strip()
        rows = load_lessons()[:_SCAN_MAX]
        if not rows:
            return []
        if not text:
            # 没给查询词 → 按出现次数排（最常犯的坑先给）
            return [dict(r, score=float(r.get("count") or 0)) for r in rows[:top_k]]
        cache = _load_vec_cache()
        keys = [str(r.get("key") or "") for r in rows]
        texts = [_rec_text(r) for r in rows]
        vecs, backend = _embed(texts, is_query=False)
        qv, qb = _embed([text], is_query=True)
        use_vec = bool(vecs and qv and len(qv[0]) == len(vecs[0]))
        if use_vec and cache.get("backend") == (backend or qb) \
                and int(cache.get("dim") or 0) == len(qv[0]):
            # 命中缓存就省一次文档侧编码
            cached = cache.get("items") or {}
            if all(k in cached for k in keys):
                vecs = [cached[k] for k in keys]
        elif use_vec:
            _save_vec_cache({"backend": backend or qb, "dim": len(qv[0]),
                             "items": {k: v for k, v in zip(keys, vecs)}})
        out = []
        for i, r in enumerate(rows):
            cov = _coverage(text, texts[i])
            if use_vec:
                sc = _W_VEC * _cos(qv[0], vecs[i]) + _W_COV * cov
            else:
                sc = cov
            if sc >= _MIN_SCORE:
                d = dict(r)
                d["score"] = round(float(sc), 3)
                d["match"] = "向量+字面" if use_vec else "字面（无向量模型）"
                out.append(d)
        out.sort(key=lambda x: -x["score"])
        return out[:max(1, int(top_k))]
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop.find_similar 失败", exc_info=True)
        return []


# ---------------------------------------------------------------- 给反思的摘要

def brief(n: int = 20, query: str = "") -> str:
    """给反思提示词用的文本：最近 n 次低分 + 语义相似的教训。

    用户 2026-11-22：「反思时结合这个日志并引以为戒」「最近 20 次」
                「用向量检索自动找出需要用到的类似的教训」
    """
    try:
        drops = load_drops(n)
        lessons = load_lessons()
        sim = find_similar(query, top_k=4) if query else []
        if not drops and not lessons:
            return ""
        lines: List[str] = []
        lines.append("## 评分退步日志（引以为戒，这是硬事实）")
        if drops:
            tot = sum(abs(float(d.get("delta") or 0)) for d in drops)
            lines.append("最近 %d 次退步，累计掉了 %.2f 分。" % (len(drops), tot))
            for d in drops:
                parts = [str(k) for k in (d.get("per_part") or {})
                         if (d.get("per_part") or {}).get(k)]
                seg = ("分项：" + "、".join(
                    "%s %+.2f" % (k, float((d.get("per_part") or {}).get(k) or 0))
                    for k in parts)) if parts else ""
                lines.append("· %s  %s → %s（%+.2f）%s"
                             % (d.get("iso") or "?", d.get("prev"),
                                d.get("total"), float(d.get("delta") or 0), seg))
                for s in (d.get("hot") or [])[:3]:
                    lines.append("    - 扣分点 %s:%s [%s] %s"
                                 % (s.get("file") or "?", s.get("line") or "?",
                                    s.get("kind") or "?", s.get("msg") or ""))
                rp = d.get("repair") or {}
                if rp.get("state") and rp.get("state") != "pending":
                    lines.append("    - 自动修复：%s %s"
                                 % (rp.get("state"), str(rp.get("msg") or "")[:160]))
        else:
            lines.append("（暂无退步记录）")
        if lessons:
            lines.append("")
            lines.append("## 低分教训（按出现次数排序，必须逐条对照）")
            for L in lessons[:8]:
                lines.append("· [%s×%d] %s" % (
                    "、".join(L.get("kinds_zh") or []) or "未分类",
                    int(L.get("count") or 1),
                    str(L.get("root_cause") or "")[:200]))
                lines.append("    修法：%s" % str(L.get("fix") or "")[:300])
        if sim:
            lines.append("")
            lines.append("## 与本轮最相似的教训（语义检索命中，优先参考）")
            for s in sim:
                lines.append("· 相似度 %.2f [%s] %s"
                             % (float(s.get("score") or 0),
                                "、".join(s.get("kinds_zh") or []),
                                str(s.get("root_cause") or "")[:200]))
                lines.append("    修法：%s" % str(s.get("fix") or "")[:300])
        lines.append("")
        lines.append("★ 制定改进计划时必须参考：这次掉分是**哪一类问题**、"
                     "落在**哪个文件哪一行**、以前是不是**犯过同样的错**。"
                     "不许只写「以后注意」——要写清改哪一行、改成什么、怎么验证。")
        return "\n".join(lines)
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop.brief 失败", exc_info=True)
        return ""


# ---------------------------------------------------------------- 自动修复

def _save_state(d: Dict[str, Any]) -> None:
    try:
        p = _state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _load_state() -> Dict[str, Any]:
    try:
        if os.path.isfile(_state_path()):
            with open(_state_path(), encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                return d
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"state": "idle", "rounds": [], "iso": ""}


def _repair_focus(row: Dict[str, Any]) -> str:
    """把一次退步转成反思的 focus 文本（告诉它这次必须修什么）。"""
    hot = row.get("hot") or []
    bits = []
    for s in hot[:6]:
        bits.append("%s:%s [%s] %s" % (s.get("file") or "?", s.get("line") or "?",
                                       s.get("kind") or "?", s.get("msg") or ""))
    return ("【评分退步自动修复】上一次 %s → 这次 %s（%+.2f）。"
            "必须把这些新增的扣分点修掉，修完分数要涨回去：%s"
            % (row.get("prev"), row.get("total"), float(row.get("delta") or 0),
               "；".join(bits) or "（未定位到具体行，先看静态分项）"))


def _do_repair(row: Dict[str, Any], max_rounds: int = _MAX_ROUNDS) -> None:
    """后台线程：反思 → 执行 → 重评，最多 max_rounds 轮。"""
    global _REPAIRING
    st = {"state": "running", "iso": _now(), "start": row.get("iso"),
          "prev": row.get("prev"), "total": row.get("total"),
          "delta": row.get("delta"), "rounds": [], "msg": ""}
    _save_state(st)
    try:
        from . import self_iterate as _SI
        from . import scoregate as _SG
        focus = _repair_focus(row)
        ok_any = False
        for i in range(max(1, int(max_rounds))):
            rnd = {"n": i + 1, "iso": _now(), "stage": "reflect"}
            st["rounds"].append(rnd)
            _save_state(st)
            try:
                out = _SI.reflect(trigger="score_drop", use_web=False,
                                  focus=focus, auto_execute=True) or {}
            except Exception as e:                      # noqa: BLE001
                rnd["stage"] = "reflect_failed"
                rnd["error"] = "%s: %s" % (type(e).__name__, e)
                _save_state(st)
                break
            rnd["plan_id"] = str(((out.get("plan") or {}).get("id")) or "")
            rnd["ok"] = bool(out.get("ok"))
            rnd["stage"] = "rescore"
            _save_state(st)
            # 重评（纯记录，不拦 —— 这里就是要拿到新分数）
            try:
                g = _SG.gate(force=True, note="自动修复后重评", block_on_drop=False) or {}
            except Exception as e:                      # noqa: BLE001
                rnd["stage"] = "rescore_failed"
                rnd["error"] = "%s: %s" % (type(e).__name__, e)
                _save_state(st)
                break
            rnd["total"] = g.get("total")
            rnd["delta"] = g.get("delta")
            rnd["stage"] = "done"
            _save_state(st)
            d = g.get("delta")
            if d is not None and float(d) >= 0:
                ok_any = True
                st["state"] = "fixed"
                st["msg"] = "第 %d 轮修复后 %s → %s（%+.2f），分数已涨回" % (
                    i + 1, row.get("total"), g.get("total"), float(d))
                break
            # 没涨回去 → 把新的扣分点并进 focus，继续下一轮
            focus = focus + "\n上一轮修完还是掉了 %+.2f，继续修：" % float(d or 0)
        if st["state"] == "running":
            last = (st["rounds"] or [{}])[-1]
            if str(last.get("stage") or "").endswith("_failed"):
                st["state"] = "error"
                st["msg"] = "自动修复中途失败：%s" % str(last.get("error") or "")[:200]
            else:
                st["state"] = "fixed" if ok_any else "exhausted"
                if not ok_any:
                    st["msg"] = ("%d 轮自动修复都没把分数涨回去，停下来等人工判断"
                                 % len(st["rounds"]))
    except Exception as e:                              # noqa: BLE001
        logger.debug("scoredrop 自动修复失败", exc_info=True)
        st["state"] = "error"
        st["msg"] = "%s: %s" % (type(e).__name__, e)
    finally:
        st["end_iso"] = _now()
        _save_state(st)
        _mark_repaired(st)
        _REPAIRING = False


def _mark_repaired(st: Dict[str, Any]) -> None:
    """把修复结果回写到那条低分记录上（按 iso 找）。"""
    try:
        start = str(st.get("start") or "")
        if not start:
            return
        rows = _read_jsonl(_drops_path())
        hit = False
        for r in rows:
            if str(r.get("iso") or "") == start:
                r["repair"] = {"state": st.get("state"), "msg": st.get("msg") or "",
                               "rounds": len(st.get("rounds") or []),
                               "end_iso": st.get("end_iso") or ""}
                hit = True
        if not hit:
            return
        with _LOCK:
            p = _drops_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("scoredrop._mark_repaired 失败", exc_info=True)


def trigger_repair(row: Dict[str, Any], max_rounds: int = _MAX_ROUNDS) -> Dict[str, Any]:
    """起一个后台线程做自动修复（立刻返回，不阻塞调用方）。"""
    global _REPAIRING
    if _REPAIRING:
        return {"started": False, "why": "已有一轮自动修复在跑"}
    _REPAIRING = True
    try:
        t = threading.Thread(target=_do_repair, args=(row, max_rounds),
                             name="scoredrop-repair", daemon=True)
        t.start()
        return {"started": True, "max_rounds": max_rounds}
    except Exception as e:                              # noqa: BLE001
        _REPAIRING = False
        return {"started": False, "why": "%s: %s" % (type(e).__name__, e)}


def repair_status() -> Dict[str, Any]:
    d = _load_state()
    return {"state": d.get("state") or "idle",
            "running": bool(_REPAIRING),
            "iso": d.get("iso") or "",
            "msg": d.get("msg") or "",
            "rounds": [{"n": r.get("n"), "stage": r.get("stage"),
                        "total": r.get("total"), "delta": r.get("delta"),
                        "plan_id": r.get("plan_id")}
                       for r in (d.get("rounds") or [])][-5:]}


# ---------------------------------------------------------------- 只读快照

def status(n: int = 20) -> Dict[str, Any]:
    drops = load_drops(n)
    lessons = load_lessons()
    rs = repair_status()
    tot = sum(abs(float(d.get("delta") or 0)) for d in drops)
    return {
        "count": len(drops),
        "total_lost": round(tot, 2),
        "drops": [{
            "iso": d.get("iso"), "prev": d.get("prev"), "total": d.get("total"),
            "delta": d.get("delta"), "per_part": d.get("per_part") or {},
            "summary": str(d.get("summary") or "")[:200],
            "repair": d.get("repair") or {},
            "hot": [{"file": s.get("file"), "line": s.get("line"),
                     "kind": s.get("kind"), "msg": str(s.get("msg") or "")[:200],
                     "in_changed": s.get("in_changed")}
                    for s in (d.get("hot") or [])[:8]],
            "suspects_n": len(d.get("suspects") or []),
        } for d in drops],
        "lessons": [{
            "key": L.get("key"), "kinds_zh": L.get("kinds_zh") or [],
            "count": int(L.get("count") or 1),
            "root_cause": str(L.get("root_cause") or "")[:300],
            "fix": str(L.get("fix") or "")[:300],
            "last_iso": L.get("last_iso"),
        } for L in lessons[:20]],
        "repair": rs,
    }
