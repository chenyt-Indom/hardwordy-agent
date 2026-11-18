# -*- coding: utf-8 -*-
"""记忆库：**一份长期记忆（跨对话共享） + 每个对话一份短期记忆（互相独立）**。

为什么这么分
------------
- **长期记忆**放"无论聊什么都成立"的稳定信息：姓名、身份、偏好、约定。
  它**全局只有一份**，所以新开对话也不会"不认识你"。
- **短期记忆**放"这件事本身"的上下文要点：正在做的项目、这段讨论的结论。
  它**按对话隔离**——切到别的对话就是另一份，互不串味；而且**可以随时释放**。

存储（data/memory/）
    long_term.json    {"content": "...", "updated_at": 123}
    short_term.json   {"<session_id>": {"content": "...", "updated_at": 123}}

归档（transcript）仍在 data/transcript/，是"提取记忆"的原始素材；
释放归档前会先把它沉淀进记忆（见 main.py 的 _sweep_*），避免重要内容一起被删掉。
"""
import os
import re
import json
import time
import hashlib
import functools
import threading
import logging
from . import config
logger = logging.getLogger(__name__)

_LOG = logging.getLogger("uvicorn.error")

# ⚠️ 记忆是**读-改-写**：get_long → 拼一条 → set_long。
# 没有锁时两个线程同时做，后写的会把先写的整段覆盖掉。
# 实测（2026-11-18）：4 个线程各写 15 条长期记忆，最终只剩 20 条（丢了 40 条）；
# 短期记忆更狠，4 个会话同时写，30 次试验**全部**出现会话丢失。
# 真实场景里「后台自动提炼」和「remember 工具」本来就可能同时写，所以必须串行化。
_MEM_LOCK = threading.RLock()


def _locked(fn):
    """把一次读-改-写整体锁住（RLock，内部嵌套调用安全）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _MEM_LOCK:
            return fn(*args, **kwargs)
    return wrapper

DATA_DIR = config.data("data")
MEM_DIR = os.path.join(DATA_DIR, "memory")
LONG_FILE = os.path.join(MEM_DIR, "long_term.json")
SHORT_FILE = os.path.join(MEM_DIR, "short_term.json")
TRANSCRIPT_DIR = os.path.join(DATA_DIR, "transcript")

# 老格式（一次性迁移用，迁移后改名保留，不删）
LEGACY_DOC_FILE = os.path.join(DATA_DIR, "memory_doc.json")        # 多分区版
LEGACY_CONV_FILE = os.path.join(MEM_DIR, "conversations.json")     # 早期"每对话一块"版
LEGACY_GLOBAL_FILE = os.path.join(MEM_DIR, "global.json")          # 早期"全局偏好"版

# 上限。
# 长期记忆放宽到 15000 字：它是跨对话共享的"用户档案 + 领域知识"，
# 用户明确要求能装下更多内容（原来 2000 字确实容易顶到）。
# 短期记忆仍保持精简 —— 它只服务当前这一件事，塞多了反而稀释重点。
LONG_CAP = 15000     # 长期记忆
SHORT_CAP = 1000     # 单个对话的短期记忆

# 空会话时的兜底 id：**定义在这里**，main.py 和 tools.py 都引用它。
# 原来 main.py 里硬编码一份 `_NO_SESSION_SID`，而 tools.py 里压根没有 ——
# 于是「前端清过缓存、session 为空」时，自动提炼能写进这个兜底 id，
# 而 remember 工具却直接回「无法确定当前对话，记忆未写入」，
# 两条写入路径对同一场景的处理不一致。集中一处避免漂移。
NO_SESSION_SID = "_nosession"

_STOP_WORDS = set(
    "的 了 在 是 和 与 我 你 他 她 它 我们 你们 一 个 有 也 都 要 把 让 这 那 就 吗 呢 啊 吧 很 会 可以 请 帮 下 换 或 并 及 想 要 说 请".split())


# =====================================================================
#  基础读写
# =====================================================================
def _now():
    return time.time()


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    # ⚠️ **读也要拿锁**。Windows 上 os.replace 不能替换一个正被读打开的文件，
    # 并发时会直接抛 PermissionError(WinError 5)。
    # 实测（2026-11-18，6 个读线程 + 4 个写线程）：写操作失败 163 次，
    # 240 条要点只落进 69 条 —— 而调用方大多把异常吞掉，用户看到的就是"这条又没记住"。
    # 读写共用 _MEM_LOCK（RLock，可重入）后彻底消除。
    # ⚠️ **读和"发现损坏后的备份"必须在同一把锁里**。
    # 原来 `with _MEM_LOCK` 只包了 open+load，备份在锁外：
    #   线程 A 读到坏文件 → 出 with（释放锁）→ 线程 B 写入成功（replace 落盘）
    #   → A 才执行 `os.replace(path, bad)`，把 B **刚写好的正常文件**当成坏文件挪走
    #   → A 返回 default，B 的写入就这么"消失"了（下一次写入再用 default 覆盖）。
    # 现在整段都在锁内，并且备份前**再确认一次**它还是坏的。
    with _MEM_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # 文件存在、非空、却解析不了 → **先备份再返回默认值**。
            # 否则下一次写入会拿默认值把整份记忆覆盖掉，用户的档案就彻底没了。
            try:
                if os.path.getsize(path) > 0:
                    still_bad = True
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            json.load(f)
                        still_bad = False      # 刚刚被修好了，别动它
                    except Exception:
                        still_bad = True
                    if still_bad:
                        # ⚠️ 先**抢救**：JSON 整体解析失败，但 `"content": "…"` 这一段
                        # 往往还是完整的（截断/多一个字符最常见），能抠出来就别丢。
                        salvaged = _salvage(path)
                        bad = "%s.corrupt-%d" % (path, int(_now()))
                        os.replace(path, bad)
                        logger.warning("记忆文件解析失败，已备份到 %s（抢救出 %d 字）",
                                       bad, len(str((salvaged or {}).get("content") or "")))
                        keep = salvaged if salvaged is not None else default
                        # ⚠️⚠️ **必须立刻把文件重建出来**。原来备份完就 return，
                        # 文件从此不存在；而 `_read_json` 开头的
                        # `if not os.path.exists(path): return default` 会直接给空。
                        # 实测（2026-11-18，4 线程各写 20 条）：80 条只落进 **19 条** ——
                        # 每个线程都读到"文件不存在"→ 拿空正文 → 各自 set_long 覆盖前一个。
                        try:
                            _write_json(path, keep)
                        except Exception:
                            logger.debug("silent-except", exc_info=True)
                        return keep
                    else:
                        with open(path, "r", encoding="utf-8") as f:
                            return json.load(f)
            except Exception:
                logger.debug("silent-except", exc_info=True)
            # 连备份都没做成：也要保证文件存在，否则同样会引发上面的互相覆盖
            try:
                if not os.path.exists(path):
                    _write_json(path, default)
            except Exception:
                logger.debug("silent-except", exc_info=True)
            return default


def _salvage(path: str):
    """从一份**解析失败的**记忆 JSON 里尽量把 `content` 抠出来。

    为什么值得做：记忆文件被写坏通常是"截断"或"多/少一个字符"，
    而 `{"content": "用户姓名：…；…", "updated_at": …}` 里那段长字符串
    绝大多数情况是完整的。整份丢掉 = 用户档案没了；
    抠出来再备份，等于把损失降到"最后一条可能残缺"。

    返回 `{"content": ..., "updated_at": ...}` 或 None（实在抠不出来）。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception:
        return None
    m = re.search(r'"content"\s*:\s*"', raw)
    if not m:
        return None
    s = raw[m.end():]
    out, i = [], 0
    esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
           "/": "/", "b": "\b", "f": "\f"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "u" and i + 5 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except Exception:
                    logger.debug("silent-except", exc_info=True)
            out.append(esc.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    txt = "".join(out).strip()
    if not txt:
        return None
    return {"content": txt, "updated_at": _now()}


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # ⚠️ tmp 名字里必须带线程号。原来固定用 `path + ".tmp"`，两个线程同时写
    # 会抢同一个临时文件，os.replace 直接抛 PermissionError（实测 WinError 32/5）——
    # 而调用方（提炼线程 / remember 工具）大多把异常吞掉，用户看到的就是"这条没记住"。
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with _MEM_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)      # 原子替换，避免写一半断电把记忆写坏
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                logger.debug("silent-except", exc_info=True)
            raise
    # 记忆一改，向量索引就得跟着更新（后台增量同步，不阻塞调用方）。
    # 挂在 _write_json 这一处收口，长期/短期/清空/释放全都覆盖到，
    # 不用在每个写函数里各加一行 —— 少一处遗漏的机会。
    if os.path.abspath(path) in (os.path.abspath(LONG_FILE),
                                 os.path.abspath(SHORT_FILE)):
        _index_touch()


