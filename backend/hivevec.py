# -*- coding: utf-8 -*-
"""群文件库的**块级向量索引** —— 让 AI 在群里"按意思"找到文件里的内容。

为什么需要这一层
----------------
群文件夹里已经能存文件（`hive.artifacts_dir` / `hive.files_dir`），但
**没有任何检索能力**：模型想知道"群里那份报价单里单价是多少"，
只能把每个文件从头读到尾（几十份文件 = 几十万 token，根本塞不进 context）。
用户 2026-11-15 的要求是「群文件库要有向量化深化检索，提高召回率，
并支持模型进行读取与操作」。

做法（和 `kb.search` / `memvec` 同一套思路，参数按实测校准）
-----------------------------------------------------------
· **块级**：一份文件切成若干 ~520 字的块，一块一个向量。
  整篇一个向量会被长文档稀释掉（这是最初版本最大的问题）。
· **混合打分**：语义向量（主）+ 字级覆盖（兜底）。
  语义分或覆盖率**任一达标就收**，两个都不过就不返回（宁缺毋滥）。
· **增量**：按文件 (rel, size, mtime) 复用旧块向量，只给新增/改动的算；
  换了 embedding 模型（`embed.signature()` 变了）则整体重建。
· **降级**：向量模型不可用时退化为纯字级覆盖检索 ——
  **检索功能不能因为"没模型"整个挂掉**。

为什么用字级覆盖而不是 2-gram
-----------------------------
实测踩过（见 `memvec.py` 的说明）：2-gram 会被断词切碎，
「哪个学」「校读书」都匹配不上「学校」，明明相关的条目召回不到。
字级覆盖只看"查询里的字有多少出现在块里"，对中文更稳。
"""
import os
import re
import json
import time
import logging
import threading

import numpy as np

from . import embed
from . import hive
from . import doc_extract
logger = logging.getLogger(__name__)

_LOG = logging.getLogger("uvicorn.error")

CHUNK = 520                 # 每块目标字数
OVERLAP = 90                # 相邻块重叠，避免答案正好被切断
MAX_CHUNKS = 4000           # 单群块数上限（防止大群索引爆炸）
MAX_FILE_BYTES = 8 << 20    # 单文件超过 8MB 不索引（抽文本也很慢）
MIN_CHUNK_CHARS = 12        # 太短的块没意义（页码、单字符）

# 阈值：和 kb.py 同一套实测值（bge-small-zh：相关 0.45~0.78，无关 0.15~0.35）
MIN_VEC = 0.46
MIN_COV = 0.50
W_VEC, W_COV = 0.75, 0.25   # 只用于排序，不参与拦截

_VEC_NAME = "_vec.npy"
_META_NAME = "_vec_meta.json"

_locks = {}
_lock_guard = threading.Lock()


def _lock(gid: str):
    with _lock_guard:
        lk = _locks.get(gid)
        if lk is None:
            lk = threading.Lock()
            _locks[gid] = lk
        return lk


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:
        _LOG.warning("[hivevec] 写索引失败 %s：%s", path, e)
        return False


