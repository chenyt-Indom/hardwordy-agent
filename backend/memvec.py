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
· **记忆拆成条目**（按 `；` / 换行切），一条一个向量；
· **混合打分**：语义向量（主）+ 关键词覆盖（兜底），
  语义分或覆盖率**任一达标就收**，两个都不过就不返回（宁缺毋滥）；
· **增量更新**：按条目内容 hash 复用旧向量，只给新增/改动的条目算；
  记忆一写入就自动同步（后台线程，不阻塞工具调用）；
· **转录不进大索引**：实测本机 233 个 .jsonl / 19.6MB，全量索引要 4 分钟、
  索引矩阵几十 MB。改成「关键词预筛 → 向量重排」：先按字面挑出候选，
  只给这十几条算向量。延迟 0.1 秒级，而且永远是最新的。

关键词覆盖为什么不用 2-gram
---------------------------
实测踩过：查询「我在哪个学校读书」，记忆里那条「学校：广州民航职业技术学院…」
的 2-gram 交集只有 {学校}，覆盖率 0.14（门槛 0.5），而它的语义分 0.381
（门槛 0.42）—— 两个信号同时不过，**明明相关的条目就是召回不到**。
原因是 2-gram 会被断词切碎（「哪个学」「校读书」都匹配不上「学校」）。
所以改用**字级覆盖**（去掉虚词后看查询的字有多少在条目里），
同一条查询覆盖率是 2/4=0.5，正常召回。
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

# 门槛：按实测校准（bge-small-zh：相关条目 vec 约 0.45~0.78，无关约 0.15~0.35）
MIN_VEC = 0.42
MIN_COV = 0.50
W_VEC, W_COV = 0.75, 0.25        # 只用于排序，不参与拦截

MAX_ITEMS = 4000                 # 记忆条目上限
# 转录：不进索引，只做预筛 + 重排
TRANS_PASSAGE = 600              # 一段对话最多多少字
TRANS_MAX = 2500                 # 最多收集多少段（按文件 mtime 从新到旧）
TRANS_PER_FILE = 400             # 单个文件最多取多少段（防止一个巨型测试会话吃掉全部名额）
TRANS_CANDIDATES = 14            # 预筛出来送去向量重排的候选数
TRANS_TOP = 2                    # 转录最多挤进结果几条

_lock = threading.RLock()
_idx = {"sig": "", "dsig": "", "items": [], "mat": None}
_trans = {"sig": "", "items": []}
_dirty = True
_syncing = False

# 虚词字：算字级覆盖率时先剔掉，否则「我」「的」「是」这种会白送覆盖率
_STOP_CHARS = set(
    "的了是在和与我你他她它吧呢啊吗么什怎哪谁几这那个有也都就还很会要能可"
    "请帮下换或并及想说对把被从到给让用做去来样些时候您咱其之而已")
_PUNCT = set("，。、；：？！…—·「」『』（）《》〈〉,.!?;:\"'()[]{}<>~`@#$%^&*_-+=/\\| \t\n\r")


# =====================================================================
#  打分工具
# =====================================================================
def _char_cov(query: str, text: str) -> float:
    """查询里的**实字**有多少出现在条目里（0~1）。抗断词，短查询也稳。"""
    qc = [c for c in (query or "")
          if c not in _STOP_CHARS and c not in _PUNCT and not c.isspace()]
    if not qc:
        return 0.0
    tc = set(text or "")
    return sum(1 for c in qc if c in tc) / float(len(qc))


def _coverage(query: str, text: str) -> float:
    """综合覆盖率：字级覆盖为主，2-gram 覆盖为辅，取较大值。"""
    q = (query or "").strip()
    if not q:
        return 0.0
    best = _char_cov(q, text)
    qt = set(memory._tokenize(q))
    if qt:
        it = set(memory._tokenize(text))
        best = max(best, len(qt & it) / float(len(qt)))
    return best


# =====================================================================
#  条目收集（长期 + 短期）
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


def _collect_items():
    items = []
    for t in memory._split_items(memory.get_long()):
        _push(items, "长期记忆", "", "所有对话共享", t)
    try:
        shorts = memory._load_shorts()
    except Exception:
        shorts = {}
    for sid, obj in (shorts or {}).items():
        body = str((obj or {}).get("content") or "")
        for t in memory._split_items(body):
            _push(items, "短期记忆", sid, sid, t)
    if len(items) > MAX_ITEMS:
        items = items[-MAX_ITEMS:]     # 超限时保留靠后的（新条目更重要）
    return items


def _data_sig():
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