def _index_touch():
    """通知向量索引：数据变了。任何失败都不影响记忆本身的读写。"""
    try:
        from . import memvec
        memvec.touch()
    except Exception:
        logger.debug("silent-except", exc_info=True)


def _size(path) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _safe_sid(sid) -> str:
    """把任意 session id 变成安全的文件名片段。

    ⚠️ 光做字符替换会**碰撞**，而碰撞的后果是两个对话共用一份短期记忆。
    实测（2026-11-18）：`"中文对话"` 和 `"英文对话"` 都变成 `"____"`、
    `"a.b"` 和 `"a b"` 都变成 `"a_b"`、`"x/y"` 和 `"x_y"` 都变成 `"x_y"`；
    先 `set_short("a.b", …)` 再 `get_short("a b")` 直接读到了 A 的内容。
    真实前端用的是 12 位 hex（`00db3090fb4f`），碰不到这个问题，但
    `data/transcript/` 里确实躺着 `cm-____.jsonl`、`e2e-__.jsonl` 这类
    被替换成下划线的归档 —— 只要 id 里出现中文/空格/点号，就可能串台。

    修法：**只有在 sanitize 改动了原文、或超长被截断时**才补原 id 的短哈希；
    纯安全字符的 id（包括所有真实 hex id）原样返回，老数据零迁移。
    """
    raw = str(sid or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if safe != raw or len(raw) > 48:
        h = hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:8]
        return (safe[:39] + "_" + h)[:48]
    return safe[:48]


def _norm_point(p: str) -> str:
    """归一化一条要点：去掉首尾空白与句末标点。

    否则「喜欢简洁的回答」和「喜欢简洁的回答。」会被当成两条重复记进去。
    """
    return (p or "").strip().strip("。.；;，,、！!？?　 -•*")


def _split_items(text: str) -> list:
    return [p.strip() for p in re.split(r"[；;\n]", text or "") if p.strip()]


# 长期记忆超上限时，**最早写入的这几条不参与淘汰**。
# 它们恰恰是姓名、身份、专业这类最稳定的档案，也恰好是最早写进来的。
_TRIM_PROTECT = 5


def _trim(text: str, cap: int, protect: int = 0) -> str:
    """超上限时从**最早**的条目开始丢（新的更重要）。

    ⚠️ 单条就超长时**不能**一路 pop 到底：那样会返回空串，而 set_short
    看到空串会把整个会话的记忆删掉（`shorts.pop(sid)`）。实测一个 1500 字的
    单条要点进来，该对话的短期记忆直接消失。超长就截断保留。

    ⚠️⚠️ `protect`：给**前 N 条豁免权**。淘汰方向是"丢最早的"，
    而最早的那几条正是姓名/身份/专业这类**最该保住的档案**。
    实测（2026-11-18，长期记忆灌到 14975 字）：
    「用户姓名：陈宇桐」和「用户专业：人工智能技术应用」**双双被挤掉**，
    第一条变成了「填充条目0452」—— 用户看到的现象就是"我的名字怎么没了"。
    """
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    items = _split_items(text)
    head = items[:protect] if protect > 0 else []
    body = items[protect:] if protect > 0 else list(items)
    while body and len("；".join(head + body)) > cap:
        body.pop(0)
    if not body:
        kept = "；".join(head)
        if len(kept) <= cap:
            if protect > 0:
                logger.warning("记忆已到上限 %d 字，可淘汰的条目已全部丢完，"
                               "仅保留最早的 %d 条稳定档案", cap, len(head))
            return kept
        return text[:cap]
    out = "；".join(head + body)
    if len(out) > cap:
        logger.warning("记忆超上限 %d 字且压不下去（当前 %d）", cap, len(out))
    return out


