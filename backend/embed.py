# -*- coding: utf-8 -*-
"""本地文本向量化（embedding）—— 给知识库检索提供语义向量。

两套后端，自动择优：

  ① **Ollama（优先）**：本机 `ollama serve` 在跑、且已经拉过 embedding 模型。
     走 `/api/embed`，一次请求可批量送多段文本，最快。
  ② **ONNX（回退）**：`data/models/embed/<模型名>/` 下有 model.onnx + tokenizer.json。
     用 onnxruntime + tokenizers 直接跑。

⚠️ 这里**刻意不碰 transformers**：本机 transformers 一 import 就要加载 torch，
而 torch 的 shm.dll 是坏的（WinError 126），import 直接抛 OSError。
`tokenizers` 是独立库，不依赖 torch，所以 ONNX 这条路只用它。

两个后端都不可用时 `available()` 返回 False，调用方自动退化为纯关键词检索 ——
**检索功能不能因为"没模型"整个挂掉**，这比"必须有向量"重要。

换模型会让向量维度变化，所以索引里必须记 backend + dim，不匹配就重建
（否则余弦算出来是错的，而且不报错，最难查）。
"""
import os
import json
import math
import threading
import logging

import numpy as np

from . import config

_LOG = logging.getLogger("uvicorn.error")

# 模型目录：data/models/embed/<模型名>/
_MODEL_DIR = config.data("data", "models", "embed")

# 后端候选，**按优先级从上到下**尝试。
#
# ⚠️ 中文专用模型必须排在前面，这是有实测依据的，不是拍脑袋：
#    nomic-embed-text 对中文几乎没有区分度 —— 查询「入团需要什么条件」时，
#    完全无关的「今天天气晴朗」相似度 0.708，反而**高于**真正相关的
#    「入团申请人需要年满十四周岁」0.664，相关/无关差距是 -0.007。
#    也就是说它给中文排序等于随机。中文知识库用它会让召回率不升反降，
#    所以只能放在最后给英文库兜底。
BACKENDS = [
    ("ollama", "bge-m3"),             # 中文最强（1.2GB）
    ("onnx", "bge-base-zh-v1.5"),     # 中文专用（约 400MB）
    ("onnx", "bge-small-zh-v1.5"),    # 中文专用，95MB，速度精度平衡最好
    ("ollama", "mxbai-embed-large"),  # 英文强
    ("ollama", "nomic-embed-text"),   # 仅英文兜底（中文实测无效）
]

ONNX_CANDIDATES = [c for b, c in BACKENDS if b == "onnx"]


def _match_model(names, cand: str) -> str:
    """在已装模型里找 cand（允许 `cand:tag` 形式），返回完整模型名。"""
    if not names:
        return ""
    base = cand.split(":")[0]
    for n in names:
        if n == cand:
            return n
    for n in names:
        if n.split(":")[0] == base:
            return n
    return ""

# bge 中文系列做**检索**时建议给查询加这个指令前缀，实测能提升召回。
# 只加在 query 上，文档侧不加。
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_state = {
    "backend": "",        # "ollama" / "onnx" / ""
    "model": "",          # 模型标识，写进索引用于判断是否需要重建
    "dim": 0,
    "ready": False,
    "probed": 0.0,        # 上次探测时间（Ollama 可能后启动，要允许重探）
    "error": "",
}
_lock = threading.Lock()
_onnx = {}                # {"tok":..., "sess":..., "names":[...]}


# ---------- Ollama ----------
def _ollama_url() -> str:
    try:
        cfg = config.load_config()
        return str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    except Exception:
        return "http://127.0.0.1:11434"


def _http_json(url: str, payload=None, timeout=180):
    import urllib.request
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ollama_models():
    """返回已拉取的模型名列表；服务不可达时返回 []。"""
    try:
        data = _http_json(_ollama_url() + "/api/tags", timeout=4)
        return [str(m.get("name") or "") for m in data.get("models", [])]
    except Exception:
        return []


