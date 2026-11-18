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
logger = logging.getLogger(__name__)

_LOG = logging.getLogger("uvicorn.error")

VEC_FILE = os.path.join(memory.MEM_DIR, "_vec.npy")
META_FILE = os.path.join(memory.MEM_DIR, "_vec_meta.json")

# 门槛：按实测校准（bge-small-zh：相关条目 vec 约 0.45~0.78，无关约 0.15~0.35）
# 实测对比（12 条已知答案的查询 + 6 条无关查询）：
#   0.42 → top3 12/12，无关查询误召 2/6
#   0.46 → top3 12/12，无关查询误召 1/6   ← 取这个
#   0.50 → top3 11/12（开始漏），误召 1/6
MIN_VEC = 0.46
MIN_COV = 0.50
# 「（推断）」条目的排序降权。实测长期记忆里 18/69（26%）是推断条目，
# 它们写得像总结、向量分偏高，常把事实条目（"专业：人工智能技术应用"）挤出 top_k。
INFER_PENALTY = 0.82
W_VEC, W_COV = 0.75, 0.25        # 只用于排序，不参与拦截

MAX_ITEMS = 4000                 # 记忆条目上限
# 转录：不进索引，只做预筛 + 重排
TRANS_PASSAGE = 600              # 一段对话最多多少字
TRANS_MAX = 2500                 # 最多收集多少段（按文件 mtime 从新到旧）
TRANS_PER_FILE = 400             # 单个文件最多取多少段（防止一个巨型测试会话吃掉全部名额）
TRANS_CANDIDATES = 8             # 预筛出来送去向量重排的候选数（实测 14 条要 400ms+）
TRANS_TOP = 2                    # 转录最多挤进结果几条

_lock = threading.RLock()
# ⚠️ **必须和 _lock 分开的一把锁**，而且只能在它里面做「读/写一个布尔值」。
# 原因（2026-11-18 实测踩到死锁）：memory 的写函数在持有 _MEM_LOCK 时调 touch()，
# 若 touch() 去抢 _lock，而后台 sync() 正持着 _lock 调 memory.get_long()（要 _MEM_LOCK），
# 两条线程就互相等成环 —— 整个应用直接卡死（测试跑满 600 秒被强杀）。
# 现在 touch 只碰这把叶子锁，永远不再请求别的锁，环就断了。
_touch_lock = threading.Lock()
_idx = {"sig": "", "dsig": "", "items": [], "mat": None}
_trans = {"sig": "", "items": [], "files": {}, "ver": 0}
# 解析逻辑变了就把它 +1，否则按 (mtime, size) 命中的**旧解析结果**会一直沿用
# （改完 `_strip_injected` 后，缓存里那 429 条带交接卡的条目不会自己消失）。
_TRANS_VER = 2
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
def _text_sets(text: str):
    """把文本变成 (实字集合, 2-gram 集合)。转录条目在构建时预算好，查询时不再分词。"""
    t = text or ""
    chars = {c for c in t
             if c not in _STOP_CHARS and c not in _PUNCT and not c.isspace()}
    return chars, set(memory._tokenize(t))


def _q_sets(query: str):
    return _text_sets(query)