def _tokenize(text):
    tokens = []
    for ch in re.findall(r"[\u4e00-\u9fa5_a-zA-Z0-9]+", (text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fa5]+", ch):
            for i in range(len(ch) - 1):
                tokens.append(ch[i:i + 2])
            if len(ch) == 1:
                tokens.append(ch)
        else:
            tokens.append(ch)
    return [w for w in tokens if w not in _STOP_WORDS and w.strip()]


# =====================================================================
#  长期记忆（全局唯一一份，跨对话共享）
# =====================================================================
def get_long() -> str:
    d = _read_json(LONG_FILE, {})
    return str((d or {}).get("content") or "") if isinstance(d, dict) else ""


def get_long_doc() -> dict:
    d = _read_json(LONG_FILE, {})
    if not isinstance(d, dict):
        d = {}
    return {"content": str(d.get("content") or ""),
            "updated_at": float(d.get("updated_at") or 0)}


@_locked
def set_long(content: str) -> dict:
    content = _trim(content, LONG_CAP, protect=_TRIM_PROTECT)
    doc = {"content": content, "updated_at": _now()}
    _write_json(LONG_FILE, doc)
    return doc


@_locked
def merge_long(point: str) -> bool:
    """把一条要点并入长期记忆（追加 + 去重），返回是否真的写入了。"""
    point = _norm_point(point)
    if not point:
        return False
    cur = get_long()
    if _already(cur, point):
        return False
    flip = _neg_flip(cur, point)
    if flip >= 0:
        items = _split_items(cur)
        items[flip] = point
        set_long("；".join(items))
        return True
    sup = _supersede(cur, point)
    if sup is not None:
        set_long(sup)
        return True
    new = (cur.rstrip("；;。 \n") + "；" + point) if cur.strip() else point
    set_long(new)
    return True


# 「含否定字、但不是否定」的常见词：地名 / 食品 / 技术词 / 固定搭配。
# 不剔掉它们，`_neg_key` 会把「无锡」「无糖」「无线」「无人机」整句判成否定句，
# 于是「喜欢无锡」和「不喜欢无锡」被判成**同为否定** → 纠正识别不出来，
# 两条互相矛盾的说法并存。实测（2026-11-18）：
#   merge「用户不喜欢无锡的湿冷天气」+「用户喜欢无锡的湿冷天气」
#   → 正文「用户不喜欢…；用户喜欢…」两句并存，用户的纠正等于没生效。
_NEG_FALSE = ("无锡", "无糖", "无线", "无人机", "无数", "无论", "无效", "无关", "无限",
              "无疑", "无损", "无奈", "无所谓", "未来", "非常", "别人", "别的",
              "区别", "类别", "级别", "性别", "告别", "识别", "别墅", "不错",
              "不断", "不仅", "不但", "不少", "不久", "不同", "不论", "不光",
              "没关系", "没错", "没准", "没意思")


def _neg_strip(s: str) -> str:
    """先剔掉「含否定字但其实不是否定」的词。"""
    t = s or ""
    for w in _NEG_FALSE:
        t = t.replace(w, "")
    return t


def _neg_count(s: str) -> int:
    """否定标记的**个数**（剔掉假否定词之后）。"""
    t = _neg_strip(s)
    return sum(t.count(w) for w in ("不", "没", "无", "未", "别"))


def _neg_key(s: str) -> bool:
    """这条要点是不是否定性表述（只用来判断"两条说的是不是同一件事"）。

    「未来」「非常」里的"未/非"不是否定，先剔掉 —— 否则「未来打算考研」
    会被当成否定句，和「计划考研」判成两件事，白白记两条。
    ⚠️ 地名/技术词里的「无」（无锡、无糖、无线、无人机…）同样不是否定，
    见 `_NEG_FALSE` 的说明。
    """
    t = _neg_strip(s)
    return any(w in t for w in ("不", "没", "无", "未", "别"))


def _already(body: str, point: str) -> bool:
    """判断要点是否已在文段里（完全重复 / 被现有条目涵盖）。

    ⚠️ 必须**先比否定状态**再比字面包含。原来只做子串判断，实测：
    已有「用户不喜欢咖啡」，新要点「喜欢咖啡」会被判成"已经在里面了"，
    于是一条**纠正**被静默丢弃 —— 用户改了口径，记忆却永远停在旧说法上。
    """
    if not body:
        return False
    pn = _neg_key(point)
    for e in _split_items(body):
        e = _norm_point(e)
        if not e:
            continue
        if point == e:
            return True
        if _neg_key(e) != pn:
            continue              # 一个肯定一个否定，不是同一条
        if point in e:
            # ⚠️ 纯前缀不算涵盖：「条目1」被「条目10」包含，但两者是不同的东西。
            # 实测（2026-11-18）60 条编号要点只落下 55 条 —— 缺的正是
            # 1~9 这类"是别的编号前缀"的条目。真实场景：已记「2026年计划」，
            # 新来「2026年计划表模板」是另一件事，不能因为含前缀就丢掉。
            if _prefix_only(point, e):
                continue
            return True           # 新要点被旧条目涵盖，确实已经有了
    return False


def _prefix_only(short: str, long_: str) -> bool:
    """short 是 long_ 的前缀扩展，且扩展部分是数字/字母（编号），不是补充说明。

    「计划考网络工程师」→「计划考网络工程师，报名费800元」：扩展以「，」开头，
    是补充信息，应当替换（返回 False）；
    「条目1」→「条目10」：扩展以数字开头，是另一个编号，不是补充（返回 True）。
    """
    pos = (long_ or "").find(short)
    if pos < 0:
        return False
    tail = long_[pos + len(short):]
    return bool(tail) and (tail[0].isdigit() or tail[0].isalpha())


def _bare(s: str) -> str:
    """去掉否定标记和尾部语气词后的字面（只用于"是不是同一件事的正反说法"判断）。

    ⚠️ 尾部语气词必须去：实测「打算考四级」→「不打算考四级**了**」，
    不去"了"的话两边 bare 一个是「打算考四级」、一个是「打算考四级了」，
    判不成同一件事，于是两条**并存**（用户的否定没生效）。
    """
    # ⚠️ 先剔掉「含否定字但不是否定」的词（无锡/无糖/无线…），
    # 否则「喜欢无锡」的 bare 会变成「喜欢锡」，和「不喜欢无锡」的「喜欢锡」虽然能对上，
    # 但跟「喜欢上海」这类就全乱了；更关键的是 `_neg_count` 要按剔完的结果数。
    t = _neg_strip(s)
    # ⚠️ 复合否定词必须**先整词删**：只删单字「没」会剩下「有每天跑步」，
    # 和「每天跑步」对不上，实测这一条纠正就漏了。
    # ⚠️ 只有「没有」需要整词删（否则剩个「有」）。
    # 而「不想/不会/不能」**不能**整词删 —— 那会把「想/会/能」也删掉，
    # 于是「想换工作」和「不想换工作」的 bare 对不上（实测 3 条纠正全漏）。
    for w in ("没有", "不", "没", "无", "未", "别"):
        t = t.replace(w, "")
    t = t.strip().rstrip("了啦呢吧啊呀哦的嘛")
    return t.strip()


def _neg_flip(body: str, point: str):
    """在正文里找「同一件事、但肯定/否定相反」的那一条，返回下标；没有返回 -1。

    ⚠️ 修的是"改了口径却并存两句"（2026-11-18 实测）：
    `_already` 遇到否定状态不同会 `continue`（本意是别把纠正当成重复丢掉），
    但跳过之后 merge_* 只会**追加**，于是正文里同时留着
    「用户不喜欢咖啡」和「用户喜欢咖啡」—— 模型读到两条互相矛盾的，
    等于用户的纠正**从来没生效过**。
    正确做法：认出这是同一件事的反面，**原地替换**。
    """
    pb = _bare(point)
    if not pb or len(pb) < 3:
        return -1                      # 太短（「咖啡」）容易误配，宁可不改
    pc = _neg_count(point)
    for i, e in enumerate(_split_items(body)):
        en = _norm_point(e)
        if not en:
            continue
        # ⚠️ 判据不能再用「`_neg_key` 不同」——
        # 那是个布尔值，一旦句子里含有「无」的地名/技术词（无锡/无糖/无线），
        # 正反两句都会被判成否定 → 永远认不出这是纠正。
        # 改成：**去掉否定词之后正文相同**，且**否定标记个数不同** → 就是同一件事的正反说法。
        if _bare(en) != pb:
            continue
        if _neg_count(en) == pc:
            continue
        return i
    return -1


def _supersede(body: str, point: str):
    """新要点**完整包含**某条旧要点且更长 → 返回替换后的正文；否则 None。

    修的是"加信息反而丢信息"：原来「计划考网络工程师」遇到
    「计划考网络工程师，报名费800元」会被 _already 判成重复、整条丢掉，
    费用就永远记不上。这种情况应该**原地替换**成信息更全的那条。
    """
    items = _split_items(body)
    for i, e in enumerate(items):
        en = _norm_point(e)
        if not en or en == point:
            continue
        if en in point and len(point) > len(en) and _neg_key(en) == _neg_key(point):
            # ⚠️ 同样是纯前缀的问题：已有「并发条目1」，新来「并发条目10」，
            # 会被当成"同一件事的更详细版本"把旧条目**原地替换掉**。
            # 实测 3 线程各写 60 条编号要点，最终只剩 55 条（被吃了 5 条）。
            if _prefix_only(en, point):
                continue
            items[i] = point
            return "；".join(items)
    return None


# =====================================================================
#  短期记忆（每个对话一份，互相独立）
# =====================================================================
def _load_shorts() -> dict:
    d = _read_json(SHORT_FILE, {})
    return d if isinstance(d, dict) else {}


def get_short_doc(sid: str) -> dict:
    sid = _safe_sid(sid)
    if not sid:
        return {"content": "", "updated_at": 0}
    it = _load_shorts().get(sid) or {}
    return {"content": str(it.get("content") or ""),
            "updated_at": float(it.get("updated_at") or 0)}


def get_short(sid: str) -> str:
    return get_short_doc(sid)["content"]


@_locked
def set_short(sid: str, content: str) -> dict:
    sid = _safe_sid(sid)
    if not sid:
        return {"content": "", "updated_at": 0}
    # ⚠️ 短期记忆也必须保护最早的几条。
    # 淘汰方向是"丢最早的"，而最早写进去的正是**这件事本身是什么**
    # （「正在做的项目：考勤打卡系统；目标是下周三前交付」）。
    # 实测（2026-11-18，塞到 998 字 / 上限 1000）：项目名和目标**双双消失**，
    # 第一条变成「过程要点23：这是一条排查记录」——
    # 用户看到的就是"聊着聊着它忘了我到底在做什么"。
    # 长期记忆早就加了 _TRIM_PROTECT，短期这里漏了。
    content = _trim(content, SHORT_CAP, protect=3)
    shorts = _load_shorts()
    # 时间戳只取一次：原来落盘用 _now()、返回值又调一次 _now()，
    # 两者差几毫秒，调用方拿返回值去比对文件永远对不上。
    ts = _now()
    if content:
        shorts[sid] = {"content": content, "updated_at": ts}
    else:
        shorts.pop(sid, None)       # 清空就不占地方
    _write_json(SHORT_FILE, shorts)
    return {"content": content, "updated_at": ts if content else 0}


@_locked
def merge_short(sid: str, point: str) -> bool:
    point = _norm_point(point)
    if not point or not _safe_sid(sid):
        return False
    cur = get_short(sid)
    if _already(cur, point):
        return False
    flip = _neg_flip(cur, point)
    if flip >= 0:
        items = _split_items(cur)
        items[flip] = point
        set_short(sid, "；".join(items))
        return True
    sup = _supersede(cur, point)
    if sup is not None:
        set_short(sid, sup)
        return True
    new = (cur.rstrip("；;。 \n") + "；" + point) if cur.strip() else point
    set_short(sid, new)
    return True


@_locked
def delete_short(sid: str) -> bool:
    """对话被删除时调用，免得攒一堆孤儿短期记忆。"""
    sid = _safe_sid(sid)
    shorts = _load_shorts()
    if sid in shorts:
        shorts.pop(sid, None)
        _write_json(SHORT_FILE, shorts)
        return True
    return False


# =====================================================================
#  改写已有条目（目标的进展更新 / 作废旧信息）
# =====================================================================
# 为什么需要这一层：原来只有 merge_*（**只会追加**，遇到重复就整条丢掉）。
# 但"目标/计划"这类信息天生是要变化的 ——
#   3 月记下「打算考四级」，6 月用户说「四级过了」，12 月又说「准备考六级」。
# 只会追加的话，档案里会堆出互相矛盾的几句（"打算考四级"和"四级已过"并存），
# 模型读到就不知道该信哪句。所以必须支持**原地改**和**删除**。
def _grams(text: str, n: int = 2) -> set:
    """中文短句的 n 元字符组（去掉空白）。"""
    t = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _overlap(a: str, b: str) -> float:
    """两条要点的相似度（0~1）：互相包含 → 1.0，否则取「字符序列比例」和「2元组覆盖度」的较大值。

    为什么不能只用 SequenceMatcher（踩过）：
    模型改写措辞时字序会变，SequenceMatcher 会明显低估 ——
    实测「考网络工程师」与「已报名网络工程师考试，2027年3月开班，正在看教材」
    的序列比例只有 **0.40**，刚好卡在匹配阈值下方，导致改写失败、变成新增一条。
    换成 2 元组覆盖度后同一对比是 0.80，能正确认出"说的是同一件事"。
    """
    a, b = _norm_point(a), _norm_point(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a, b).ratio()
    ga, gb = _grams(a), _grams(b)
    # 分母取**较短**那一侧：问的是"较短的那句有多少能在另一句里找到"。
    # 取较长一侧会把长条目稀释掉（实测同一条对比 0.17 vs 0.80）。
    cov = len(ga & gb) / max(1, min(len(ga), len(gb)))
    return max(ratio, cov)


def _locate(body: str, hint: str):
    """在记忆正文里找与 hint 最像的那一条，返回 (下标, 条目列表, 相似度)。

    ⚠️ 定位词太短时**只认完全相同的那一条**。`_overlap` 里"被包含"直接给 1.0，
    于是删「用户」会把「用户姓名：陈宇桐」整条删掉（实测），改「喜欢」也可能
    改到毫不相干的一条上。宁可改不到，也不能改错。

    但一刀切到"长度<4 全部拒绝"又太狠（实测）：`old="考四级"` 定位不到
    「打算考四级」、`old="陈宇桐"` 定位不到「用户姓名：陈宇桐」，
    于是 forget 永远删不掉、update 只能退化成**追加** ——
    记忆里就出现「打算考四级」和「四级不考了」并存，正是"记了却不更新"的来源。
    折中：**3 个字**的定位词，若只出现在**唯一一条**里，就认它。
    """
    items = _split_items(body)
    hint_n = _norm_point(hint)
    best_i, best_s = -1, 0.0
    for i, e in enumerate(items):
        sc = _overlap(e, hint)
        if sc > best_s:
            best_i, best_s = i, sc
    if best_i >= 0 and len(hint_n) < 4 and _norm_point(items[best_i]) != hint_n:
        if len(hint_n) >= 3:
            hits = [i for i, e in enumerate(items) if hint_n in _norm_point(e)]
            if len(hits) == 1:
                return hits[0], items, 1.0
        return -1, items, 0.0
    return best_i, items, best_s


MATCH_THRESHOLD = 0.5           # 低于它就认为"不是同一条"，宁可不改也不乱改


@_locked
def update_long(old_hint: str, new_point: str, threshold: float = MATCH_THRESHOLD,
                append_missing: bool = True) -> str:
    """把长期记忆里的某一条**改成新内容**。

    返回：replaced（改成功）/ added（没找到旧条目，改为追加）/ skipped（没变化或内容为空）
    append_missing=False 时，找不到旧条目就返回 "missing"（不追加）——
    调用方想"先在这里改，改不到再换别处"时用得上。
    """
    new_point = _norm_point(new_point)
    if not new_point:
        return "skipped"
    cur = get_long()
    if not cur.strip():
        return ("added" if merge_long(new_point) else "skipped") if append_missing else "missing"
    i, items, score = _locate(cur, old_hint)
    if i < 0 or score < threshold:
        if not append_missing:
            return "missing"
        return "added" if merge_long(new_point) else "skipped"
    if _norm_point(items[i]) == new_point:
        return "skipped"                     # 和原句一样，不用动
    items[i] = new_point
    set_long("；".join(items))
    return "replaced"


@_locked
def update_short(sid: str, old_hint: str, new_point: str,
                 threshold: float = MATCH_THRESHOLD,
                 append_missing: bool = True) -> str:
    """短期记忆版的 update_long。"""
    sid = _safe_sid(sid)
    new_point = _norm_point(new_point)
    if not sid or not new_point:
        return "skipped"
    cur = get_short(sid)
    if not cur.strip():
        return ("added" if merge_short(sid, new_point) else "skipped") if append_missing else "missing"
    i, items, score = _locate(cur, old_hint)
    if i < 0 or score < threshold:
        if not append_missing:
            return "missing"
        return "added" if merge_short(sid, new_point) else "skipped"
    if _norm_point(items[i]) == new_point:
        return "skipped"
    items[i] = new_point
    set_short(sid, "；".join(items))
    return "replaced"


@_locked
def drop_long(point: str, threshold: float = 0.5) -> bool:
    """删掉长期记忆里最像 point 的那一条（用户明确说"不做了/弄错了"时用）。"""
    cur = get_long()
    if not cur.strip():
        return False
    i, items, score = _locate(cur, point)
    if i < 0 or score < threshold:
        return False
    items.pop(i)
    set_long("；".join(items))
    return True


@_locked
def drop_short(sid: str, point: str, threshold: float = 0.5) -> bool:
    sid = _safe_sid(sid)
    if not sid:
        return False
    cur = get_short(sid)
    if not cur.strip():
        return False
    i, items, score = _locate(cur, point)
    if i < 0 or score < threshold:
        return False
    items.pop(i)
    set_short(sid, "；".join(items))
    return True


# =====================================================================
#  注入上下文
# =====================================================================
CTX_LONG_CAP = 6000     # 注入上下文时长期记忆的上限（字）


def _ctx_trim_long(lng: str, cap: int = CTX_LONG_CAP,
                   query: str = "", sid: str = "") -> str:
    """长期记忆过长时，注入**头部稳定档案 + 尾部最新内容**。

    ⚠️ 原来是**全文注入**。长期记忆上限 15000 字，实测灌到 14975 字时
    `build_context` 返回 15064 字 —— 而这块在 main.py 里被算进 `budget_sys`
    （`sys_prompt + mem_ctx + rag_ctx`），于是**聊天历史被裁掉一大半**：
    用户看到的现象是"聊到一半它不记得刚才说什么了"，根子在这里而不在记忆本身。
    现在给一个上限，超了按头尾摘录并明确告知可以检索。
    """
    if len(lng) <= cap:
        return lng
    # ⚠️ 必须**按条目边界**切，不能直接 `lng[:n]` / `lng[-n:]`。
    # 记忆条目是用 `；` 拼起来的，硬切会把一条要点劈成两半：
    # 实测（2026-11-18，10698 字）头段末尾停在「第5条：这是一个用于测试截断行为的条目内容」，
    # 尾段从「右的样子；第289条」开始 —— 模型读到的是两条**残缺事实**，
    # 比不注入更糟（它会把半句话当真）。
    #
    # ⚠️⚠️ 而且**不能只做头尾摘录**。`build_context(sid, query=…)` 一直是带 query 的，
    # 但这里从来没接过 —— 于是注入的内容**和用户当前问什么完全无关**。
    # 实测（2026-11-18，10255 字）：超限后注入 6110 字，其中 **174 条是无关填充**，
    # 换一个查询（「什么时候考网络工程师」vs「我的专业是什么」）注入内容**一字不差**。
    # 这块被算进 budget_sys，等于用 6000 字挤掉聊天历史，换来一堆无关信息。
    # 现在按「头部稳定档案 → 与当前问题相关的条目 → 最新条目」挑，省略的才真的省略。
    items = _split_items(lng)
    if len(items) < 2:
        return lng[:cap]
    picked, used = [], 0

    def _take(it):
        nonlocal used
        if not it or it in picked:
            return False
        if used + len(it) + 1 > cap:
            return False
        picked.append(it)
        used += len(it) + 1
        return True

    # 1) 头部稳定档案（姓名/身份/专业 —— 每轮都用得上，必须给）
    head_budget = max(cap // 4, 200)
    for it in items:
        if used + len(it) + 1 > head_budget:
            break
        _take(it)

    # 2) 与当前问题相关的条目（向量检索，只取长期记忆那几条）
    n_rel = 0
    if query:
        try:
            from . import memvec
            # levels=("长期记忆",)：只让长期记忆参与，别让本对话的短期条目占掉名额
            # （原来取 12 条再自己筛，短期条目一多就筛出 0 条，这一步白做）。
            for h in memvec.search(query, sid=sid, top_k=12,
                                   with_transcripts=False, levels=("长期记忆",)):
                if h.get("level") != "长期记忆":
                    continue
                if _take((h.get("content") or "").strip()):
                    n_rel += 1
        except Exception:
            logger.debug("silent-except", exc_info=True)

    # 3) 剩余空间补**最新**条目（从尾部往前）。
    # ⚠️ 但**不能把 cap 填满**：cap 是"上限"，不是"目标"。
    # 实测（2026-11-18）：头部 + 相关条目只占 1600 字，尾部补足却一路填到 6090 字，
    # 其中 4500 字是与当前问题无关的填充 —— 这块算进 budget_sys，等于用 4500 字
    # 把聊天历史挤掉，换来一堆用不上的内容。宁可少注入，也要把 context 留给对话。
    tail_budget = used + int(cap * 0.35)
    for it in reversed(items):
        if used + len(it) + 1 > tail_budget:
            break
        _take(it)

    if len(picked) >= len(items):
        return lng
    # 日志要能看出「凭什么挑了这几条」
    logger.warning("长期记忆 %d 字，超过注入上限 %d：相关条目 %d 条，"
                   "共注入 %d/%d 条（头部档案 + 相关 + 最新补足）",
                   len(lng), cap, n_rel, len(picked), len(items))
    return ("；".join(picked)
            + "\n…（记忆较长，其余 %d 条未逐条列出；用户问到具体某条时用 search_memory 查）…"
            % max(0, len(items) - len(picked)))


def build_context(sid: str, query: str = "") -> str:
    """拼装注入 system prompt 的记忆：长期（全局）+ 短期（本对话）。"""
    lng = get_long().strip()
    sht = get_short(sid).strip()
    if not lng and not sht:
        return ""
    parts = ["【记忆】以下是已经知道的用户信息，回答时**直接用**，不要再问一遍。"
             "有新信息用 remember 工具补充（是**追加**，不用重写旧的）："]
    if lng:
        # query 一路传下去：超长时才能按「当前问的是什么」挑相关条目，
        # 而不是把中间一大段无关内容也塞进上下文（见 _ctx_trim_long 的说明）。
        parts.append(f"【长期记忆 · 所有对话通用】\n"
                     f"{_ctx_trim_long(lng, query=query, sid=_safe_sid(sid))}")
    if sht:
        parts.append(f"【短期记忆 · 仅本次对话】\n{sht}")
    return "\n\n".join(parts)


# =====================================================================
#  检索（供 search_memory 工具）
# =====================================================================
def search_all(query: str, sid: str = "", top_k: int = 5) -> list:
    """检索记忆 —— **条目级**，只返回命中的那几条。

    改造前：整块正文比一次、整块正文吐回去（长期记忆上限 15000 字、现在 67 条，
    命中一条就返回全部），既塞 context 又定位不到具体哪条。
    改造后：交给 memvec 做「语义向量 + 关键词覆盖率」的条目级混合检索，
    无关时返回空（宁缺毋滥，不拿无关记忆污染回答）。
    memvec 任何异常都退回到条目级关键词，功能不会整个挂掉。
    """
    q = (query or "").strip()
    if not q:
        return []
    # ⚠️ sid 必须**先归一化**再进检索层。索引里存的 `it["sid"]` 是 `_safe_sid`
    # 之后的（短期记忆落盘的 key 就是 safe 过的），而调用方传进来的是**原始**
    # session id（tools.py 直接取 context["session"]）。
    # 一旦原始 id 含 `[A-Za-z0-9_-]` 以外的字符（空格、中文、点号…），
    # memvec.search 里 `it["sid"] != sid` 就永远成立 → **本对话的短期记忆被全部过滤掉**。
    # 实测（2026-11-18）：sid="2026-11-18 22:50:34"，查「线程安全的问题」，
    # 传 safe sid 能召回（vec 0.5874），传原始 sid **完全召回不到** ——
    # 而且因为 memvec 返回空，还会掉进 _keyword_items 兜底（门槛 0.5 更严），
    # 结果就是"刚才说的事它想不起来了"。
    sid = _safe_sid(sid) if sid else ""
    try:
        from . import memvec
        hits = memvec.search(q, sid=sid, top_k=top_k)
        if hits:
            return hits
    except Exception as e:
        _LOG.warning("记忆向量检索失败，退化为关键词：%s", e)
    return _keyword_items(q, sid, top_k)


def _keyword_items(query: str, sid: str = "", top_k: int = 5) -> list:
    """兜底：条目级关键词检索（没有向量模型 / 向量层出错时用）。"""
    q = set(_tokenize(query or ""))
    if not q:
        return []
    # 空 sid 等价于 NO_SESSION_SID（与 remember 的写入兜底、memvec.search 的过滤口径一致）。
    # 原来这里 `if sid else ""` —— 空 sid 时短期记忆**完全不参与**兜底检索，
    # 而向量层不可用时这是唯一的检索路径，等于"刚记下就问它、它说没找到"。
    mine = sid or NO_SESSION_SID
    out = []
    for label, body, s in (("长期记忆", get_long(), ""),
                           ("短期记忆", get_short(mine), mine)):
        for e in _split_items(body or ""):
            e = _norm_point(e)
            if len(e) < 2:
                continue
            cov = len(q & set(_tokenize(e))) / max(len(q), 1)
            if cov >= 0.5:
                out.append({"level": label, "section": s, "content": e,
                            "source": "auto", "score": round(cov, 4), "cov": cov})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_k]


def search_memories(query: str, top_k: int = 5) -> list:
    return search_all(query, "", top_k)


# =====================================================================
#  占用 / 释放
# =====================================================================
def usage() -> dict:
    shorts = _load_shorts()
    used = sum(1 for v in shorts.values() if str((v or {}).get("content") or "").strip())
    s_chars = sum(len(str((v or {}).get("content") or "")) for v in shorts.values())
    return {
        "long_bytes": _size(LONG_FILE), "long_chars": len(get_long()),
        "short_bytes": _size(SHORT_FILE), "short_chars": s_chars,
        "short_used": used,
        "long_cap": LONG_CAP, "short_cap": SHORT_CAP,
    }


@_locked
def release(scope: str = "short", session_id: str = "") -> dict:
    """释放记忆占用。

    scope=short  : 清空所有对话的**短期记忆**（长期记忆保留）
    scope=session: 只清某个对话的短期记忆
    长期记忆**不参与释放** —— 那是最该保住的东西，只能由用户显式清空。
    """
    if scope == "session":
        sid = _safe_sid(session_id)
        n = len(get_short(sid))
        delete_short(sid)
        return {"freed_chars": n, "scope": "session"}
    shorts = _load_shorts()
    freed = sum(len(str((v or {}).get("content") or "")) for v in shorts.values())
    _write_json(SHORT_FILE, {})
    return {"freed_chars": freed, "scope": "short", "cleared": len(shorts)}


# =====================================================================
#  老数据迁移（一次性，只做加法，绝不删用户数据）
# =====================================================================
def migrate_legacy(active_sid: str = "") -> dict:
    """把历史版本的记忆文件搬进新结构。

    历史版本有三代：
      A. memory_doc.json    多分区（工作背景/个人背景/当前关注/近期动态…）
      B. memory/conversations.json  每对话一块
      C. memory/global.json         极简全局偏好
    统一搬到：稳定信息 → 长期记忆；对话相关 → 该对话的短期记忆。
    老文件一律**改名保留**（.migrated），不删 —— 迁移出错还能翻回来。
    """
    moved = {"long": 0, "short": 0}

    # ---- A. 多分区版 ----
    if os.path.exists(LEGACY_DOC_FILE):
        doc = _read_json(LEGACY_DOC_FILE, None)
        secs = doc.get("sections") if isinstance(doc, dict) else doc
        if not isinstance(secs, list):
            secs = []
        for s in secs:
            if not isinstance(s, dict):
                continue
            body = str(s.get("content") or "").strip()
            if not body:
                continue
            title = str(s.get("title") or "").strip()
            for piece in _split_items(body):
                # 身份/偏好/约定/项目 → 长期；"近期动态"这类时效性内容 → 当前对话短期
                target = "short" if any(k in title for k in ("近期", "动态", "当前关注")) else "long"
                if target == "long":
                    if merge_long(piece if not title else f"{title}：{piece}"):
                        moved["long"] += 1
                elif active_sid:
                    if merge_short(active_sid, piece):
                        moved["short"] += 1
        _rename(LEGACY_DOC_FILE)

    # ---- C. 早期"全局偏好" → 长期记忆 ----
    if os.path.exists(LEGACY_GLOBAL_FILE):
        g = _read_json(LEGACY_GLOBAL_FILE, {})
        body = str((g or {}).get("content") or "").strip() if isinstance(g, dict) else ""
        for piece in _split_items(body):
            if merge_long(piece):
                moved["long"] += 1
        _rename(LEGACY_GLOBAL_FILE)

    # ---- B. 早期"每对话一块" → 各自的短期记忆 ----
    if os.path.exists(LEGACY_CONV_FILE):
        convs = _read_json(LEGACY_CONV_FILE, {})
        if isinstance(convs, dict):
            for sid, it in convs.items():
                body = str((it or {}).get("content") or "").strip()
                if not body:
                    continue
                for piece in _split_items(body):
                    if merge_short(sid, piece):
                        moved["short"] += 1
        _rename(LEGACY_CONV_FILE)

    return {"migrated": bool(moved["long"] or moved["short"]), **moved}


def _rename(path: str) -> None:
    try:
        os.replace(path, path + ".migrated")
    except Exception:
        logger.debug("silent-except", exc_info=True)


# =====================================================================
#  历史会话归档（transcript）
# =====================================================================
def _transcript_path(session_id: str):
    """归档文件路径。

    ⚠️ 和 _safe_sid 同一个坑：字符替换 + 截断都会碰撞。
    实测（2026-11-18）两个只有末尾不同的长 id（前 40 字符相同）
    **落到了同一个 .jsonl**，两个对话的历史混在一起 —— 释放时会被
    当成同一个对话一起沉淀，内容和归属全乱。
    同样只在"确实改动过原文"时补哈希，纯安全字符的 id 文件名保持不变。
    """
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    raw = session_id or "session"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if safe != raw or len(raw) > 40:
        h = hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:8]
        safe = (safe[:31] + "_" + h)[:40]
    else:
        safe = safe[:40]
    return os.path.join(TRANSCRIPT_DIR, f"{safe}.jsonl")