# ---------------------------------------------------------------- 收集文件
def _iter_files(gid: str):
    """列出群里所有**能抽文本**的文件。

    磁盘名是 `fid__原名`（见 hive.save_artifact），对外要还原成原名 ——
    否则模型看到的是 `f_1a2b3c__报价单.docx`，引用起来很难看。
    """
    out = []
    root = hive._gdir(gid)
    for sub in ("artifacts", "files"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in names:
            if fn.startswith(".") or fn.startswith("_"):
                continue
            # ⚠️ Word/WPS 打开文档时会在同目录生成 `~$xxx.docx` 锁文件，
            #    实测它 162 字节、无正文，却会作为一个"文件"出现在清单里。
            if fn.startswith("~$") or fn.startswith("~"):
                continue
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            show = fn.split("__", 1)[1] if "__" in fn else fn
            if show.startswith("~$"):
                continue
            if not doc_extract.is_supported(show):
                continue
            try:
                st = os.stat(p)
            except Exception:
                continue
            if st.st_size <= 0 or st.st_size > MAX_FILE_BYTES:
                continue
            out.append({"rel": sub + "/" + fn, "path": p, "name": show,
                        "size": int(st.st_size), "mtime": float(st.st_mtime)})
    return out


def _chunk_text(text: str):
    """按段落合并成 ~CHUNK 字的块，块间留 OVERLAP 字重叠。"""
    text = re.sub(r"\r\n?", "\n", str(text or ""))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(p) > CHUNK:
            if buf:
                out.append(buf)
                buf = ""
            step = max(1, CHUNK - OVERLAP)
            for i in range(0, len(p), step):
                seg = p[i:i + CHUNK]
                if seg.strip():
                    out.append(seg)
                if i + CHUNK >= len(p):
                    break
            continue
        if len(buf) + len(p) + 1 <= CHUNK:
            buf = (buf + "\n" + p) if buf else p
        else:
            out.append(buf)
            tail = buf[-OVERLAP:] if len(buf) > OVERLAP else buf
            buf = (tail + "\n" + p) if tail else p
    if buf:
        out.append(buf)
    return [c for c in out if len(c.strip()) >= MIN_CHUNK_CHARS]


# ---------------------------------------------------------------- 字级覆盖
_STOP = set("的了吗呢吧啊呀是在有和与及对把被从到就都很也还要会能可以这那个我你他她它们什么怎么如何为哪什")


def _chars(s):
    return [c for c in str(s or "")
            if ("\u4e00" <= c <= "\u9fff") or c.isalnum()]


def _coverage(qchars, text: str) -> float:
    if not qchars:
        return 0.0
    qs = [c for c in qchars if c not in _STOP] or list(qchars)
    ts = set(_chars(text))
    if not ts:
        return 0.0
    hit = sum(1 for c in qs if c in ts)
    return hit / float(len(qs))


def _grams(s: str, n: int = 2):
    """查询的 n-gram（只保留中英文数字）。"""
    t = "".join(_chars(s))
    return [t[i:i + n] for i in range(len(t) - n + 1)] if len(t) >= n else []


def _gram_hits(qgrams, text: str) -> int:
    """有多少个 n-gram 真的出现在这段文字里。

    ⚠️ 为什么需要它（2026-11-15 实测）：查「今天天气怎么样」，
    单字覆盖率能到 0.60（"天""气""样"这些字文档里恰好都有），
    于是被当成命中返回 —— 但它跟文档毫无关系。
    而这条查询的 2-gram（今天/天气/怎么/么样）**在全文里一个都没有**。
    所以「覆盖率达标」再叠一条「至少有 1 个 2-gram 命中」，
    闲聊查询就挡得干净，而真正的换说法查询（「HR 要变成什么角色」→ 命中「角色」）照常放行。
    """
    if not qgrams:
        return 0
    ts = "".join(_chars(text))
    if not ts:
        return 0
    return sum(1 for g in qgrams if g in ts)


# ---------------------------------------------------------------- 建索引
def build_index(gid: str, force: bool = False) -> dict:
    """重建/增量更新某个群的索引。

    返回 `{ok, files, chunks, reused, embedded, vec, ms, error}`。
    """
    gid = str(gid or "")
    if not gid:
        return {"ok": False, "error": "缺少 gid"}
    t0 = time.time()
    with _lock(gid):
        root = hive._gdir(gid)
        try:
            os.makedirs(root, exist_ok=True)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        meta_p = os.path.join(root, _META_NAME)
        vec_p = os.path.join(root, _VEC_NAME)
        old = _read_json(meta_p, {}) or {}
        sig = embed.signature()
        old_chunks = old.get("chunks") or []
        old_vec = None
        if (not force) and sig == old.get("sig") and os.path.exists(vec_p):
            try:
                arr = np.load(vec_p)
                if arr.ndim == 2 and arr.shape[0] == len(old_chunks):
                    old_vec = arr
            except Exception:
                old_vec = None

        # 按文件分组旧块（便于整文件复用）
        by_file = {}
        if old_vec is not None:
            for i, c in enumerate(old_chunks):
                if not isinstance(c, dict):
                    continue
                by_file.setdefault(str(c.get("rel") or ""), []).append((c, old_vec[i]))

        files = _iter_files(gid)
        chunks, vecs, meta_files = [], [], []
        reused = 0
        to_embed = []          # [(chunk_index, text)]
        for f in files:
            key = (f["rel"], f["size"], round(f["mtime"], 3))
            reuse = by_file.get(f["rel"]) or []
            ok_reuse = bool(reuse) and all(
                (c.get("size"), round(float(c.get("mtime") or 0), 3)) == (key[1], key[2])
                for c, _v in reuse)
            if ok_reuse:
                for c, v in reuse:
                    if len(chunks) >= MAX_CHUNKS:
                        break
                    chunks.append(dict(c))
                    vecs.append(v)
                reused += 1
                meta_files.append({"rel": f["rel"], "name": f["name"],
                                   "size": f["size"], "mtime": f["mtime"],
                                   "chunks": len(reuse), "note": "复用"})
                continue

            try:
                txt, note = doc_extract.extract_text(f["path"], limit=400_000)
            except Exception as e:
                txt, note = "", "解析异常：%s" % e
            if not (txt or "").strip():
                meta_files.append({"rel": f["rel"], "name": f["name"],
                                   "size": f["size"], "mtime": f["mtime"],
                                   "chunks": 0, "note": note or "无文本"})
                continue
            pieces = _chunk_text(txt)
            n = 0
            for piece in pieces:
                if len(chunks) >= MAX_CHUNKS:
                    break
                chunks.append({"rel": f["rel"], "name": f["name"],
                               "size": f["size"], "mtime": f["mtime"],
                               "idx": n, "text": piece})
                to_embed.append((len(chunks) - 1, piece))
                vecs.append(None)
                n += 1
            meta_files.append({"rel": f["rel"], "name": f["name"],
                               "size": f["size"], "mtime": f["mtime"],
                               "chunks": n, "note": note or ""})

        embedded = 0
        if to_embed:
            texts = [t for _i, t in to_embed]
            mat = embed.embed_texts(texts)
            if mat is not None and getattr(mat, "shape", (0,))[0] == len(texts):
                for k, (ci, _t) in enumerate(to_embed):
                    vecs[ci] = mat[k]
                embedded = len(texts)

        # 向量矩阵：没算出来的块填 0（检索时语义分按 0 处理，靠覆盖率兜底）
        if chunks and all(v is not None for v in vecs):
            mat = np.asarray(vecs, dtype=np.float32)
        elif chunks:
            dim = 0
            for v in vecs:
                if v is not None:
                    dim = len(v)
                    break
            mat = np.zeros((len(chunks), max(1, dim)), dtype=np.float32)
            for i, v in enumerate(vecs):
                if v is not None:
                    mat[i] = v
        else:
            mat = np.zeros((0, 0), dtype=np.float32)

        try:
            if mat.size:
                np.save(vec_p, mat)
            elif os.path.exists(vec_p):
                os.remove(vec_p)
        except Exception as e:
            _LOG.warning("[hivevec] 保存向量失败：%s", e)

        _write_json(meta_p, {
            "gid": gid, "sig": sig, "ts": time.time(),
            "files": meta_files, "chunks": chunks,
            "dim": int(mat.shape[1]) if mat.ndim == 2 else 0,
        })
        return {"ok": True, "files": len(files), "chunks": len(chunks),
                "reused": reused, "embedded": embedded,
                "vec": sig, "ms": int((time.time() - t0) * 1000)}


def ensure_index(gid: str, max_age: float = 30.0) -> dict:
    """没有索引或太旧才重建（30 秒内复用，避免每次检索都扫盘）。"""
    meta_p = os.path.join(hive._gdir(str(gid)), _META_NAME)
    meta = _read_json(meta_p, {}) or {}
    if meta.get("sig") == embed.signature() and (time.time() - float(meta.get("ts") or 0) < max_age):
        return {"ok": True, "skipped": True,
                "chunks": len(meta.get("chunks") or [])}
    return build_index(gid)


# ---------------------------------------------------------------- 检索
def search(gid: str, query: str, top_k: int = 6, min_vec: float = None,
           min_cov: float = None) -> list:
    """混合检索群文件里的片段。

    返回 `[{name, rel, chunk, score, vec_score, kw_score, content}]`。
    """
    gid = str(gid or "")
    query = str(query or "").strip()
    if not gid or not query:
        return []
    ensure_index(gid)
    meta = _read_json(os.path.join(hive._gdir(gid), _META_NAME), {}) or {}
    chunks = [c for c in (meta.get("chunks") or []) if isinstance(c, dict)]
    if not chunks:
        return []
    mat = None
    try:
        arr = np.load(os.path.join(hive._gdir(gid), _VEC_NAME))
        if arr.ndim == 2 and arr.shape[0] == len(chunks):
            mat = arr
    except Exception:
        mat = None

    qchars = _chars(query)
    if not qchars:
        return []
    mv = MIN_VEC if min_vec is None else float(min_vec)
    mc = MIN_COV if min_cov is None else float(min_cov)

    cov = np.asarray([_coverage(qchars, c.get("text") or "") for c in chunks],
                     dtype=np.float32)
    qgrams = _grams(query)
    gram = np.asarray([_gram_hits(qgrams, c.get("text") or "") for c in chunks],
                      dtype=np.int32)
    vec = None
    if mat is not None:
        qv = embed.embed_texts([query], is_query=True)
        if qv is not None and getattr(qv, "shape", (0,))[0] == 1:
            dim = mat.shape[1]
            if len(qv[0]) == dim:
                vec = mat @ qv[0]

    if vec is not None:
        final = W_VEC * vec + W_COV * cov
    else:
        final = cov.copy()

    picked = []
    for i in range(len(chunks)):
        v = float(vec[i]) if vec is not None else 0.0
        c = float(cov[i])
        ok = False
        if vec is not None and v >= mv:
            ok = True                       # 语义强信号，直接收
        elif c >= mc:
            # 只靠覆盖率时要防"闲聊词恰好都在文档里"：
            # 至少有一个 2-gram 真的命中，否则要求覆盖率非常高（0.75）才放行。
            ok = (int(gram[i]) > 0) or (c >= 0.75)
        if not ok:
            continue
        picked.append((float(final[i]), i, v, c))
    picked.sort(key=lambda x: x[0], reverse=True)

    # 同一文件最多占一半名额（否则一份长文档的相邻块把别的文件全挤掉）
    cand_files = len({chunks[i].get("rel") for _f, i, _v, _c in picked})
    per_file = int(top_k) if cand_files <= 1 else max(2, int(top_k) // 2)
    used, seen, out = {}, [], []
    for f, i, v, c in picked:
        ch = chunks[i]
        rel = str(ch.get("rel") or "")
        if used.get(rel, 0) >= per_file:
            continue
        txt = str(ch.get("text") or "")
        # 相邻块有重叠，top_k 小时会把同一段话返回两遍
        if any(txt[:100] and (txt[:100] in s or s[:100] in txt) for s in seen):
            continue
        used[rel] = used.get(rel, 0) + 1
        seen.append(txt)
        out.append({"name": ch.get("name") or os.path.basename(rel),
                    "rel": rel, "chunk": ch.get("idx"),
                    "score": round(f, 4), "vec_score": round(v, 4),
                    "kw_score": round(c, 4), "content": txt})
        if len(out) >= int(top_k):
            break
    return out


def status(gid: str) -> dict:
    meta = _read_json(os.path.join(hive._gdir(str(gid)), _META_NAME), {}) or {}
    files = [f for f in (meta.get("files") or []) if isinstance(f, dict)]
    return {"gid": gid, "chunks": len(meta.get("chunks") or []),
            "files": len(files),
            "indexed_files": sum(1 for f in files if int(f.get("chunks") or 0) > 0),
            "dim": int(meta.get("dim") or 0),
            "sig": meta.get("sig") or "",
            "ts": float(meta.get("ts") or 0),
            "vec_ready": embed.available(),
            "embed": embed.info()}


def read_text(gid: str, name: str, limit: int = 60000) -> dict:
    """按文件名读全文（模型"深度干预读取"用）。重名取最新修改的那份。"""
    gid = str(gid or "")
    want = os.path.basename(str(name or "").replace("\\", "/"))
    if not want:
        return {"ok": False, "error": "缺少文件名"}
    cands = [f for f in _iter_files(gid)
             if f["name"] == want or f["rel"].endswith("/" + want)]
    if not cands:
        # 模糊匹配：包含即可（用户常只记得关键词）
        cands = [f for f in _iter_files(gid) if want in f["name"]]
    if not cands:
        return {"ok": False, "error": "群里没有这个文件：%s" % want}
    f = sorted(cands, key=lambda x: x["mtime"], reverse=True)[0]
    txt, note = doc_extract.extract_text(f["path"], limit=limit)
    return {"ok": bool(txt), "name": f["name"], "rel": f["rel"],
            "chars": len(txt), "note": note, "text": txt}


def list_files(gid: str) -> list:
    """群文件清单（带是否可检索）。"""
    out = []
    meta = _read_json(os.path.join(hive._gdir(str(gid)), _META_NAME), {}) or {}
    idx = {str(f.get("rel")): f for f in (meta.get("files") or []) if isinstance(f, dict)}
    for f in _iter_files(gid):
        m = idx.get(f["rel"]) or {}
        out.append({"name": f["name"], "rel": f["rel"], "size": f["size"],
                    "mtime": f["mtime"], "chunks": int(m.get("chunks") or 0),
                    "searchable": int(m.get("chunks") or 0) > 0})
    return out
