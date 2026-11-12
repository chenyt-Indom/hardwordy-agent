# -*- coding: utf-8 -*-
"""本地知识库 + RAG 轻量检索。

- 用户可把领域知识文档保存到本地知识库（data/kb/*.txt 或 .md）
- 采用轻量 TF-IDF（词频 + 逆文档频率）向量化 + 余弦检索，
  无需额外下载大模型，完全本地、离线、占用小。
- 前端通过 RAG 开关控制是否启用检索增强。
"""
import os
import re
import json
import math
import time
import threading
import logging

import numpy as np

from . import config
from . import embed as embed_mod

KB_DIR = config.data("data", "kb")
INDEX_FILE = os.path.join(KB_DIR, "_index.json")

STOP_WORDS = set("的 了 在 是 和 与 我 你 他 她 它 们 一 有 也 都 要 把 让 这 那 就 吗 呢 啊 吧 很 会 可以 请 并 及 或 上 下 中 的 关于 什么是 如何 怎么".split())


def _tokenize(text):
    """轻量分词：英文/数字按字词，中文按 2-gram 切分。
    中文 2-gram 可让检索匹配短语而无需额外分词库，完全本地零依赖。"""
    tokens = []
    for ch in re.findall(r"[\u4e00-\u9fa5_a-zA-Z0-9]+", text.lower()):
        # 纯中文段：2-gram
        if re.fullmatch(r"[\u4e00-\u9fa5]+", ch):
            for i in range(len(ch) - 1):
                tokens.append(ch[i:i + 2])
            # 保留单字符边界，避免 2 字词内部信息丢失
            if len(ch) == 1:
                tokens.append(ch)
        else:
            tokens.append(ch)
    return [w for w in tokens if w not in STOP_WORDS and w.strip()]


def _ensure_dir():
    os.makedirs(KB_DIR, exist_ok=True)


def ensure_dir() -> str:
    """确保知识库目录存在并放一份说明，返回路径（启动时调用）。

    用户直接把 .txt / .md 拷进这个目录就行，**不需要任何导入操作** ——
    检索时 `_build_index` 会按目录实际内容重建索引，放进去就能被查到。
    """
    _ensure_dir()
    readme = os.path.join(KB_DIR, "_说明.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write("把领域资料放进这个文件夹即可被检索到，不需要重新导入。\n"
                        "支持任意格式（txt / md / pdf / docx / xlsx / pptx…），\n"
                        "也支持子文件夹 —— 按课程、按项目分门别类放，检索时照样找得到。\n"
                        "以 _ 开头的文件或文件夹会被忽略（说明和索引就放在这类名字下）。\n")
        except Exception:
            pass
    return KB_DIR


def _rel(path: str) -> str:
    """取相对知识库根的路径（子文件夹用 / 分隔，跨平台一致）。"""
    return os.path.relpath(path, KB_DIR).replace(os.sep, "/")


def _is_hidden(rel: str) -> bool:
    """任意一段以 _ 开头就跳过（说明文件、索引、用户想忽略的东西）。"""
    return any(p.startswith("_") for p in rel.split("/"))


def _safe_rel(name: str) -> str:
    """把名字收敛成**库内**的安全相对路径（支持子文件夹）。

    和生成文库同一套规则：挡 `..`、挡盘符、非法字符替换成 _。
    盘符要**明确拒绝**而不是悄悄剥掉 —— 否则 `D:/笔记.md` 会变成库里的
    `笔记.md`，用户以为写到了 D 盘（生成文库那边踩过这个坑）。
    """
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("文件名不能为空")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError("不要写盘符（如 D:）—— 只能填知识库目录内的相对路径")
    raw = raw.lstrip("/")
    parts = []
    for p in raw.split("/"):
        p = p.strip()
        if not p or p == ".":
            continue
        if p == "..":
            raise ValueError("路径里不允许出现 ..")
        parts.append(re.sub(r'[<>:"|?*\x00-\x1f]', "_", p))
    if not parts:
        raise ValueError("文件名不合法")
    rel = "/".join(parts)
    full = os.path.abspath(os.path.join(KB_DIR, rel))
    root = os.path.abspath(KB_DIR) + os.sep
    if not full.startswith(root):
        raise ValueError("路径越界（只能写在知识库目录里）")
    return rel


