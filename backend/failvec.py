# -*- coding: utf-8 -*-
"""失败归因的**语义级**相似检索。

为什么需要
----------
`failattr.brief()` 原来只按 `kind` / `files` / `stage` 做**聚类统计**：
它能告诉你「patch_not_found 这类失败了 7 次」，但答不了更重要的一问——

    「我这次打算做的事，和历史上哪一次**踩的是同一个坑**？」

用户 2026-11-22 明确要求：「加进去，而且要实现体系化完美运行」。
纯分类聚合的盲区很具体：同样是 `patch_not_found`，
「锚点写太死」和「改的是同一个函数但行号已变」是两回事，
只看 kind 会把它们算成同一类，反思拿不到真正可复用的那条教训。

做法
----
对每条历史失败记录拼一段可检索文本（标题 + 类型 + 涉及文件 + 失败原因
+ 失败上下文 + 教训），做**向量 + 字面覆盖度混合**打分：

    score = 0.72 * cosine  +  0.28 * 字面覆盖度

为什么要混而不是纯向量：中文短文本（「锚点没命中」这种 6 个字）向量本身
信息量少，向量空间里近邻噪声大；字面覆盖度能兜住「同一个报错原文」这类
精确重复。反过来说，纯字面又抓不到「文件被占用」≈「PermissionError」这种
换了说法的同一个坑。两者互补，实测比单用任一种都稳。

向量来源复用项目已有的 `backend/embed.py`（本地 onnx，bge-small-zh-v1.5，
512 维，不联网、不占显存）。它不可用时**自动降级为纯字面检索**，
绝不抛异常、绝不静默返回空——降级状态会写进 status() 供前端显示。

存储：data/self_iterate/failvec.json（向量缓存，按记录 key 增量，不重算）
"""
import os
import json
import math
import time
import threading

from . import config

_LOCK = threading.RLock()

# 混合权重：向量为主，字面兜底
_W_VEC = 0.72
_W_COV = 0.28

# 低于这个分数不算「相似」——宁缺勿滥，把不相干的记录塞进提示词
# 比不给更糟（模型会照着无关的教训乱改方向）。
_MIN_SCORE = 0.34

# 一次最多参与比较的历史记录条数（防止归因表上千条时每轮都全量算）
_SCAN_MAX = 400


def _dir() -> str:
    return os.path.join(config.data("data"), "self_iterate")


def _cache_path() -> str:
    return os.path.join(_dir(), "failvec.json")


# ---------------------------------------------------------------- 文本拼装
def _rec_text(r: dict) -> str:
    """把一条归因记录拼成可检索文本。

    只取信息密度高的字段：类型 + 标题 + 文件 + 原因 + 上下文 + 教训。
    traceback 不进（几 KB 的堆栈会把语义向量带偏到「函数名匹配」上，
    实测那样「同一个坑换了个函数」就检索不到了）。
    """
    parts = [
        str(r.get("kind_zh") or r.get("kind") or ""),
        str(r.get("title") or "")[:120],
        " ".join(str(f) for f in (r.get("files") or [])[:6]),
        str(r.get("why_failed") or "")[:400],
        str(r.get("context") or "")[:200],
        str(r.get("lesson") or "")[:160],
        str(r.get("stage_zh") or r.get("stage") or ""),
    ]
    return " ｜ ".join(p for p in parts if p and p.strip())


def _rec_key(r: dict) -> str:
    """稳定 key：同一秒两条同名记录也不能撞（加原因前 40 字）。"""
    return "%s|%s|%s" % (
        r.get("iso") or r.get("ts") or "",
        str(r.get("title") or "")[:40],
        str(r.get("why_failed") or "")[:40],
    )


# ---------------------------------------------------------------- 字面覆盖度
def _ngrams(s: str, n: int = 2) -> set:
    s = "".join(ch for ch in (s or "").lower() if not ch.isspace())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _coverage(query: str, text: str) -> float:
    """query 的 2-gram 有多大比例出现在 text 里。

    用「覆盖 query」而不是 Jaccard：Jaccard 会被长文本稀释——
    历史记录往往比 query 长得多，Jaccard 会把真正相关的记录也压到很低。
    """
    q = _ngrams(query)
    if not q:
        return 0.0
    t = _ngrams(text)
    if not t:
        return 0.0
    hit = len(q & t)
    return hit / float(len(q))


# ---------------------------------------------------------------- 向量与缓存
def _load_cache() -> dict:
    try:
        p = _cache_path()
        if not os.path.isfile(p):
            return {}
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                     # noqa: BLE001
        return {}


