# -*- coding: utf-8 -*-
"""记忆库的**条目级**向量索引（长期 / 短期 / 历史会话）。

为什么要这一层
--------------
原来的 `memory.search_all` 是「整块正文比一次、整块正文吐回去」：
长期记忆上限 15000 字、现在 67 条，查询命中一条就把**全部**返回 ——
既塞满 context，又定位不到具体是哪一条。而且它只做 2-gram 交集，
换个说法（「我叫什么」vs「用户姓名：陈宇桐」）就完全召回不到。

这里的做法
----------
· **拆成条目**：按 `；` / 换行切开，一条一个向量；
· **混合打分**：语义向量（主）+ 关键词覆盖率（兜底），
  语义分或覆盖率**任一达标就收**，两个都不过就不返回（宁缺毋滥）；
· **增量更新**：按条目内容的 hash 复用旧向量，只给新增/改动的条目算；
  记忆一写入就自动同步（后台线程，不阻塞工具调用）；
· **不依赖模型也能用**：没有可用 embedding 模型时自动退化为**条目级**关键词，
  功能不会整个挂掉。

和知识库（kb.py）的区别：那边是长文档切块，这边是短条目；
所以这边不做重叠、不做块大小控制，直接一条一条来。
"""
import os
import json
import time
import hashlib
import threading
import logging

import numpy as np

from . import embed
from . import memory

_LOG = logging.getLogger("uvicorn.error")

VEC_FILE = os.path.join(memory.MEM_DIR, "_vec.npy")
META_FILE = os.path.join(memory.MEM_DIR, "_vec_meta.json")

# 门槛：**按实测校准**（bge-small-zh 上，记忆条目相关约 0.45~0.75，无关约 0.15~0.30）
MIN_VEC = 0.42       # 语义分门槛
MIN_COV = 0.50       # 关键词覆盖率门槛（查询词有多少能在条目里找到）
W_VEC, W_COV = 0.75, 0.25   # 融合权重（只用于排序，不做拦截）

MAX_ITEMS = 4000          # 条目上限，超了只取最近的，避免索引无限膨胀
TRANS_PASSAGE = 600       # 历史会话一段最多多少字
MAX_TRANS_BYTES = 12 * 1024 * 1024   # 转录总量超过这个就不进索引（防止拖慢）

_lock = threading.RLock()
_idx = {"sig": "", "dsig": "", "items": [], "mat": None}
_dirty = True
_syncing = False


# =====================================================================
#  条目收集
# =====================================================================
def _mk(level, sid, section, text, source="auto"):
    key = hashlib.md5(
        ("%s|%s|%s" % (level, sid, text)).encode("utf-8")).hexdigest()[:16]
    return {"key": key, "level": level, "sid": sid or "",
            "section": section or "", "text": text, "source": source}


def _push(items, level, sid, section, text, source="auto"):
    t = memory._norm_point(text)
    if len(t) < 2:
        return
    items.append(_mk(level, sid, section, t, source))


def _collect_transcripts(items):
    """历史会话按「一段对话」成条，不是一句一条（单句没有上下文，检索出来没用）。"""
    d = memory.TRANSCRIPT_DIR
    if not os.path.isdir(d):
        return
    files = [f for f in os.listdir(d) if f.endswith(".jsonl")]
    total = 0
    for fn in files:
        try:
            total += os.path.getsize(os.path.join(d, fn))
        except Exception:
            pass
    if total > MAX_TRANS_BYTES:
        _LOG.warning("历史会话总量 %.1fMB 超过上限，本轮跳过转录索引", total / 1048576.0)
        return

    for fn in files:
        path = os.path.join(d, fn)
        buf, buf_len = [], 0

        def _flush():
            if not buf:
                return
            txt = "\n".join("%s：%s" % (r, c) for r, c in buf)[:TRANS_PASSAGE]
            _push(items, "历史会话", fn[:-6], fn[:-6], txt, "transcript")

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    role = o.get("role")
                    if role == "sep":
                        _flush()
                        buf, buf_len = [], 0
                        continue
                    if role not in ("user", "assistant"):
                        continue
                    c = str(o.get("content") or "").strip()
                    if not c:
                        continue
                    buf.append(("用户" if role == "user" else "助手", c[:400]))
                    buf_len += len(c)
                    if buf_len >= TRANS_PASSAGE:
                        _flush()
                        buf, buf_len = [], 0
                _flush()
        except Exception as e:
            _LOG.warning("读转录失败（%s）：%s", fn, e)