def _iter_docs():
    """递归遍历知识库，产出 (rel, 绝对路径)。

    ⚠️ 遍历只留这一处。原来是"列目录"和"建索引"各写一遍 os.listdir ——
    加子目录支持时最容易只改一处，结果列表里有、检索里没有（或反过来）。
    """
    _ensure_dir()
    for root, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for fn in files:
            if fn.startswith("_") or fn.endswith(".json"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, KB_DIR).replace(os.sep, "/")
            if _is_hidden(rel):
                continue
            yield rel, full


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_num(text: str) -> str:
    """把中文数字换成阿拉伯数字，**只用于排序**（不改变显示）。

    「第一章」→「第1章」。不这么做的话，按拼音排出来是
    二、九、六、七、三、十、四、五、一 —— 用户完全看不懂这个顺序。
    """
    def conv(m):
        t = m.group(0)
        if len(t) == 1:
            if t == "十":
                return "10"                 # 单字「十」是个坑：不特判就漏掉
            return str(_CN_DIGITS.get(t, t))
        if "十" in t:                       # 十一 / 二十 / 二十三
            parts = t.split("十")
            head = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
            tail = _CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return str(head * 10 + tail)
        return "".join(str(_CN_DIGITS.get(c, c)) for c in t)

    return re.sub(r"[零〇一二两三四五六七八九十]+", conv, text)


def _sort_key(rel: str):
    """目录排序键：中文数字转阿拉伯 + 所有数字零填充。

    零填充是必须的：排序是按字符串比的，"第10章" < "第2章"
    （'1' < '2'），必须变成 0010 vs 0002 才是人期待的顺序。
    """
    out = []
    for part in rel.split("/"):
        t = _cn_to_num(part)
        t = re.sub(r"\d+", lambda m: m.group(0).zfill(4), t)
        out.append(t)
    return "/".join(out)


def _doc_id(path):
    """doc_id = 相对路径去掉扩展名（支持子文件夹，如 `课程/讲义1`）。

    用相对路径而不是 basename：子文件夹里出现同名文件时
    （`第一章/讲义.pdf` 和 `第二章/讲义.pdf`），basename 会在索引里
    **互相覆盖**，最后只有一个能被检索到 —— 另一个等于白放。
    """
    return os.path.splitext(_rel(path))[0]


def find_doc_path(doc_id: str) -> str:
    """按 doc_id（不含扩展名）找到实际文件路径。

    ⚠️ 以前检索里写死了 `did + ".txt"`，找不到再试 `".md"` ——
    PDF / Word 文档两个都试不中，取回空文本，于是就"检索不到"。
    """
    want = str(doc_id or "").strip().replace("\\", "/").lstrip("/")
    stem = os.path.splitext(want)[0]          # 允许传带扩展名的名字
    for rel, full in _iter_docs():
        if os.path.splitext(rel)[0] == stem:
            return full
    return ""


# 文档内容缓存：{文件名: (mtime, 文本)}
# 解析 PDF/docx 比读 txt 贵得多，而检索每次都要遍历全部文档 ——
# 没有缓存的话每问一句都要把所有 PDF 重解析一遍，慢到不可用。
_text_cache: dict = {}


def read_doc(path: str) -> tuple:
    """读取文档正文，返回 (文本, 说明)。

    ⚠️ 以前这里是 `open(path, encoding="utf-8").read()` ——
    遇到 PDF / Word 会直接抛 UnicodeDecodeError，
    连累 list_documents 整个崩掉，用户放进去的文档"检索不到"。
    现在统一走 doc_extract，支持 txt/md/pdf/docx/xlsx/pptx。
    """
    from . import doc_extract
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0
    key = os.path.abspath(path)
    hit = _text_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    text, note = doc_extract.extract_text(path)
    _text_cache[key] = (mtime, text, note)
    return text, note


def _read(path):
    """兼容旧调用：只要文本。"""
    return read_doc(path)[0]