def _save_cache(d: dict) -> None:
    try:
        os.makedirs(_dir(), exist_ok=True)
        p = _cache_path()
        tmp = p + ".tmp"
        # 只保留最近 800 条向量，缓存文件不无限膨胀
        items = d.get("items") or {}
        if len(items) > 800:
            ks = sorted(items.keys())[-800:]
            d["items"] = {k: items[k] for k in ks}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:                                     # noqa: BLE001
        pass


def _embed_ready() -> dict:
    """探测向量后端。失败不抛异常，返回 ready=False。"""
    try:
        from . import embed
        if not embed.available():
            return {"ready": False, "reason": "embed.available() 为 False",
                    "sig": ""}
        info = embed.info() or {}
        if not info.get("ready"):
            return {"ready": False,
                    "reason": str(info.get("error") or "向量模型未就绪"),
                    "sig": ""}
        return {"ready": True, "reason": "",
                "sig": str(embed.signature() or ""),
                "model": str(info.get("model") or "")}
    except Exception as e:                                # noqa: BLE001
        return {"ready": False, "reason": "%s: %s" % (type(e).__name__, e),
                "sig": ""}


def _vectors_for(recs: list) -> tuple:
    """拿到这批记录的向量矩阵（带磁盘缓存增量）。

    返回 (mat, ready_info)：mat 为 list[list[float]] 或 None（降级）。
    """
    inf = _embed_ready()
    if not inf.get("ready"):
        return (None, inf)
    sig = inf.get("sig") or ""
    cache = _load_cache()
    if str(cache.get("sig") or "") != sig:
        # 换模型/换维度 → 旧向量全部作废（维度不同直接算不了）
        cache = {"sig": sig, "items": {}}
    items = cache.get("items") or {}
    todo, todo_keys = [], []
    for r in recs:
        k = _rec_key(r)
        if k not in items:
            todo.append(_rec_text(r))
            todo_keys.append(k)
    if todo:
        try:
            from . import embed
            vs = embed.embed_texts(todo, is_query=False)
            # ⚠️ embed_texts 返回的是 numpy 数组，不能用 `if vs and ...` 判断
            #    （ndarray 的真值判断会抛 ValueError）。用 len()。
            n = len(vs)
            for i, k in enumerate(todo_keys):
                if i < n:
                    items[k] = [round(float(x), 6) for x in vs[i]]
            cache["items"] = items
            cache["sig"] = sig
            _save_cache(cache)
        except Exception as e:                            # noqa: BLE001
            return (None, {"ready": False, "sig": sig,
                           "reason": "向量化失败 %s: %s" % (type(e).__name__, e)})
    mat = []
    for r in recs:
        mat.append(items.get(_rec_key(r)) or [])
    if not mat or not mat[0]:
        return (None, {"ready": False, "sig": sig, "reason": "向量为空"})
    return (mat, inf)