# =====================================================================
#  索引构建（增量）
# =====================================================================
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
    """把记忆条目索引同步到最新（增量：只算新增/改动的条目）。"""
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
                _LOG.warning("记忆向量化失败，本轮退化为关键词检索")

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
#  历史会话：预筛 + 向量重排（不进大索引）
# =====================================================================
def _trans_items():
    """把转录文件切成「一段对话」的候选，按文件新→旧收集，有上限。"""
    with _lock:
        dsig = _data_sig()
        if _trans["sig"] == dsig:
            return _trans["items"]

    d = memory.TRANSCRIPT_DIR
    out = []
    if os.path.isdir(d):
        files = []
        for fn in os.listdir(d):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            try:
                files.append((os.path.getmtime(p), fn, p))
            except Exception:
                pass
        files.sort(reverse=True)          # 新的优先

        for _, fn, path in files:
            if len(out) >= TRANS_MAX:
                break
            taken = 0
            buf, buf_len = [], 0

            def _flush():
                if not buf or taken >= TRANS_PER_FILE:
                    return
                txt = "\n".join("%s：%s" % (r, c) for r, c in buf)[:TRANS_PASSAGE]
                out.append({"key": "T:" + fn + ":" + str(len(out)),
                            "level": "历史会话", "sid": fn[:-6],
                            "section": fn[:-6], "text": txt, "source": "transcript"})

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
                            taken += 1
                    _flush()
            except Exception as e:
                _LOG.warning("读转录失败（%s）：%s", fn, e)

    with _lock:
        _trans.update({"sig": dsig, "items": out})
    return out


def _search_transcripts(query: str, top_k: int = TRANS_TOP) -> list:
    items = _trans_items()
    if not items:
        return []
    scored = []
    for it in items:
        cov = _coverage(query, it["text"])
        if cov >= MIN_COV:
            scored.append((cov, it))
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    cand = scored[:TRANS_CANDIDATES]

    vecs = None
    try:
        vecs = embed.embed_texts([it["text"] for _, it in cand])
        qv = embed.embed_texts([query], is_query=True)
        if vecs is None or qv is None:
            vecs = None
    except Exception:
        vecs = None

    out = []
    for i, (cov, it) in enumerate(cand):
        vec = 0.0
        if vecs is not None:
            vec = float(np.dot(vecs[i], qv[0]))
            if vec < MIN_VEC and cov < 0.75:
                continue          # 语义也不像、字面也只是勉强 → 不要
        score = (W_VEC * vec + W_COV * cov) if vecs is not None else cov
        out.append({"level": "历史会话", "section": it["section"],
                    "content": it["text"], "source": "transcript",
                    "score": round(float(score), 4),
                    "vec": round(vec, 4), "cov": round(cov, 3)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_k]


# =====================================================================
#  检索
# =====================================================================
def _label(it, sid):
    lv = it["level"]
    if lv == "短期记忆":
        return "本对话短期" if (sid and it["sid"] == sid) else "其他对话短期"
    return lv


def search(query: str, sid: str = "", top_k: int = 5,
           min_vec: float = MIN_VEC, min_cov: float = MIN_COV,
           with_transcripts: bool = True) -> list:
    """条目级检索。返回命中的**条目**（不是整块正文），无关时返回空列表。"""
    q = (query or "").strip()
    if not q:
        return []
    st = sync()
    items, mat = st["items"], st["mat"]
    if not items:
        return []

    vec_scores = None
    if mat is not None:
        qv = embed.embed_texts([q], is_query=True)
        if qv is not None and qv.ndim == 2 and qv.shape[1] == mat.shape[1]:
            vec_scores = (mat @ qv[0]).astype(float).tolist()

    out = []
    for i, it in enumerate(items):
        cov = _coverage(q, it["text"])
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
    seen, res = set(), []
    for h in out:
        k = h["content"][:80]
        if k in seen:
            continue
        seen.add(k)
        res.append(h)

    if with_transcripts and len(res) < top_k:
        try:
            for h in _search_transcripts(q, top_k=min(TRANS_TOP, top_k - len(res))):
                k = h["content"][:80]
                if k in seen:
                    continue
                seen.add(k)
                res.append(h)
        except Exception as e:
            _LOG.warning("历史会话检索失败：%s", e)

    return res[:top_k]


def status() -> dict:
    st = sync()
    info = embed.info()
    levels = {}
    for it in st["items"]:
        levels[it["level"]] = levels.get(it["level"], 0) + 1
    d = memory.TRANSCRIPT_DIR
    tfiles, tbytes = 0, 0
    if os.path.isdir(d):
        try:
            for fn in os.listdir(d):
                if fn.endswith(".jsonl"):
                    tfiles += 1
                    tbytes += os.path.getsize(os.path.join(d, fn))
        except Exception:
            pass
    return {"items": len(st["items"]), "levels": levels,
            "indexed": st["mat"] is not None,
            "backend": info.get("backend", ""), "model": info.get("model", ""),
            "dim": info.get("dim", 0), "ready": bool(info.get("ready")),
            "error": info.get("error", ""),
            "transcript_files": tfiles,
            "transcript_mb": round(tbytes / 1048576.0, 1)}


def rebuild() -> dict:
    """强制全量重建（换模型、或怀疑索引坏了时用）。"""
    with _lock:
        _idx.update({"sig": "", "dsig": "", "items": [], "mat": None})
        _trans.update({"sig": "", "items": []})
        for p in (VEC_FILE, META_FILE):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    t0 = time.time()
    st = sync(force=True)
    info = embed.info()
    return {"items": len(st["items"]), "indexed": st["mat"] is not None,
            "ms": int((time.time() - t0) * 1000),
            "backend": info.get("backend", ""), "model": info.get("model", "")}