# ---------- 文档管理 ----------
def list_documents():
    """列出知识库文档。

    读不出文字的（例如旧版 .doc/.wps）也会列出来，但带上 note 说明原因 ——
    让用户知道"文件在，是格式读不了"，而不是"我明明放进去了怎么没有"。
    """
    _ensure_dir()
    docs = []
    for rel, path in _iter_docs():
        text, note = read_doc(path)
        ext = os.path.splitext(rel)[1].lower().lstrip(".")
        docs.append({
            "id": _doc_id(path),
            "rel": rel,                                   # 相对路径（含子文件夹）
            "folder": os.path.dirname(rel),               # "" = 根目录
            "filename": os.path.basename(rel),
            "ext": ext,
            "size": os.path.getsize(path),
            "chars": len(text),
            "indexed": bool(text.strip()),
            "note": note,
            "updated_at": os.path.getmtime(path),
        })
    # 能检索的排前面；再按"转数字后的路径"排 —— 同层内就是自然顺序
    # （第一章 < 第二章 < 第十章），前端直接照这个顺序分组显示
    docs.sort(key=lambda d: (not d["indexed"], _sort_key(d["rel"])))
    return docs


def save_document(name: str, content: str) -> dict:
    """保存一份**纯文本**知识文档（界面上粘贴文字用的）。"""
    _ensure_dir()
    # 名字为空才用默认名；名字**不合法**（含 ..、盘符）要直接报错 ——
    # 静默回退成 doc_<时间戳>.txt 的话，用户以为存下了、回头根本找不到。
    # 注意：字符串可能本来就是 str，不用考虑 None
    if not str(name or "").strip():
        rel = f"doc_{int(time.time())}.txt"
    else:
        rel = _safe_rel(name)
    if not rel.lower().endswith((".txt", ".md")):
        rel += ".txt"
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path) or KB_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"id": _doc_id(path), "rel": rel, "filename": os.path.basename(rel),
            "size": len(content), "updated_at": os.path.getmtime(path)}


def save_bytes(name: str, data: bytes) -> dict:
    """保存**任意格式**的文档（PDF / Word / Excel …）到知识库。

    必须按二进制原样落盘 —— 之前上传接口是
    `(await file.read()).decode("utf-8", errors="replace")`，
    这会把 PDF 这类二进制文件**解码坏掉**再存进去，存进去就是垃圾。
    原始字节存好，读取时交给 doc_extract 解析。
    """
    _ensure_dir()
    if not str(name or "").strip():
        rel = f"doc_{int(time.time())}.txt"
    else:
        rel = _safe_rel(name)          # 非法名直接抛错，不静默改名
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path) or KB_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    _text_cache.pop(os.path.abspath(path), None)   # 覆盖了旧文件要清缓存
    text, note = read_doc(path)
    return {"id": _doc_id(path), "rel": rel, "filename": os.path.basename(rel),
            "size": len(data),
            "chars": len(text), "indexed": bool(text.strip()), "note": note,
            "updated_at": os.path.getmtime(path)}


def delete_document(name: str) -> bool:
    """删除文档。name 可以是相对路径（含子文件夹），也可以只是文件名。

    只给文件名、而子文件夹里存在**多个同名文件**时**拒绝删除**并返回 False ——
    猜错就删掉别人一份资料，代价太大，宁可让调用方给全路径。
    """
    _ensure_dir()
    try:
        rel = _safe_rel(name)
    except ValueError:
        return False
    path = os.path.join(KB_DIR, rel.replace("/", os.sep))
    if os.path.isfile(path):
        return _remove_file(path)
    # 兜底：只给了文件名时，在子文件夹里找；有歧义就不动
    base = os.path.basename(rel)
    hits = [full for r, full in _iter_docs() if os.path.basename(r) == base]
    if len(hits) == 1:
        return _remove_file(hits[0])
    if not hits:
        # **一个都没找到 → 文件本来就不在，算成功**。
        # 实测踩过：用户先在资源管理器里把文件删了，界面上还留着旧列表，
        # 再点「删除」→ 原来是 404「文档不存在」，用户看着莫名其妙
        # （"我就是要删它，它没了不正是我要的结果吗"）。
        # 删除是**幂等**的：目标状态"这个文件不存在"已经达到，就该算成功。
        return True
    # 多个同名文件：猜错就删掉别人一份资料，宁可让调用方给全路径
    return False