# 单条归档消息的上限。超过就**保留头尾**，而不是只留前面 2000 字。
# 实测（2026-11-18）：4008 字的消息进来，原来的 `[:2000]` 把结尾的
# 「结尾重点」直接切掉了 —— 而用户真正想问的结论往往就在最后一句。
_TRANSCRIPT_MSG_CAP = 2000


def _clip_transcript_msg(text: str) -> str:
    text = str(text or "")
    if len(text) <= _TRANSCRIPT_MSG_CAP:
        return text
    half = _TRANSCRIPT_MSG_CAP // 2
    return text[:half] + "…（中间省略）…" + text[-half:]


def save_transcript(session_id: str, messages: list):
    """把一轮对话追加进归档（jsonl）。

    ⚠️ 必须**先拼成一个字符串再一次 write**，并且整体拿锁。
    原来逐行 `f.write()` 且没有锁：6 个线程各写 40 轮，实测只落下
    473/480 行，还产生了 **1 行坏 JSON** —— 两个线程的写入交错在了一行里。
    坏行会被 `read_transcript` 静默跳过，等于那几条对话**永久消失**，
    而释放归档时正是靠这些行来沉淀记忆的。
    """
    if not session_id:
        return
    path = _transcript_path(session_id)
    buf = []
    for msg in messages:
        if msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content") or ""
        if not content or len(str(content)) < 2:
            continue
        buf.append(json.dumps({"role": msg["role"],
                               "content": _clip_transcript_msg(content),
                               "ts": _now()}, ensure_ascii=False) + "\n")
    buf.append(json.dumps({"role": "sep", "ts": _now()}, ensure_ascii=False) + "\n")
    with _MEM_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(buf))
    _index_touch()      # 历史会话也是记忆的一部分，新内容要能被检索到