def _collect_items():
    items = []
    # ① 长期记忆
    for t in memory._split_items(memory.get_long()):
        _push(items, "长期记忆", "", "所有对话共享", t)
    # ② 短期记忆（**所有对话**，不只是当前这个）
    try:
        shorts = memory._load_shorts()
    except Exception:
        shorts = {}
    for sid, obj in (shorts or {}).items():
        body = str((obj or {}).get("content") or "")
        for t in memory._split_items(body):
            _push(items, "短期记忆", sid, sid, t)
    # ③ 历史会话
    _collect_transcripts(items)

    # 超上限时保留**靠后**的（新条目更重要）
    if len(items) > MAX_ITEMS:
        items = items[-MAX_ITEMS:]
    return items


# =====================================================================
#  索引构建（增量）
# =====================================================================
def _data_sig():
    """数据指纹：文件没变就不用重新收集条目。"""
    parts = []
    for p in (memory.LONG_FILE, memory.SHORT_FILE):
        try:
            parts.append("%d:%d" % (os.path.getsize(p), int(os.path.getmtime(p))))
        except Exception:
            parts.append("0")
    d = memory.TRANSCRIPT_DIR
    n, sz, mt = 0, 0, 0
    if os.path.isdir(d):
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                st = os.stat(os.path.join(d, fn))
                n += 1
                sz += st.st_size
                mt = max(mt, int(st.st_mtime))
        except Exception:
            pass
    parts.append("t:%d:%d:%d" % (n, sz, mt))
    return "|".join(parts)


def _load_disk():
    try:
        if not (os.path.isfile(VEC_FILE) and os.path.isfile(META_FILE)):
            return None
        with open(META_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f)
        arr = np.load(VEC_FILE)
        keys = list(meta.get("keys") or [])
        if arr.ndim != 2 or arr.shape[0] != len(keys):
            return None
        return {"sig": meta.get("sig") or "",
                "vecs": {k: arr[i] for i, k in enumerate(keys)}}
    except Exception:
        return None


def _save_disk(sig, items, mat):
    try:
        os.makedirs(memory.MEM_DIR, exist_ok=True)
        if mat is None or not items:
            for p in (VEC_FILE, META_FILE):
                if os.path.isfile(p):
                    os.remove(p)
            return
        tmp = VEC_FILE + ".tmp.npy"
        np.save(tmp, mat)
        os.replace(tmp, VEC_FILE)
        meta = {"sig": sig, "updated_at": time.time(),
                "keys": [it["key"] for it in items],
                "items": [{"level": it["level"], "sid": it["sid"],
                           "text": it["text"][:200]} for it in items]}
        tmp2 = META_FILE + ".tmp"
        with open(tmp2, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp2, META_FILE)
    except Exception as e:
        _LOG.warning("记忆索引落盘失败：%s", e)


def sync(force: bool = False):
    """把索引同步到最新（增量：只算新增/改动的条目）。"""
    global _dirty
    with _lock:
        dsig = _data_sig()
        sig = embed.signature()
        if (not force and _idx["mat"] is not None
                and _idx["sig"] == sig and _idx.get("dsig") == dsig):
            return _idx

        items = _collect_items()
        keys = [it["key"] for it in items]

        cache = {}
        disk = _load_disk()
        if disk and disk["sig"] == sig:
            cache = disk["vecs"]

        missing = [it for it in items if it["key"] not in cache]
        if missing and sig != "none":
            vecs = embed.embed_texts([it["text"] for it in missing])
            if vecs is not None:
                for it, v in zip(missing, vecs):
                    cache[it["key"]] = np.asarray(v, dtype=np.float32)
            else:
                _LOG.warning("记忆向量化失败，本轮退化为条目级关键词检索")

        mat = None
        if keys and all(k in cache for k in keys):
            try:
                mat = np.stack([cache[k] for k in keys]).astype(np.float32)
            except Exception:
                mat = None

        _idx.update({"sig": sig, "dsig": dsig, "items": items, "mat": mat})
        _dirty = False
        _save_disk(sig, items, mat)
        return _idx