def _remove_file(path: str) -> bool:
    """删掉一个知识库文件；**文件本来就不在也算成功**。

    ⚠️ 这里必须容错 —— 实测踩过（日志里那条 FileNotFoundError 就是它）：
      ① 用户**先在资源管理器里把文件删了**，再回应用界面点「删除」→
         `os.remove` 抛 FileNotFoundError → 整个 /api/kb/delete 返回 500。
         用户看到"删除失败"，可文件明明已经没了，只会一头雾水。
      ② 本机还有一层"删除走回收站"的拦截，回收站环节失败也会抛别的异常。

    删除应当是**幂等**的：目标状态是"这个文件不存在"，它已经达到了，就该算成功。
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True            # 本来就不在了 = 用户的目的已达成
    except Exception:
        logging.getLogger("uvicorn.error").warning(
            "删除知识库文档失败：%s", path, exc_info=True)
        return False


# ---------- 分块 ----------
# 块大小的取舍：太小 → 语义不完整、块数暴增；太大 → 又回到"整篇稀释"。
# 400 字左右是中文检索的常用甜点区，配合 70 字重叠避免答案被切在边界上。
CHUNK_TARGET = 420
CHUNK_MIN = 180
CHUNK_MAX = 720
CHUNK_OVERLAP = 70

# 混合检索权重：向量为主（语义泛化），关键词为辅（专有名词/编号/数字更准）
VEC_WEIGHT = 0.65
KW_WEIGHT = 0.35

# 阈值：低于这个分的不返回（用户要求"分数太低的不给"）。
# 这几个数是**实测校准**出来的，不是拍脑袋：bge-small-zh-v1.5 下
# 真正相关的块余弦 0.52~0.66，无关块 0.21~0.26，但"沾一点边"的块能到 0.41~0.43，
# 所以 MIN_VEC 必须取到 0.45，否则问「介绍人」会把毫不相干的段落也带出来。
MIN_VEC = 0.45          # 纯语义相似度下限
MIN_COV = 0.50          # 查询词覆盖率下限（专有名词/编号靠它）
MIN_FINAL = 0.30        # 融合分下限

_INDEX_META = os.path.join(KB_DIR, "_vec_meta.json")
_INDEX_VEC = os.path.join(KB_DIR, "_vec.npy")
_INDEX_VERSION = 2


def _split_long(par: str, limit: int):
    """把超长段落按句末标点切成 <= limit 的片段。"""
    out, buf = [], ""
    for sent in re.split(r"(?<=[。！？；!?;\n])", par):
        if not sent:
            continue
        if len(buf) + len(sent) > limit and buf:
            out.append(buf)
            buf = sent
        else:
            buf += sent
        # 单句本身就超长（没有标点的长串）时硬切
        while len(buf) > limit:
            out.append(buf[:limit])
            buf = buf[limit:]
    if buf.strip():
        out.append(buf)
    return out


def _chunk_text(text: str):
    """把一篇文档切成检索用的块。

    策略：语义边界优先 —— 先按空行 / Markdown 标题切段落，
    段落太短就往后合并、太长就按句号再切，最后相邻块保留 70 字重叠。

    ⚠️ 这是本次改造的核心。原来是**整篇文档一个向量**：
    34.5 万字的 PDF 里某一段的匹配，会被整篇的平均词频稀释到接近 0，
    于是"文件明明在库里、就是检索不出来"。切成 400 字左右的块之后，
    每个块只跟"这一段"比，召回率的提升是数量级的。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    paras, buf = [], ""
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            if buf.strip():
                paras.append(buf)
            buf = ""
            continue
        # 标题行天然是语义边界，前面攒的内容先结块
        if re.match(r"^#{1,6}\s", s) and buf.strip():
            paras.append(buf)
            buf = s + "\n"
            continue
        buf += line + "\n"
    if buf.strip():
        paras.append(buf)

    merged, cur = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > CHUNK_MAX:
            if cur:
                merged.append(cur)
                cur = ""
            merged.extend(_split_long(p, CHUNK_TARGET))
            continue
        if len(cur) + len(p) + 1 <= CHUNK_TARGET:
            cur = (cur + "\n" + p) if cur else p
        else:
            if cur:
                merged.append(cur)
            cur = p
    if cur:
        merged.append(cur)

    # 末尾单句成块检索价值低，还占 top_k 名额，并进前一块
    chunks = []
    for c in merged:
        if chunks and len(c) < CHUNK_MIN // 2:
            chunks[-1] = chunks[-1] + "\n" + c
        else:
            chunks.append(c)

    out, prev_tail = [], ""
    for c in chunks:
        out.append(((prev_tail + "\n" + c).strip() if prev_tail else c))
        prev_tail = c[-CHUNK_OVERLAP:] if len(c) > CHUNK_OVERLAP else c
    return out