def _cov_pair(qchars, qtoks, tchars, ttoks):
    """返回 (字级覆盖, 2-gram 覆盖)。

    门槛用两者**较大值**（只要一种说得通就收）——
    实测：查「我在哪个学校读书」，2-gram 只命中 {学校}（0.14），
    但字级是 2/4=0.5，靠字级才召回得到「学校：广州民航职业技术学院…」。
    排序用加权（2-gram 更能分出「后端」与「前端」这类近义边界）。
    """
    c = (len(qchars & tchars) / float(len(qchars))) if qchars else 0.0
    b = (len(qtoks & ttoks) / float(len(qtoks))) if qtoks else 0.0
    return c, b


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
    long_n = len(items)
    for sid, obj in (shorts or {}).items():
        body = str((obj or {}).get("content") or "")
        for t in memory._split_items(body):
            _push(items, "短期记忆", sid, sid, t)
    if len(items) > MAX_ITEMS:
        # ⚠️ **长期记忆一条都不能丢**。原来是 `items[-MAX_ITEMS:]`（保留尾部），
        # 而长期记忆恰好排在最前面 —— 条目一超限，整块长期档案被裁掉，
        # 只剩短期记忆，检索就再也找不到姓名/身份/专业。
        # 实测（2026-11-18，5 个会话各 1000 条短期）：收集到 4000 条，
        # **全部是短期**，「用户姓名：陈宇桐」消失，第一条变成「短期条目1-0」。
        # 正确做法：长期全保，只裁短期（保留靠后的，新会话更重要）。
        if long_n >= MAX_ITEMS:
            # 极端情况：长期自己就超了。保尾部（最新写的），但要记日志
            items = items[len(items) - MAX_ITEMS:]
            logger.warning("长期记忆条目数 %d 已超索引上限 %d，仅索引最近的部分",
                           long_n, MAX_ITEMS)
        else:
            keep_short = MAX_ITEMS - long_n
            items = items[:long_n] + items[len(items) - keep_short:]
    return items