def _pick_ollama_model(names):
    """从已装模型里挑一个 embedding 模型。

    ⚠️ 必须精确比对模型名，不能看名字里有没有 "embed" ——
    对话模型（如 qwen2.5:7b）也能被 /api/embed 调用，但**输出维度不对**，
    更糟的是它不会报错，会安静地给出一堆语义无关的向量。
    """
    if not names:
        return ""
    norm = {}
    for n in names:
        base = n.split(":")[0]
        norm.setdefault(base, n)          # 保留带 tag 的完整名
        norm.setdefault(n, n)
    for cand in OLLAMA_CANDIDATES:
        if cand in norm:
            return norm[cand]
        base = cand.split(":")[0]
        if base in norm:
            return norm[base]
    return ""


def _ollama_embed(texts, model):
    """批量向量化。返回 list[list[float]]；失败抛异常。"""
    url = _ollama_url() + "/api/embed"
    try:
        data = _http_json(url, {"model": model, "input": list(texts)})
        embs = data.get("embeddings")
        if embs and len(embs) == len(texts):
            return embs
        raise ValueError("返回条数不匹配：%s vs %s" % (
            len(embs) if embs else 0, len(texts)))
    except Exception as e:
        # 老版本 Ollama 没有 /api/embed，只有 /api/embeddings（一次一条）
        if "404" not in str(e) and "not found" not in str(e).lower():
            raise
    out = []
    for t in texts:
        d = _http_json(_ollama_url() + "/api/embeddings",
                       {"model": model, "prompt": t})
        out.append(d.get("embedding") or [])
    return out


# ---------- ONNX ----------
def _onnx_load(name):
    """加载本地 ONNX 模型 + tokenizer，返回 (tok, sess, input_names) 或 None。"""
    d = os.path.join(_MODEL_DIR, name)
    mpath = os.path.join(d, "model.onnx")
    tpath = os.path.join(d, "tokenizer.json")
    if not (os.path.isfile(mpath) and os.path.isfile(tpath)):
        return None
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except Exception as e:
        _LOG.warning("ONNX 后端依赖缺失：%s", e)
        return None
    try:
        tok = Tokenizer.from_file(tpath)
        tok.enable_truncation(max_length=512)
        tok.enable_padding(pad_id=0, pad_token="[PAD]")
        sess = ort.InferenceSession(mpath, providers=["CPUExecutionProvider"])
        names = [i.name for i in sess.get_inputs()]
        return tok, sess, names
    except Exception as e:
        _LOG.warning("ONNX 模型加载失败（%s）：%s", name, e)
        return None