# ---------- 索引（分块 + 向量 + BM25 倒排）----------
# 结构：
#   files   {doc_id: {"mtime":..., "size":..., "n": 块数, "rel":...}}
#   chunks  [{"doc_id","rel","idx","text"}]
#   vectors np.float32 (N, dim) —— 已 L2 归一化，余弦即点积
#   tokens  [list[str]] —— 仅内存，用于 BM25，不落盘（能从 text 重算）
_idx = {"sig": "", "files": {}, "chunks": [], "vectors": None, "tokens": []}
_idx_lock = threading.RLock()
_idx_state = {"building": False, "last_ms": 0, "last_n": 0, "error": ""}


def _doc_scan():
    """扫描当前文档，返回 [(doc_id, rel, path, mtime, size)]。"""
    out = []
    for rel, path in _iter_docs():
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append((_doc_id(path), rel, path, st.st_mtime, st.st_size))
    return out


def _load_index():
    """从磁盘载入索引；签名不匹配（换模型/换维度）就当作没有。"""
    global _idx
    sig = embed_mod.signature()
    try:
        with open(_INDEX_META, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = None
    if not meta or meta.get("version") != _INDEX_VERSION:
        return False
    # ⚠️ 模型换了（维度不同）必须重建 —— 否则余弦拿两个不同空间的向量算，
    # 结果是错的，而且**不会报错**，最难查
    if str(meta.get("sig")) != str(sig):
        return False
    vec_path = meta.get("vec_file") or _INDEX_VEC
    try:
        vectors = np.load(vec_path).astype(np.float32)
    except Exception:
        return False
    chunks = meta.get("chunks") or []
    if vectors.ndim != 2 or vectors.shape[0] != len(chunks):
        return False
    _idx = {"sig": sig, "files": meta.get("files") or {}, "chunks": chunks,
            "vectors": vectors, "tokens": [_tokenize(c.get("text", "")) for c in chunks]}
    return True


def _save_index():
    _ensure_dir()
    with _idx_lock:
        vectors = _idx.get("vectors")
        if vectors is None:
            vectors = np.zeros((0, 0), dtype=np.float32)
        # .npy 单独存：向量是二进制，塞进 json 会膨胀十几倍
        tmp_vec = _INDEX_VEC + ".tmp.npy"
        np.save(tmp_vec, vectors)
        meta = {"version": _INDEX_VERSION, "sig": _idx.get("sig", ""),
                "dim": int(vectors.shape[1]) if vectors.ndim == 2 else 0,
                "files": _idx.get("files", {}), "chunks": _idx.get("chunks", []),
                "vec_file": _INDEX_VEC, "built_at": time.time()}
        tmp_meta = _INDEX_META + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp_vec, _INDEX_VEC)
        os.replace(tmp_meta, _INDEX_META)