def _data_sig():
    parts = []
    for p in (memory.LONG_FILE, memory.SHORT_FILE):
        try:
            st = os.stat(p)
            # ⚠️ 必须用**纳秒** mtime。原来取整到秒，实测同一秒内把
            # 「用户姓名：陈宇桐」改成「用户姓名：张小明」（字节数相同），
            # 数据签名一模一样 → sync() 判定"没变化" → 索引里永远留着旧名字，
            # 检索也永远返回旧的。纳秒级现实中撞不上。
            parts.append("%d:%d" % (st.st_size, st.st_mtime_ns))
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
                mt = max(mt, st.st_mtime_ns)
        except Exception:
            logger.debug("silent-except", exc_info=True)
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
        # ⚠️ `mat is None` **不等于**数据没了 —— 它多半只是"这一轮向量化失败"
        # （模型还没加载完、显存不够、onnx 报错…）。
        # 原来这时会 `os.remove(VEC_FILE/META_FILE)`，把磁盘上**一份完好的索引**删掉：
        # 实测（2026-11-18）调一次 `_save_disk(sig, items, None)`，638KB 的 _vec.npy 直接消失。
        # 后果是下次模型恢复后要**全量重算**（109 条几秒，条目多时更久），
        # 而且如果失败反复发生，索引永远建不起来。
        # 正确做法：只有"真的没有条目了"才删；向量暂时不可用就**保留旧索引不动**。
        if not items:
            for p in (VEC_FILE, META_FILE):
                if os.path.isfile(p):
                    os.remove(p)
            return
        if mat is None:
            _LOG.warning("本轮向量不可用，保留磁盘上的旧索引（不覆盖、不删除）")
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
        # `_dirty` 也必须参与判断：记忆刚被写过就一定要重算条目，
        # 不能只靠文件签名（签名可能因为"同秒同字节"看不出变化）。
        if (not force and not _dirty and _idx["mat"] is not None
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
        with _touch_lock:
            _syncing = False


def touch():
    """记忆被写入后调用：标记脏 + 后台增量同步（不阻塞调用方）。

    ⚠️ 只碰 `_touch_lock`（叶子锁），**绝不**在这里请求 `_lock` 或 memory 的锁，
    否则会和后台 sync() 形成锁序环（详见 `_touch_lock` 处的说明）。
    """
    global _dirty, _syncing
    _dirty = True
    with _touch_lock:
        if _syncing:
            return
        _syncing = True
    try:
        threading.Thread(target=_bg_sync, daemon=True).start()
    except Exception:
        with _touch_lock:
            _syncing = False


# =====================================================================
#  历史会话：预筛 + 向量重排（不进大索引）
# =====================================================================
# ⚠️ **应用自己注入到用户消息里的内容，不是用户说的话**。
# 每轮重启后，系统会往用户消息末尾追加「自我迭代交接卡」和「工具动作账本」，
# 它们以 role="user" 落盘，于是被当成用户原话进了转录检索。
# 实测（2026-11-18）：转录候选 2500 条里 **429 条（17%）** 含交接卡，
# 查「上次重启」返回的第一条就是交接卡本身，而不是真正的对话内容。
# 这些段总是追加在用户原话**之后**，所以从标记处截到末尾即可。
_INJECTED_MARKS = (
    "【自我迭代交接卡",
    "【本对话真实执行过的工具动作",
    "【最近",          # 【最近 N 轮速览】同样是系统注入的
)


def _strip_injected(c: str) -> str:
    """去掉系统注入的段落，只留用户/助手真正说的话。"""
    s = c or ""
    cut = len(s)
    for m in _INJECTED_MARKS:
        i = s.find(m)
        if 0 <= i < cut:
            cut = i
    return s[:cut].strip()


def _parse_trans_file(path: str, fn: str) -> list:
    """把一个 .jsonl 切成「一段对话」的条目列表。"""
    out = []
    buf, buf_len = [], 0

    def _flush():
        if not buf or len(out) >= TRANS_PER_FILE:
            return
        txt = "\n".join("%s：%s" % (r, c) for r, c in buf)[:TRANS_PASSAGE]
        ch, tk = _text_sets(txt)
        out.append({"key": "T:%s:%d" % (fn, len(out)),
                    "level": "历史会话", "sid": fn[:-6],
                    "section": fn[:-6], "text": txt, "source": "transcript",
                    "_chars": ch, "_tok": tk})

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
                # 剥掉应用自己注入的交接卡/账本/速览（详见 _strip_injected 处的实测）
                c = _strip_injected(c)
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
    return out


def _trans_items():
    """把转录文件切成「一段对话」的候选，按文件新→旧收集，有上限。

    ⚠️ **按文件增量缓存**：转录每轮对话都会追加，如果每次都重扫全部 19.6MB，
    每次 search_memory 都要 1 秒。这里按 (mtime, size) 判断哪个文件变了，
    只重解析变了的那个，其余复用上次结果。
    """
    d = memory.TRANSCRIPT_DIR
    if not os.path.isdir(d):
        return []
    files = []
    for fn in os.listdir(d):
        if not fn.endswith(".jsonl"):
            continue
        p = os.path.join(d, fn)
        try:
            st = os.stat(p)
            files.append((st.st_mtime_ns, fn, p, st.st_size))
        except Exception:
            logger.debug("silent-except", exc_info=True)
    files.sort(reverse=True)          # 新的优先

    with _lock:
        if _trans.get("ver") != _TRANS_VER:
            _trans.update({"ver": _TRANS_VER, "files": {}, "items": []})
        cache = dict(_trans.get("files") or {})

    new_cache, out = {}, []
    for mt, fn, path, size in files:
        if len(out) >= TRANS_MAX:
            break
        stamp = (int(mt), size)
        got = cache.get(fn)
        if got and got[0] == stamp:
            items = got[1]
        else:
            items = _parse_trans_file(path, fn)
        new_cache[fn] = (stamp, items)
        for it in items:
            if len(out) >= TRANS_MAX:
                break
            out.append(it)

    with _lock:
        _trans.update({"files": new_cache, "items": out})
    return out


def _search_transcripts(query: str, top_k: int = TRANS_TOP) -> list:
    """历史会话：先按字面预筛（便宜），再只给候选算向量（贵）。"""
    items = _trans_items()
    if not items:
        return []
    qc, qt = _q_sets(query)

    scored = []
    for it in items:
        c, b = _cov_pair(qc, qt, it.get("_chars") or set(), it.get("_tok") or set())
        cov = max(c, b)
        if cov >= MIN_COV:
            scored.append((cov, 0.6 * c + 0.4 * b, it))
    if not scored:
        return []
    scored.sort(key=lambda x: x[1], reverse=True)
    cand = scored[:TRANS_CANDIDATES]

    vecs, qv = None, None
    try:
        vecs = embed.embed_texts([it["text"] for _, _, it in cand])
        qv = embed.embed_texts([query], is_query=True)
        if vecs is None or qv is None:
            vecs = None
    except Exception:
        vecs = None

    out = []
    for i, (cov, rank_cov, it) in enumerate(cand):
        vec = 0.0
        if vecs is not None:
            vec = float(np.dot(vecs[i], qv[0]))
            # 门槛收紧：实测原来「语义 0.34 + 字面 0.80」也能过，
            # 结果查「挑餐厅」把一段系统提示词捞了出来。转录必须语义也像。
            if not (vec >= 0.45 or (vec >= 0.38 and cov >= 0.85)):
                continue
        else:
            # ⚠️ **向量不可用时的兜底**（模型没装 / onnx 加载失败 / 显存不够）。
            # 原来这里 `vec` 恒为 0.0，于是上面那条判定永远不成立 → 转录部分**一条都返回不了**，
            # 而模块开头的设计写着"检索功能不能因为没模型整个挂掉"。
            # 退化为**高阈值字面匹配**（cov >= 0.85，宁缺毋滥）：
            # 至少"用户原话里提到过的关键词"还能搜到，而不是整块功能消失。
            if cov < 0.85:
                continue
        score = (W_VEC * vec + W_COV * rank_cov) if vecs is not None else rank_cov
        body = it["text"]
        if len(body) > 300:
            body = body[:300] + " …（后续已截断）"
        out.append({"level": "历史会话", "section": it["section"],
                    "content": body, "source": "transcript",
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
        # 与 search() 的过滤口径保持一致：空 sid 等价 NO_SESSION_SID。
        # 原来空 sid 时本对话的条目会被标成「其他对话短期」（实测踩过），
        # 模型看到这个标签会当成"别人的事"，从而不用它回答。
        mine = sid or memory.NO_SESSION_SID
        return "本对话短期" if it["sid"] == mine else "其他对话短期"
    return lv


def search(query: str, sid: str = "", top_k: int = 5,
           min_vec: float = MIN_VEC, min_cov: float = MIN_COV,
           with_transcripts: bool = True, levels=None) -> list:
    """条目级检索。返回命中的**条目**（不是整块正文），无关时返回空列表。"""
    q = (query or "").strip()
    if not q:
        return []
    # ⚠️ sid 要在这里也归一化一次。索引里存的 `it["sid"]` 是 `memory._safe_sid`
    # 之后的（落盘的 key 就是 safe 过的），而本函数是**公开入口**，
    # 任何调用方传原始 id（含空格/中文/点号）都会让下面的过滤条件永远成立 →
    # 本对话的短期记忆**一条都召回不到**。
    # 实测（2026-11-18）：sid="2026-11-18 22:50:34" 传原始值 → []；
    # 传 safe 值 → 召回（vec 0.7056）。memory.search_all 已经归一化了，
    # 但 `_ctx_trim_long` 等直接调本函数的地方不应该依赖调用方自觉。
    sid = memory._safe_sid(sid) if sid else ""
    st = sync()
    # ⚠️ `items` 和 `mat` 必须**一起取**。`_idx.update()` 是逐个键替换的，
    # 原来 `items, mat = st["items"], st["mat"]` 是两次独立读取 ——
    # 中间若另一个线程跑完一次 sync()（条目增删/重排），就会拿到"旧条目 + 新矩阵"：
    # 轻则 `vec_scores[i]` 越界抛异常（退化成关键词检索），
    # 重则**分数贴到别的条目上**，返回一条内容与得分完全不匹配的记忆。
    with _lock:
        items = list(st["items"])
        mat = st["mat"]
    if not items:
        return []

    vec_scores = None
    if mat is not None:
        qv = embed.embed_texts([q], is_query=True)
        if qv is not None and qv.ndim == 2 and qv.shape[1] == mat.shape[1]:
            vec_scores = (mat @ qv[0]).astype(float).tolist()

    qc, qt = _q_sets(q)
    out = []
    for i, it in enumerate(items):
        # 短期记忆**按对话隔离**：给了 sid 就只留本对话的。
        # 原来 sid 只用来打标签（_label），别的对话的短期记忆照样参与排序，
        # 实测查「今天天气怎么样」时前 4 条全是别的对话里的元评论，
        # 把真正的偏好条目挤出了 top_k。
        # ⚠️ **sid 为空时不能"不过滤"**。原来条件是 `if sid and ...`，
        # 于是 sid="" 时这条过滤整个失效 → **所有对话的短期记忆全部进候选**。
        # 实测（2026-11-18）：A、B 两个对话各有一条「考勤打卡系统…」，
        # 空 sid 检索把两条 + _nosession 那条一起返回了（3 条全中），
        # 而 tools._do_remember 在空 session 时用的是 NO_SESSION_SID 兜底 ——
        # 也就是说"记进去的那份"和"检索时看的那份"根本不是一个口径。
        # 现在统一：空 sid 等价于 NO_SESSION_SID（与写入侧兜底一致）。
        if it["level"] == "短期记忆":
            mine = sid or memory.NO_SESSION_SID
            if it["sid"] != mine:
                continue
        # ⚠️ 按层级过滤（`_ctx_trim_long` 只想要长期记忆）。
        # 原来那边是"取 top_k=12 再自己筛 level==长期记忆" —— 而本对话的短期条目
        # 同样占名额，相关条目一多，12 个名额可能被短期记忆全占掉，
        # 筛完剩 0 条，等于"按当前问题挑相关档案"这一步白做。
        if levels is not None and it["level"] not in levels:
            continue
        c, b = _cov_pair(qc, qt, *(_text_sets(it["text"])))
        cov = max(c, b)                       # 门槛：任一说得通就收
        vec = float(vec_scores[i]) if vec_scores else 0.0
        if vec >= min_vec or cov >= min_cov:
            rank_cov = 0.6 * c + 0.4 * b      # 排序：2-gram 更能分近义词边界
            score = (W_VEC * vec + W_COV * rank_cov) if vec_scores else rank_cov
            # 事实优先于推断：长期记忆里 26% 是「（推断）」条目，它们措辞更"总结感"，
            # 向量分天然偏高，经常把真档案（如"专业：人工智能技术应用"）挤下去。
            # 只降排序分，不影响门槛判定 —— 推断条目该召回还是召回，只是排在事实后面。
            if it["text"].startswith(("（推断）", "(推断)")):
                score *= INFER_PENALTY
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

    # 名额分配：**记忆占前 top_k-1 位，转录固定留 1 个**。
    # 为什么不让两边一起排序：实测查「我在哪个学校读书」时，转录里一段
    # 「帮我做份学校图书馆的PPT」分数 0.478，高于真正的答案
    # 「学校：广州民航职业技术学院…」的 0.375，一起排序会把答案挤出去。
    # 而完全不查转录又会让「我说过什么关于PPT的事」永远看不到历史对话。
    # 分开占名额两头都能顾到。
    n_mem = top_k - 1 if (with_transcripts and top_k > 1) else top_k
    res = res[:n_mem]

    if with_transcripts:
        try:
            for h in _search_transcripts(q, top_k=1):
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
            logger.debug("silent-except", exc_info=True)
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
        _trans.update({"files": {}, "items": []})
        for p in (VEC_FILE, META_FILE):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                logger.debug("silent-except", exc_info=True)
    t0 = time.time()
    st = sync(force=True)
    info = embed.info()
    return {"items": len(st["items"]), "indexed": st["mat"] is not None,
            "ms": int((time.time() - t0) * 1000),
            "backend": info.get("backend", ""), "model": info.get("model", "")}