def read_transcript(session_id: str, limit: int = 200) -> list:
    path = _transcript_path(session_id)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("role") in ("user", "assistant") and (o.get("content") or "").strip():
                    out.append({"role": o["role"], "content": o["content"]})
    except Exception:
        return []
    return out[-limit:]


def purge_transcript(session_id: str) -> bool:
    """删除某个对话的归档底稿（用户删掉对话时必须调）。

    ⚠️⚠️ 为什么非有不可：删对话时 `sessions.delete` 删了历史、`delete_short`
    删了短期记忆，但归档 `.jsonl` **一直留在磁盘上**。而 `search_transcripts`
    是直接扫目录的 —— 于是"用户以为已经删掉"的对话仍能被历史会话检索召回。
    实测（2026-11-18）：删掉含「身份证号 440101199001011234」的对话后，
    查「身份证」照样把那两句原话返回回来；归档文件也一直占着磁盘
    （`data/transcript/` 里已经攒了 235 个文件）。
    删对话 = 用户要它消失，底稿不删就不算删干净。
    """
    path = _transcript_path(session_id)
    ok = False
    with _MEM_LOCK:
        try:
            if os.path.exists(path):
                os.remove(path)
                ok = True
        except Exception:
            # 删不掉不影响对话本身已经删掉这件事，只记日志
            logger.warning("删除归档失败（不影响其它功能）：%s", path, exc_info=True)
    if ok:
        _index_touch()      # 归档也是检索源，删了要通知索引重建
    return ok


def transcript_stats() -> dict:
    files, size = 0, 0
    if os.path.isdir(TRANSCRIPT_DIR):
        for fn in os.listdir(TRANSCRIPT_DIR):
            if fn.endswith(".jsonl"):
                files += 1
                size += _size(os.path.join(TRANSCRIPT_DIR, fn))
    return {"files": files, "bytes": size}


def search_transcripts(query: str, limit: int = 3) -> list:
    q = _tokenize(query or "")
    if not q:
        return []
    hits = []
    if not os.path.isdir(TRANSCRIPT_DIR):
        return hits
    for fn in os.listdir(TRANSCRIPT_DIR):
        if not fn.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(TRANSCRIPT_DIR, fn), "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        buffer = []
        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("role") == "sep":
                buffer = []
                continue
            content = obj.get("content", "")
            if set(q) & set(_tokenize(content)):
                buffer.append(content)
            else:
                buffer = []
            if len(buffer) >= 2:
                hits.append({"level": "历史会话", "content": " ".join(buffer[-4:])[:500],
                             "source": "transcript"})
                buffer = []
                break
        if len(hits) >= limit:
            break
    return hits