def _onnx_embed(texts, loaded):
    tok, sess, names = loaded
    encs = tok.encode_batch([str(t) for t in texts])
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in names:
        feeds["token_type_ids"] = np.array([e.type_ids for e in encs], dtype=np.int64)
    feeds = {k: v for k, v in feeds.items() if k in names}
    out = sess.run(None, feeds)
    # bge / bge-zh 系列用 **CLS pooling**（取 [CLS] 位置的输出），不是均值池化
    vec = np.asarray(out[0], dtype=np.float32)
    if vec.ndim == 3:
        vec = vec[:, 0, :]
    # L2 归一化：之后余弦相似度就是点积，省一半算力
    norm = np.linalg.norm(vec, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return (vec / norm).tolist()


# ---------- 对外 ----------
def probe(force: bool = False) -> dict:
    """探测可用后端（结果缓存；force=True 强制重探）。

    Ollama 可能是应用启动后才拉起来/才拉完模型的，所以缓存带 60 秒有效期，
    过期的下次调用会重新探一次 —— 否则用户拉了模型还得重启应用才能用上。
    """
    import time
    with _lock:
        now = time.time()
        if not force and _state["ready"] and now - _state["probed"] < 60:
            return dict(_state)
        if not force and not _state["ready"] and now - _state["probed"] < 60:
            return dict(_state)

        _state.update({"backend": "", "model": "", "dim": 0,
                       "ready": False, "error": "", "probed": now})

        names = _ollama_models()
        errs = []
        for backend, cand in BACKENDS:
            try:
                if backend == "ollama":
                    model = _match_model(names, cand)
                    if not model:
                        continue
                    v = _ollama_embed(["探测"], model)
                    dim = len(v[0]) if v and v[0] else 0
                    if dim <= 0:
                        continue
                    _state.update({"backend": "ollama", "model": model,
                                   "dim": dim, "ready": True})
                    _LOG.info("向量后端：Ollama / %s（%d 维）", model, dim)
                    return dict(_state)
                else:
                    loaded = _onnx_load(cand)
                    if not loaded:
                        continue
                    v = _onnx_embed(["探测"], loaded)
                    dim = len(v[0]) if v and v[0] else 0
                    if dim <= 0:
                        continue
                    _onnx.update({"tok": loaded[0], "sess": loaded[1],
                                  "names": loaded[2], "name": cand})
                    _state.update({"backend": "onnx", "model": cand,
                                   "dim": dim, "ready": True})
                    _LOG.info("向量后端：ONNX / %s（%d 维）", cand, dim)
                    return dict(_state)
            except Exception as e:
                errs.append("%s/%s: %s" % (backend, cand, e))

        _state["error"] = ("没有可用的向量模型（Ollama 里没有 embedding 模型，"
                           "本地也没有 ONNX 模型）"
                           + ("；尝试过：" + "；".join(errs[:3]) if errs else ""))
        return dict(_state)


def available() -> bool:
    return bool(probe().get("ready"))


def info() -> dict:
    st = probe()
    return {"backend": st.get("backend", ""), "model": st.get("model", ""),
            "dim": st.get("dim", 0), "ready": bool(st.get("ready")),
            "error": st.get("error", "")}


def signature() -> str:
    """后端签名，形如 `ollama:nomic-embed-text:768`。索引靠它判断要不要重建。"""
    st = probe()
    if not st.get("ready"):
        return "none"
    return "%s:%s:%d" % (st.get("backend"), st.get("model"), st.get("dim"))


def embed_texts(texts, is_query: bool = False, batch: int = 24):
    """把一批文本变成 L2 归一化的向量矩阵。

    返回 `np.ndarray (n, dim) float32`；**任何失败都返回 None**，
    由调用方退化为关键词检索 —— 不抛异常，避免一条坏文本毁掉整个索引构建。
    """
    texts = [str(t or "") for t in texts]
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    st = probe()
    if not st.get("ready"):
        return None
    backend, model = st["backend"], st["model"]

    # bge 中文系列：查询侧加指令前缀（只加查询，文档侧不加）
    if is_query and str(model).lower().startswith("bge") and backend == "onnx":
        texts = [BGE_QUERY_PREFIX + t for t in texts]

    out = []
    try:
        for i in range(0, len(texts), batch):
            part = texts[i:i + batch]
            if backend == "ollama":
                vecs = _ollama_embed(part, model)
            else:
                loaded = (_onnx.get("tok"), _onnx.get("sess"), _onnx.get("names"))
                if not loaded[0]:
                    return None
                vecs = _onnx_embed(part, loaded)
            out.extend(vecs)
    except Exception as e:
        _LOG.warning("向量化失败（%s/%s）：%s", backend, model, e)
        return None

    arr = np.asarray(out, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] != len(texts):
        return None
    # 统一再做一次 L2 归一化（Ollama 各模型是否归一化不一致）
    norm = np.linalg.norm(arr, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return arr / norm


def model_dir() -> str:
    return _MODEL_DIR


def install_hint() -> str:
    """给用户看的安装提示。"""
    return ("向量检索需要一个 embedding 模型（二选一）：\n"
            "· 推荐：命令行执行 `ollama pull nomic-embed-text`（274MB，中英通用）\n"
            "· 或把 bge-small-zh-v1.5 的 model.onnx + tokenizer.json "
            "放进 %s/bge-small-zh-v1.5/\n"
            "没装也能用，会自动退化为关键词检索（召回率低一些）。" % _MODEL_DIR)