def _bg_sync():
    global _syncing
    try:
        time.sleep(0.25)          # 防抖：连续写入只同步一次
        sync()
    except Exception as e:
        _LOG.warning("记忆索引后台同步失败：%s", e)
    finally:
        with _lock:
            _syncing = False


def touch():
    """记忆被写入后调用：标记脏 + 后台增量同步（不阻塞调用方）。"""
    global _dirty, _syncing
    _dirty = True
    with _lock:
        if _syncing:
            return
        _syncing = True
    try:
        threading.Thread(target=_bg_sync, daemon=True).start()
    except Exception:
        with _lock:
            _syncing = False


# =====================================================================
#  检索
# =====================================================================
def _label(it, sid):
    lv = it["level"]
    if lv == "短期记忆":
        return "本对话短期" if (sid and it["sid"] == sid) else "其他对话短期"
    return lv


def search(query: str, sid: str = "", top_k: int = 5,
           min_vec: float = MIN_VEC, min_cov: float = MIN_COV) -> list:
    """条目级检索。返回命中的**条目**（不是整块正文），无关时返回空列表。"""
    q = (query or "").strip()
    if not q:
        return []
    st = sync()
    items, mat = st["items"], st["mat"]
    if not items:
        return []

    qt = set(memory._tokenize(q))
    vec_scores = None
    if mat is not None:
        qv = embed.embed_texts([q], is_query=True)
        if qv is not None and qv.ndim == 2 and qv.shape[1] == mat.shape[1]:
            vec_scores = (mat @ qv[0]).astype(float).tolist()

    out = []
    for i, it in enumerate(items):
        it_tokens = set(memory._tokenize(it["text"]))
        cov = (len(qt & it_tokens) / len(qt)) if qt else 0.0
        vec = float(vec_scores[i]) if vec_scores else 0.0
        if vec >= min_vec or cov >= min_cov:
            score = (W_VEC * vec + W_COV * cov) if vec_scores else cov
            out.append({
                "level": _label(it, sid),
                "section": it["section"],
                "content": it["text"],
                "source": it["source"],
                "score": round(float(score), 4),
                "vec": round(vec, 4),
                "cov": round(cov, 3),
            })

    out.sort(key=lambda x: x["score"], reverse=True)
    # 去重：同一句话可能既在长期又在短期
    seen, res = set(), []
    for h in out:
        k = h["content"][:80]
        if k in seen:
            continue
        seen.add(k)
        res.append(h)
    return res[:top_k]


def status() -> dict:
    st = sync()
    info = embed.info()
    levels = {}
    for it in st["items"]:
        levels[it["level"]] = levels.get(it["level"], 0) + 1
    return {"items": len(st["items"]), "levels": levels,
            "indexed": st["mat"] is not None,
            "backend": info.get("backend", ""), "model": info.get("model", ""),
            "dim": info.get("dim", 0), "ready": bool(info.get("ready")),
            "error": info.get("error", "")}


def rebuild() -> dict:
    """强制全量重建（换模型、或怀疑索引坏了时用）。"""
    with _lock:
        _idx.update({"sig": "", "dsig": "", "items": [], "mat": None})
        for p in (VEC_FILE, META_FILE):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    t0 = time.time()
    st = sync(force=True)
    return {"items": len(st["items"]), "indexed": st["mat"] is not None,
            "ms": int((time.time() - t0) * 1000),
            "backend": embed.info().get("backend", ""),
            "model": embed.info().get("model", "")}