def _embed_chunks(texts):
    """把块文本向量化；后端不可用时返回 None。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    return embed_mod.embed_texts(texts, is_query=False)


def _build_index(force: bool = False):
    """构建/增量更新索引。返回统计 dict。

    增量规则：文档的 mtime + size 都没变，就**原样复用**它的块和向量；
    只有变过的文档才重新分块 + 向量化。这样往库里加一篇新文档时，
    不需要把已有几百块全部重算一遍。
    """
    global _idx
    with _idx_lock:
        if _idx_state.get("building"):
            return {"ok": False, "error": "索引正在构建中"}
        _idx_state["building"] = True
    t0 = time.time()
    try:
        sig = embed_mod.signature()
        with _idx_lock:
            if force or not _idx.get("chunks") and not _idx.get("files"):
                _idx = {"sig": sig, "files": {}, "chunks": [], "vectors": None, "tokens": []}
            elif _idx.get("sig") != sig:
                _idx = {"sig": sig, "files": {}, "chunks": [], "vectors": None, "tokens": []}

        scan = _doc_scan()
        old_files = dict(_idx.get("files") or {})
        old_chunks = list(_idx.get("chunks") or [])
        old_vec = _idx.get("vectors")

        # 按文档把旧块分组（保持顺序，便于复用）
        old_by_doc = {}
        if old_vec is not None and len(old_chunks) == len(old_vec):
            for i, c in enumerate(old_chunks):
                old_by_doc.setdefault(c.get("doc_id"), []).append((c, old_vec[i]))

        new_files, new_chunks, new_vecs, need = {}, [], [], []
        for did, rel, path, mtime, size in scan:
            rec = old_files.get(did)
            reused = False
            if (not force and rec and rec.get("mtime") == mtime
                    and rec.get("size") == size and did in old_by_doc):
                pairs = old_by_doc[did]
                if len(pairs) == int(rec.get("n") or 0):
                    for c, v in pairs:
                        new_chunks.append(c)
                        new_vecs.append(v)
                    new_files[did] = rec
                    reused = True
            if reused:
                continue
            text, _note = read_doc(path)
            pieces = _chunk_text(text)
            if not pieces:
                continue
            base = len(new_chunks)
            for i, p in enumerate(pieces):
                new_chunks.append({"doc_id": did, "rel": rel, "idx": i, "text": p})
                new_vecs.append(None)          # 占位，待向量化
            new_files[did] = {"mtime": mtime, "size": size, "n": len(pieces), "rel": rel}
            need.append((base, len(pieces)))

        # 向量化：只算新块
        if need:
            pending = []
            for base, n in need:
                pending.extend(range(base, base + n))
            vecs = _embed_chunks([new_chunks[i]["text"] for i in pending])
            if vecs is None:
                # 没有向量后端：索引照样建（关键词检索可用），但不落盘向量
                for i in pending:
                    new_vecs[i] = None
                _idx_state["error"] = embed_mod.info().get("error") or "向量后端不可用"
            else:
                for k, i in enumerate(pending):
                    new_vecs[i] = vecs[k]

        # 组装：有向量的块留在索引里；整库都没有向量时退化为纯关键词模式
        has_vec = any(v is not None for v in new_vecs)
        if has_vec and new_vecs:
            keep = [i for i, v in enumerate(new_vecs) if v is not None]
            chunks = [new_chunks[i] for i in keep]
            vectors = np.asarray([new_vecs[i] for i in keep], dtype=np.float32)
        else:
            chunks = new_chunks
            vectors = None

        with _idx_lock:
            _idx = {"sig": sig, "files": new_files, "chunks": chunks,
                    "vectors": vectors,
                    "tokens": [_tokenize(c.get("text", "")) for c in chunks]}
        if vectors is not None:
            _save_index()
        _idx_state["last_ms"] = int((time.time() - t0) * 1000)
        _idx_state["last_n"] = len(chunks)
        _idx_state["error"] = "" if vectors is not None else _idx_state.get("error", "")
        return {"ok": True, "chunks": len(chunks), "docs": len(new_files),
                "embedded": vectors is not None,
                "ms": _idx_state["last_ms"], "sig": sig,
                "note": "" if vectors is not None else
                        ("向量后端不可用，已退化为关键词检索：" +
                         (embed_mod.info().get("error") or ""))}
    except Exception as e:
        logging.getLogger("uvicorn.error").warning("构建知识库索引失败", exc_info=True)
        _idx_state["error"] = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        _idx_state["building"] = False


def ensure_index():
    """确保索引可用（首次检索时自动构建）。"""
    with _idx_lock:
        ready = bool(_idx.get("chunks")) or bool(_idx.get("files"))
        vectors = _idx.get("vectors")
    if not ready:
        _load_index()
    with _idx_lock:
        if not _idx.get("chunks"):
            return _build_index()
        # 库里新增/删除了文档时也要重建（增量，很快）
        if vectors is None:
            return _build_index()
    return {"ok": True, "chunks": len(_idx.get("chunks") or []),
            "docs": len(_idx.get("files") or {}), "embedded": True, "ms": 0}


def reindex(force: bool = True):
    """手动重建索引（界面按钮用）。"""
    if force:
        embed_mod.probe(force=True)
    return _build_index(force=force)


def index_status():
    """索引状态（前端显示用）。"""
    with _idx_lock:
        chunks = len(_idx.get("chunks") or [])
        vectors = _idx.get("vectors")
        files = len(_idx.get("files") or {})
        sig = _idx.get("sig", "")
    info = embed_mod.info()
    return {"chunks": chunks, "docs": files, "embedded": vectors is not None,
            "dim": int(vectors.shape[1]) if vectors is not None and vectors.ndim == 2 else 0,
            "sig": sig, "backend": info.get("backend"), "model": info.get("model"),
            "ready": bool(info.get("ready")), "error": info.get("error") or _idx_state.get("error", ""),
            "building": bool(_idx_state.get("building")),
            "last_ms": _idx_state.get("last_ms", 0),
            "hint": "" if info.get("ready") else embed_mod.install_hint()}


def _build_postings(tokens_list):
    """BM25 倒排：{term: [(chunk_i, tf)]} + 各块长度。"""
    post, lens = {}, []
    for i, toks in enumerate(tokens_list):
        lens.append(len(toks))
        cnt = {}
        for t in toks:
            cnt[t] = cnt.get(t, 0) + 1
        for t, f in cnt.items():
            post.setdefault(t, []).append((i, f))
    return post, lens


def _bm25(q_tokens, tokens_list, post=None, lens=None, k1=1.5, b=0.75):
    """标准 BM25。只遍历查询词命中的倒排链，不全库扫。"""
    n = len(tokens_list)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    if post is None:
        post, lens = _build_postings(tokens_list)
    avgdl = max(sum(lens) / max(n, 1), 1.0)
    scores = np.zeros(n, dtype=np.float32)
    for t in set(q_tokens):
        plist = post.get(t)
        if not plist:
            continue
        df = len(plist)
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for i, f in plist:
            denom = f + k1 * (1.0 - b + b * lens[i] / avgdl)
            scores[i] += idf * f * (k1 + 1.0) / denom
    return scores


def _coverage(q_tokens, n: int, post) -> np.ndarray:
    """查询词在每块里的**覆盖比例**（0~1，有绝对意义）。

    这是阈值判断的依据。为什么不用 BM25 归一化分：
    BM25 分是"相对当前查询最大值"归一化的，命中一个常见词就能拿满分，
    跟"这段真的回答了问题"没关系 —— 实测查「年龄要求」时，
    一段讲"我学的东西将来要用到什么地方去"的无关段落，因为撞上"要求"
    两个字拿了 kw=1.000 排到第一。覆盖率则要求**查询里的词真的都在**，
    命中越多分越高，才有资格当阈值。
    """
    qset = set(q_tokens)
    if not qset:
        return np.zeros(n, dtype=np.float32)
    hit = {}
    for t in qset:
        for idx, _f in post.get(t, ()):
            hit[idx] = hit.get(idx, 0) + 1
    cov = np.zeros(n, dtype=np.float32)
    for idx, h in hit.items():
        cov[idx] = h / float(len(qset))
    return cov


def search(query: str, top_k: int = 6, min_score: float = None):
    """混合检索（语义向量 + BM25），返回最相关的**文档片段**（块级）。

    召回率的提升来自三件事：
      ① **块级检索** —— 不再拿整篇文档的平均向量去比，长文档里的一段话
         不会被整篇稀释掉（这是原来最大的问题）；
      ② **语义向量** —— 「发动机」能召回「引擎」，换个说法问也不再漏；
      ③ **BM25 兜底** —— 专有名词、条款编号、数字这类语义模型反而不敏感，
         靠关键词精确匹配补回来。

    返回字段兼容旧版（doc_id / score / content / filename / rel / folder），
    另外新增 vec_score / kw_score / chunk，方便排查"为什么这条排第一"。
    """
    query = (query or "").strip()
    if not query:
        return []
    ensure_index()
    with _idx_lock:
        chunks = list(_idx.get("chunks") or [])
        vectors = _idx.get("vectors")
        tokens_list = list(_idx.get("tokens") or [])
    if not chunks:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    n = len(chunks)

    # ① 关键词：BM25 管排序细节，**覆盖率**管阈值
    post, lens = _build_postings(tokens_list)
    kw = _bm25(q_tokens, tokens_list, post, lens)
    kw_max = float(kw.max()) if kw.size else 0.0
    kw_norm = (kw / kw_max) if kw_max > 0 else kw
    cov = _coverage(q_tokens, n, post)

    # ② 语义分（向量已 L2 归一化，余弦 = 点积）
    vec = None
    if vectors is not None and len(vectors) == n:
        qv = embed_mod.embed_texts([query], is_query=True)
        if qv is not None and getattr(qv, "shape", (0,))[0] == 1:
            vec = vectors @ qv[0]

    # ③ 融合：语义为主、覆盖率兜底（专有名词/编号/数字靠它）
    if vec is not None:
        final = VEC_WEIGHT * vec + KW_WEIGHT * cov
    else:
        final = cov.copy()

    # ④ 阈值过滤：分数太低的不返回（用户要求）
    lo_final = MIN_FINAL if min_score is None else float(min_score)
    picked = []
    for i in range(n):
        base = float(final[i])
        if base < lo_final:
            continue
        v = float(vec[i]) if vec is not None else 0.0
        c = float(cov[i])
        # 语义和覆盖率**至少有一个**像样才收 —— 两个都低说明真的不相关
        if vec is not None and v < MIN_VEC and c < MIN_COV:
            continue
        if vec is None and c < MIN_COV:
            continue
        picked.append((base, i, v, c, float(kw_norm[i])))
    # 同分时用 BM25 打破平局（排序更稳定，结果可复现）
    picked.sort(key=lambda x: (x[0], x[4]), reverse=True)

    # 同一篇文档最多占 top_k 的一半名额：否则一个长文档的相邻块会把
    # 其他同样相关的文档全挤掉，用户看到"怎么全是同一篇"
    per_doc = max(2, int(top_k) // 2)
    used = {}
    results = []
    for f, i, v, k, bm in picked:
        c = chunks[i]
        did = c.get("doc_id") or ""
        if used.get(did, 0) >= per_doc:
            continue
        used[did] = used.get(did, 0) + 1
        rel = c.get("rel") or ""
        results.append({
            "doc_id": did,
            "score": round(f, 4),
            "vec_score": round(v, 4),
            "kw_score": round(k, 4),      # 查询词覆盖率（0~1，绝对意义）
            "kw_bm25": round(bm, 4),      # BM25 归一化分（仅参考）
            "chunk": c.get("idx"),
            "content": c.get("text", ""),
            "filename": os.path.basename(rel),
            "rel": rel,
            "folder": os.path.dirname(rel),
        })
        if len(results) >= int(top_k):
            break
    return results


def tree_text(max_items: int = 200, head_chars: int = 50) -> str:
    """给模型看的知识库目录树（按文件夹分组）。

    为什么要树形：用户是按课程 / 项目建子文件夹来组织资料的，
    平铺一串文件名会让模型**看不出结构**，也就想不到"去某个文件夹里找"。
    带上每篇开头几十个字，模型才好判断"这篇是不是我要的"。
    """
    _ensure_dir()
    docs = list_documents()
    if not docs:
        return "（知识库是空的）"
    groups: dict = {}
    for d in docs:
        groups.setdefault(d.get("folder") or "", []).append(d)
    lines = []
    shown = 0
    # 根目录排最后，子文件夹按名字排 —— 看起来就像资源管理器
    for folder in sorted(groups, key=lambda x: (x == "", x)):
        items = groups[folder]
        lines.append("【%s】%d 篇" % (folder or "根目录", len(items)))
        for d in items:
            if shown >= max_items:
                break
            full = os.path.join(KB_DIR, d["rel"].replace("/", os.sep))
            text, _note = read_doc(full)
            head = (text or "").strip().replace("\n", " ")[:head_chars]
            mark = "" if d.get("indexed") else "（读不出文字：%s）" % (d.get("note") or "格式不支持")
            lines.append("  · %s %s%s" % (d["filename"], mark, head))
            shown += 1
    if len(docs) > shown:
        lines.append("…… 还有 %d 篇" % (len(docs) - shown))
    return "\n".join(lines)


def build_rag_context(query: str, top_k: int = 4) -> str:
    """把检索结果拼成可注入 system 的 RAG 上下文。

    只在**真的命中**时才有内容（无关提问的分数是 0，`search` 会直接返回空），
    所以这相当于"自动检索"：相关就自动带上，不相关一点都不占上下文。

    同时在末尾告诉模型还能用 search_knowledge 工具继续深挖 ——
    自动检索只给最相关的几段，需要更多细节或想先看目录时由模型自己决定。
    """
    results = search(query, top_k)
    if not results:
        return ""
    lines = ["【知识库资料】（来自用户导入的领域文档，**优先于联网结果采信**；"
             "回答时注明出自哪一篇）"]
    for i, r in enumerate(results, 1):
        # 显示完整相对路径（含文件夹），模型才能说清"出自哪一篇"，
        # 也能据此知道该去哪翻更多资料
        tag = r.get("rel") or r.get("filename")
        lines.append(f"[资料{i}]({tag})\n{r['content']}")
    lines.append("\n（以上是自动检索到的相关片段，通常够用。"
                 "若需要更多细节、或想先看看知识库里都有哪些资料，"
                 "可调用 search_knowledge 工具（list_all=true 列目录）；"
                 "需要时效性信息时也可同时调用 web_search。）")
    return "\n".join(lines)