def _cos(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------- 主接口
def similar(text: str, top_k: int = 5, days: int = 90,
            min_score: float = _MIN_SCORE) -> dict:
    """找与 `text` 语义最像的历史失败记录。

    返回 {"ok": bool, "items": [...], "backend": "vec|cov", "scanned": n,
          "note": str}
    items 每项：{score, vec, cov, title, kind, kind_zh, files, why_failed,
                why_rollback, lesson, context, iso, age_days}
    """
    q = str(text or "").strip()
    if not q:
        return {"ok": False, "items": [], "backend": "none", "scanned": 0,
                "note": "query 为空"}
    try:
        from . import failattr as _FA
        rows = _FA.load(0) or []
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "items": [], "backend": "none", "scanned": 0,
                "note": "归因表读取失败 %s: %s" % (type(e).__name__, e)}
    since = time.time() - max(1, int(days)) * 86400
    rows = [r for r in rows if float(r.get("ts") or 0) >= since]
    if not rows:
        return {"ok": True, "items": [], "backend": "none", "scanned": 0,
                "note": "近 %d 天没有失败记录" % int(days)}
    rows = rows[-_SCAN_MAX:]
    texts = [_rec_text(r) for r in rows]

    mat, inf = _vectors_for(rows)
    backend = "vec" if mat else "cov"
    qvec = None
    if mat:
        try:
            from . import embed
            qv = embed.embed_texts([q], is_query=True)
            if len(qv):
                qvec = [float(x) for x in qv[0]]
        except Exception:                                 # noqa: BLE001
            qvec = None
    # ⚠️ 2026-11-22 实测修：原来只在 except 分支降级，`embed` 正常返回但
    #   结果为空（qv 长度 0）时 backend 仍停在 "vec"，于是走
    #   `0.72*vec + 0.28*cov` 而 vec 恒为 0 → 分数只剩字面的 0.28 倍，
    #   静默偏低、几乎召回不到东西。判据改成「有没有真拿到查询向量」。
    if not qvec:
        backend = "cov"
    # 降级到纯字面时阈值必须同步下调。
    # ★ 2026-11-22 实测数据（6 条记录 × 6 个 query）：
    #   相关的 cov = 0.083 / 0.111 / 0.222；无关的 cov = 0.000 / 0.000 / 0.000。
    #   即**区分度很好，只是绝对分低**——所以阈值要按这个分布定，不能拍脑袋。
    #   取 0.34 × 0.22 ≈ 0.075：能过全部相关样本，且无关样本仍然 0 召回。
    #   （cov 的分母是 query 的 n-gram 数，不随记录变长而稀释，所以这个阈值
    #     在记录很长时同样成立。）
    _min = float(min_score) if backend == "vec" else float(min_score) * 0.22

    out = []
    for i, r in enumerate(rows):
        cov = _coverage(q, texts[i])
        vec = _cos(qvec, mat[i]) if (qvec and mat) else 0.0
        if backend == "vec":
            score = _W_VEC * vec + _W_COV * cov
        else:
            score = cov
        if score < _min:
            continue
        out.append({
            "score": round(score, 4),
            "vec": round(vec, 4),
            "cov": round(cov, 4),
            "title": str(r.get("title") or "")[:120],
            "kind": str(r.get("kind") or ""),
            "kind_zh": str(r.get("kind_zh") or r.get("kind") or ""),
            "stage_zh": str(r.get("stage_zh") or r.get("stage") or ""),
            "files": [str(f) for f in (r.get("files") or [])][:6],
            "why_failed": str(r.get("why_failed") or "")[:400],
            "why_rollback": str(r.get("why_rollback") or "")[:240],
            "lesson": str(r.get("lesson") or "")[:200],
            "context": str(r.get("context") or "")[:200],
            "iso": str(r.get("iso") or ""),
            "age_days": round((time.time() - float(r.get("ts") or 0)) / 86400.0, 1),
        })
    out.sort(key=lambda x: -x["score"])
    return {"ok": True, "items": out[:max(1, int(top_k))], "backend": backend,
            "scanned": len(rows),
            "note": "" if backend == "vec" else
                    ("向量后端不可用，已降级为字面检索：%s"
                     % str((inf or {}).get("reason") or ""))}


def brief_similar(text: str, top_k: int = 4, days: int = 90,
                  max_chars: int = 1100) -> str:
    """把相似历史失败压成一段给反思提示词用（没有命中返回空串）。"""
    r = similar(text, top_k=top_k, days=days)
    items = r.get("items") or []
    if not items:
        return ""
    head = ("## 与本次要做的事**语义相近**的历史失败（共 %d 条命中，按相似度排；"
            "后端=%s）\n★ 这些是「换了说法但其实是同一个坑」的记录 —— "
            "做之前先判断这次会不会再踩，会踩就先按「教训」改做法。"
            % (len(items), r.get("backend")))
    lines = [head]
    for i, it in enumerate(items, 1):
        seg = ["%d) 相似度 %.2f（向量 %.2f / 字面 %.2f）｜ [%s/%s] %s"
               % (i, it["score"], it["vec"], it["cov"], it["kind_zh"],
                  it["stage_zh"] or "-", it["title"] or "未命名")]
        if it.get("files"):
            seg.append("   涉及：%s" % "、".join(it["files"]))
        if it.get("why_failed"):
            seg.append("   当时为什么失败：%s"
                       % it["why_failed"].replace("\n", " ")[:260])
        if it.get("lesson"):
            seg.append("   教训：%s" % it["lesson"].replace("\n", " ")[:180])
        if it.get("age_days") is not None:
            seg.append("   （%s，%.1f 天前）" % (it.get("iso") or "-", it["age_days"]))
        lines.append("\n".join(seg))
    s = "\n".join(lines)
    if len(s) > max_chars:
        s = s[:max_chars] + "\n…（相似失败较多，已截断）"
    return s


def status() -> dict:
    """供前端 / 自检用：后端是否就绪、缓存多少条、最近一次是否降级。"""
    inf = _embed_ready()
    c = _load_cache()
    try:
        from . import failattr as _FA
        n = len(_FA.load(0) or [])
    except Exception:                                     # noqa: BLE001
        n = -1
    return {
        "vec_ready": bool(inf.get("ready")),
        "model": inf.get("model") or "",
        "sig": inf.get("sig") or "",
        "reason": inf.get("reason") or "",
        "records": n,
        "cached_vectors": len((c.get("items") or {})),
        "weights": {"vec": _W_VEC, "cov": _W_COV},
        "min_score": _MIN_SCORE,
    }


def invalidate() -> dict:
    """清掉向量缓存（归因表被裁剪 / 手动重建时用）。"""
    try:
        with _LOCK:
            p = _cache_path()
            if os.path.isfile(p):
                os.remove(p)
        return {"ok": True, "cleared": True}
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
