# -*- coding: utf-8 -*-
"""跨任务抄近道：开新任务时，自动翻出「历史上最像的那 2~3 次」并抽出可复用要素。

【他为什么需要】（用户原话转述）
    上周让 AI 做过一份带特定格式的 Word 报告，这周又要做一份类似的，
    结果得把「章节结构 / 语气 / 格式要求」重新描述一遍 ——
    其实上次那套完全能复用，但没人帮他自动找出来。

【数据从哪来】
    长期记忆（memory）里只有被提炼过的「要点」，找不回
    「上次那份报告的具体提示词、章节骨架、跑过的脚本」。
    真正还留着这些东西的是**会话历史**（sessions，{role, content} 全量原文），
    所以这里按会话切「任务」，每个任务抽出三样可复用要素：

        · prompt   —— 当时用户是怎么描述这个任务的（第一句实质需求）
        · template —— 产出模板：助手回复里的章节骨架（标题 / 一二三… / 编号列表）
        · scripts  —— 用到的脚本：回复里的代码块（按语言标注）+ 提到的产出文件

【怎么算相似】
    任务文本向量化后与当前任务做余弦（走 backend/embed，和 papervec/taskvec 同一套），
    再混字面覆盖率做兜底：向量模型没装时**不能静默失效**，
    必须退化成「字面 + 2-gram」检索 —— 这条分支是有意保留的，
    否则没装 embedding 的机器上这个功能会「永远不弹提示条」且不报错。

【对外接口】
    find_similar(query, k=3, exclude_sid="")  →  {mode, items[], note}
    reuse_payload(task_id)                    →  点一下「复用」时要注入的完整文本
    stats()                                   →  自检 / 看板用的计数
"""
from __future__ import annotations

import logging
import re
import threading
import time

from . import sessions

logger = logging.getLogger(__name__)

try:                                     # 向量检索是「有则更好」，没有也要能跑
    import numpy as np
except Exception:                        # noqa: BLE001
    np = None

MAX_SESSIONS = 120        # 最多看多少个历史会话（新的优先）
MIN_MSGS = 2              # 只有一句「你好」的会话不算任务
MAX_PROMPT = 600
MAX_TEMPLATE = 1200
MAX_SCRIPT = 700
MAX_SCRIPTS = 3
LIST_PREVIEW = 180        # 提示条上只显示这么长的提示词预览

# 关键词兜底模式的阈值（中文语料上调过：太松会把不相干的会话全捞出来）
KW_MIN_COV = 0.30
KW_MIN_GRAM = 1

_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(\S.{0,70})$")
_CN_NUM_RE = re.compile(
    r"^\s{0,3}(?:第[一二三四五六七八九十百]+[章节部分条讲]"
    r"|[一二三四五六七八九十]{1,3}[、.．)）]"
    r"|\d{1,2}[、.．)）])\s*\S.{0,70}$")
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.#-]*)[ \t]*\r?\n(.*?)```", re.S)
_FILE_RE = re.compile(
    r"[\w\-./\\一-龥]{1,80}\.(?:docx|xlsx|pptx|pdf|md|py|csv|txt|json)", re.I)
_SCRIPT_EXT = (".py", ".js", ".sh", ".bat", ".ps1")

_CACHE = {"sig": "", "tasks": [], "mat": None, "emb_sig": "", "emb_off": False}
_LOCK = threading.Lock()

# ---------------------------------------------------------------- 基础工具

def _clip(s, n) -> str:
    s = str(s or "").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _text_of(m) -> str:
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, list):              # 少数会话存的是分段数组
        c = " ".join(str(x) for x in c if isinstance(x, str))
    return str(c or "").strip()


def _role(m) -> str:
    return str(m.get("role") or "") if isinstance(m, dict) else ""


# ---------------------------------------------------------------- 要素抽取

def _structure_lines(text: str, limit: int = 14) -> list:
    """把一段成品正文压成「章节骨架」。

    只认三类行，别的都不收 —— 收多了提示条会变成一整篇报告：
        · Markdown 标题      `## 二、现状`
        · 中文序号小标题      `一、背景` / `2. 方法`
        · 短小标题行          `背景：`（含冒号、不超过 30 字）
    """
    out = []
    for raw in str(text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("```") or len(ln) > 90:
            continue
        m = _HEAD_RE.match(ln)
        if m:
            out.append("#" * len(m.group(1)) + " " + m.group(2).strip())
            continue
        if _CN_NUM_RE.match(ln):
            out.append(ln)
            continue
        if ln.endswith(("：", ":")) and 2 <= len(ln) <= 30:
            out.append(ln)
            continue
    return out[:limit]


def _paragraph_outline(text: str, limit: int = 8) -> list:
    """没有标题结构时的退化方案：每段取第一句当骨架。"""
    out = []
    for para in re.split(r"\n\s*\n", str(text or "")):
        p = " ".join(para.split())
        if len(p) < 6:
            continue
        first = re.split(r"(?<=[。！？；;!?])", p)[0]
        out.append(_clip(first, 40))
        if len(out) >= limit:
            break
    return out


_CODE_LANGS = {
    "py", "python", "js", "javascript", "ts", "typescript", "sh", "bash", "shell",
    "bat", "powershell", "ps1", "html", "css", "json", "yaml", "yml", "sql",
    "c", "cpp", "java", "go", "rust", "cs", "vb", "lua", "r", "php", "xml",
}
# 无语言标注的代码块：靠这些信号判断它到底是脚本还是「一段排版好的文字」
_CODE_HINT = re.compile(
    r"(?m)^\s*(?:def |class |import |from [\w.]+ import|#include|function |"
    r"const |let |var |print\(|echo |@echo|curl |pip |python |py |node |"
    r"subprocess|return |if \(|for \(|while \(|SELECT |CREATE |\$ )"
    r"|;[ \t]*$|\{\s*$|</?[a-z][\w-]*>")


def _scripts_of(text: str) -> list:
    """抽出回复里的代码块（= 这次任务实际用到的脚本）。

    ⚠️ 不能「凡是 ``` 就收」：模型的回复里经常用无语言标注的代码块
    做**对图片段/对齐列表**（实测：一段「待批准 ≥ 30 条 → 自动跳过」的说明
    被当成「脚本」塞进复用卡片，用户点一下得到一堆不是代码的东西）。
    所以无标注的块要过一遍代码特征；有明确语言的（py/js/bat…）直接收。
    """
    out = []
    for m in _FENCE_RE.finditer(str(text or "")):
        lang = (m.group(1) or "").strip().lower()
        code = m.group(2).strip("\n")
        if len(code.strip()) < 12:
            continue
        if lang not in _CODE_LANGS:
            if not _CODE_HINT.search(code[:600]):
                continue
            lang = lang or "code"
        out.append({"lang": lang, "code": _clip(code, MAX_SCRIPT)})
        if len(out) >= MAX_SCRIPTS:
            break
    return out


def _files_of(text: str) -> list:
    """回复里提到过的产出文件（Word/Excel/PPT/脚本…），去重保序。"""
    seen, out = set(), []
    for m in _FILE_RE.finditer(str(text or "")):
        name = m.group(0).replace("\\", "/").strip("./")
        base = name.rsplit("/", 1)[-1]
        if not base or base in seen or len(base) > 60:
            continue
        seen.add(base)
        out.append(base)
        if len(out) >= 8:
            break
    return out


def _pick_prompt(users: list) -> str:
    """「当时的提示词」= 用户第一次把需求说清楚的那句话。

    跳过「你好 / 在吗 / 继续」这类开场白：它们进了提示条毫无信息量。
    """
    weak = ("你好", "在吗", "hi", "hello", "继续", "开始", "好的", "嗯", "谢谢")
    for u in users:
        t = u.strip()
        if len(t) < 4 or t.lower() in weak:
            continue
        return _clip(t, MAX_PROMPT)
    return _clip(users[0], MAX_PROMPT) if users else ""


def _task_from_session(meta: dict, msgs: list):
    """一个会话 → 一个「历史任务」。抽不出可复用内容的返回 None。"""
    if not isinstance(msgs, list) or len(msgs) < MIN_MSGS:
        return None
    users = [t for t in (_text_of(m) for m in msgs if _role(m) == "user") if t]
    bots = [t for t in (_text_of(m) for m in msgs if _role(m) == "assistant") if t]
    if not users or not bots:
        return None

    prompt = _pick_prompt(users)
    if not prompt:
        return None

    # 骨架优先取最长的那条成品（通常是真正的交付物），不够再补其他回复
    ordered = sorted(bots, key=len, reverse=True)
    main_out = ordered[0]
    lines = _structure_lines(main_out)
    if len(lines) < 3:
        for other in ordered[1:3]:
            lines += [x for x in _structure_lines(other) if x not in lines]
        if len(lines) < 3:
            for x in _paragraph_outline(main_out):
                if x not in lines:
                    lines.append(x)
    template = "\n".join(lines[:14])

    scripts = []
    for t in ordered[:4]:
        for s in _scripts_of(t):
            if s["code"] not in [x["code"] for x in scripts]:
                scripts.append(s)
        if len(scripts) >= MAX_SCRIPTS:
            break
    scripts = scripts[:MAX_SCRIPTS]

    files = _files_of("\n\n".join(ordered[:4]))
    code_files = [f for f in files if f.lower().endswith(_SCRIPT_EXT)]

    title = str(meta.get("title") or "").strip() or _clip(prompt, 24)
    return {
        "id": str(meta.get("id") or ""),
        "title": _clip(title, 40),
        "when": str(meta.get("updated") or meta.get("created") or ""),
        "n_msgs": len(msgs),
        "prompt": prompt,
        "template": _clip(template, MAX_TEMPLATE),
        "scripts": scripts,
        "files": files,
        "code_files": code_files,
        "out_len": len(main_out),
    }


def _doc_text(t: dict) -> str:
    """喂给向量模型的文本：标题 + 提示词 + 骨架 + 脚本语言。"""
    parts = [t.get("title") or "", t.get("prompt") or "", t.get("template") or ""]
    langs = [s.get("lang") or "" for s in (t.get("scripts") or [])]
    if langs:
        parts.append("脚本：" + "、".join(sorted(set(langs))))
    return "\n".join(p_ for p_ in parts if p_)


# ---------------------------------------------------------------- 任务装载

def _sig_of(idx: list) -> str:
    return "|".join("%s:%s:%s" % (it.get("id"), it.get("updated"), it.get("count"))
                    for it in idx[:MAX_SESSIONS])


def load_tasks(force: bool = False) -> list:
    """所有历史任务（新的优先）。会话索引没变就复用上次扫出来的结果。

    为什么带缓存：这个函数会被提示条每次请求调用一次，
    每次都重新读几十个会话文件 + 重新算字面覆盖会很浪费；
    而会话索引里的 updated/count 一变就说明有新内容，缓存自动失效。
    """
    try:
        idx = sessions.list_sessions()
    except Exception as e:                              # noqa: BLE001
        logger.warning("[tasksim] 读会话索引失败：%s", e)
        return []
    if not isinstance(idx, list):
        return []
    clean = []
    for it in idx[:MAX_SESSIONS]:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        try:
            n = int(it.get("count") or 0)
        except Exception:                               # noqa: BLE001
            n = 0
        if n >= MIN_MSGS:
            clean.append(it)
    idx = clean
    sig = _sig_of(idx)
    with _LOCK:
        if not force and _CACHE["tasks"] and _CACHE["sig"] == sig:
            return _CACHE["tasks"]

    tasks = []
    for meta in idx:
        try:
            msgs = sessions.get_messages(meta["id"])
        except Exception:                               # noqa: BLE001
            continue
        t = _task_from_session(meta, msgs)
        if t:
            tasks.append(t)

    with _LOCK:
        if _CACHE["sig"] != sig:
            _CACHE["mat"] = None                        # 任务集变了，旧向量作废
            _CACHE["emb_sig"] = ""
        _CACHE["sig"] = sig
        _CACHE["tasks"] = tasks
    return tasks


# ---------------------------------------------------------------- 相似度

def _chars(s) -> set:
    return {c for c in str(s or "") if not c.isspace()}


def _coverage(qc: set, text: str) -> float:
    if not qc:
        return 0.0
    return len(qc & _chars(text)) / float(len(qc))


def _grams(s, n: int = 2) -> set:
    s = "".join(str(s or "").split())
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _matrix(tasks: list):
    """任务向量矩阵。拿不到就 None → 调用方走关键词分支（不许静默返回空）。"""
    if np is None or not tasks or _CACHE["emb_off"]:
        return None
    sig = _CACHE["sig"]
    with _LOCK:
        if _CACHE["mat"] is not None and _CACHE["emb_sig"] == sig:
            return _CACHE["mat"]
    t0 = time.time()
    try:
        from . import embed
        if embed.signature() == "none":
            _CACHE["emb_off"] = True
            return None
        mat = embed.embed_texts([_doc_text(t) for t in tasks])
        if mat is None or getattr(mat, "ndim", 0) != 2 or mat.shape[0] != len(tasks):
            _CACHE["emb_off"] = True
            return None
    except Exception as e:                              # noqa: BLE001
        logger.warning("[tasksim] 向量化失败，退化为关键词检索：%s", e)
        _CACHE["emb_off"] = True
        return None
    if time.time() - t0 > 20:                           # 太慢就别再试了
        _CACHE["emb_off"] = True
    with _LOCK:
        _CACHE["mat"] = mat
        _CACHE["emb_sig"] = sig
    return mat


def _query_vec(query: str, mat):
    if mat is None:
        return None
    try:
        from . import embed
        qv = embed.embed_texts([query], is_query=True)
    except Exception:                                   # noqa: BLE001
        return None
    if qv is None or getattr(qv, "shape", (0,))[0] != 1 or len(qv[0]) != mat.shape[1]:
        return None
    return (mat @ qv[0]).astype(np.float32)


def _public(t: dict, **extra) -> dict:
    """列表用的精简视图（提示条上只放得下两三行字）。"""
    it = {
        "id": t.get("id"),
        "title": t.get("title"),
        "when": t.get("when"),
        "prompt_preview": _clip(t.get("prompt"), LIST_PREVIEW),
        "has_template": bool((t.get("template") or "").strip()),
        "scripts": len(t.get("scripts") or []),
        "script_langs": sorted({(s.get("lang") or "code") for s in (t.get("scripts") or [])}),
        "files": (t.get("files") or [])[:4],
    }
    it.update(extra)
    return it


def find_similar(query: str = "", k: int = 3, exclude_sid: str = "",
                 allow_recent: bool = True) -> dict:
    """找和新任务最像的历史任务。

    query 为空（用户刚点「新建」还没打字）→ 返回**最近做过的**几个，
    并在 mode 里标明 "recent"，让前端文案说人话（不能谎称「相似」）。
    """
    try:
        k = max(1, min(int(k or 3), 5))
    except Exception:                                   # noqa: BLE001
        k = 3
    query = " ".join(str(query or "").split())
    explicit = bool(query)       # 用户到底有没有把新任务说出来（决定要不要谎称「相似」）
    tasks = [t for t in load_tasks() if t.get("id") != (exclude_sid or "")]

    if not tasks:
        return {"mode": "none", "items": [], "query": query,
                "note": "还没有可复用的历史任务（有内容的会话才会被收录）"}

    if not query:
        # 刚点「新建」还没打字：拿长期记忆里「这人平时在做什么」当软查询。
        # 记忆够具体时能直接命中相似任务（mode=similar），
        # 命中不了就老实退回「最近做过的」（mode=recent，前端文案会跟着改）。
        query = _memory_hint()
        if not query:
            if not allow_recent:
                return {"mode": "none", "items": [], "query": "",
                        "note": "缺少任务描述"}
            return {"mode": "recent", "query": "",
                    "items": [_public(t, score=None, why="最近做过") for t in tasks[:k]],
                    "note": "最近做过的任务，格式和脚本可以直接沿用"}

    qc = _chars(query)
    qg = _grams(query)
    mat = _matrix(tasks)
    vec = _query_vec(query, mat) if mat is not None else None

    picked = []
    for i, t in enumerate(tasks):
        doc = _doc_text(t)
        cov = _coverage(qc, doc)
        ghits = len(qg & _grams(doc)) if qg else 0
        if vec is not None:
            v = float(vec[i])
            if v >= 0.62:
                ok = True                       # 强语义，不要求字面重合
            elif v >= 0.45 and cov >= 0.34:
                ok = True
            elif v >= 0.40 and ghits >= 1 and cov >= 0.24:
                ok = True
            else:
                ok = False
            score = round(0.72 * v + 0.28 * cov, 4)
        else:
            v = 0.0
            gram_ratio = (ghits / float(max(1, len(qg)))) if qg else 0.0
            ok = (cov >= KW_MIN_COV and ghits >= KW_MIN_GRAM) or cov >= 0.62
            score = round(0.6 * cov + 0.4 * gram_ratio, 4)
        if not ok:
            continue
        why = _why(t, cov, ghits, v)
        picked.append(_public(t, score=score, sem=round(v, 4), cov=round(cov, 4),
                              why=why))

    picked.sort(key=lambda x: x.get("score") or 0, reverse=True)
    picked = picked[:k]
    if not picked and allow_recent:
        # 一条都没过阈值：宁可明说「没找到相似的」，也别把不相干的当相似推给用户；
        # 这里退回「最近做过」，前端会换成「最近做过」的文案。
        note = ("没找到足够像的旧任务，下面是最近做过的" if explicit
                else "按长期记忆没匹配到具体任务，下面是最近做过的")
        return {"mode": "recent", "query": query,
                "items": [_public(t, score=None, why="最近做过") for t in tasks[:k]],
                "note": note}
    return {"mode": "similar", "query": query, "items": picked, "note": ""}


def _memory_hint() -> str:
    """从长期记忆里捞一句「这人平时在做什么」，当新任务还没描述时的软查询。

    为什么这么绕：点「新建」的瞬间我们**不知道**他这次想做什么。
    但长期记忆里往往写着「主要工作是写带固定格式的季度报告」这类偏好，
    拿它去比历史任务，比直接推「最近做过的三条」准得多。
    记忆读不到 / 太短就返回空串，让调用方退回「最近做过的」。
    """
    try:
        from . import memory
        txt = memory.get_long() or ""
    except Exception:                                   # noqa: BLE001
        return ""
    txt = " ".join(str(txt).split())
    if len(txt) < 8:
        return ""
    # 只要尾部一段：记忆是追加式的，越靠后越贴近现在
    return _clip(txt[-400:], 400)


def _why(t: dict, cov: float, ghits: int, v: float) -> str:
    """一句话说清「凭什么说它像」—— 用户才不会觉得是乱推的。"""
    bits = []
    if v >= 0.5:
        bits.append("语义接近")
    if cov >= 0.4:
        bits.append("用词重合 %d%%" % int(round(cov * 100)))
    elif ghits >= 2:
        bits.append("有 %d 处相同措辞" % ghits)
    if (t.get("template") or "").strip():
        bits.append("带章节骨架")
    n = len(t.get("scripts") or [])
    if n:
        bits.append("%d 段脚本" % n)
    return "、".join(bits) or "历史任务"


# ---------------------------------------------------------------- 复用

def get_task(task_id: str):
    """按会话 id 取出完整的可复用要素（点「复用」时才需要全量内容）。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    try:                                   # 缓存里没有（比如刚存的新会话）→ 强制重扫一次
        for t in load_tasks(force=True):
            if t.get("id") == task_id:
                return t
    except Exception:                      # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return None


def reuse_payload(task_id: str, session_id: str = "") -> dict:
    """「复用它的格式/结构/脚本？」点下去之后要注入当前任务的全部内容。

    返回的 `inject` 就是前端填进输入框的正文：提示词 + 产出模板 + 脚本，
    用户可以直接改两行再发出去 —— 这样它自然进入这一轮的上下文，
    不需要改动 /api/chat 的主链路（那条链路里每一行都牵着回归断言）。
    """
    t = get_task(task_id)
    if not t:
        return {"ok": False, "error": "找不到这次历史任务（可能已被删除）"}

    blocks = []
    blocks.append("【复用旧任务「%s」%s】" % (
        t.get("title") or "", ("（" + t["when"] + "）") if t.get("when") else ""))
    if t.get("prompt"):
        blocks.append("· 上次的提示词：\n" + t["prompt"])
    if (t.get("template") or "").strip():
        blocks.append("· 上次的产出模板（章节结构，直接沿用这套骨架）：\n" + t["template"])
    else:
        blocks.append("· 上次的产出模板：（这次没留下明确章节结构，按语气与篇幅对齐即可）")
    if t.get("scripts"):
        lines = []
        for s in t["scripts"]:
            lines.append("```%s\n%s\n```" % (s.get("lang") or "", s.get("code") or ""))
        blocks.append("· 上次用到的脚本（可复用/改路径）：\n" + "\n".join(lines))
    else:
        blocks.append("· 上次用到的脚本：（这次没有留下脚本）")
    if t.get("files"):
        blocks.append("· 上次产出的文件：" + "、".join(t["files"][:6]))
    blocks.append("请按上面这套格式 / 结构 / 脚本，完成我这次的新需求：\n")

    payload = {
        "ok": True,
        "id": t.get("id"),
        "title": t.get("title"),
        "when": t.get("when"),
        "session_id": session_id or "",
        "prompt": t.get("prompt") or "",
        "template": t.get("template") or "",
        "scripts": t.get("scripts") or [],
        "files": t.get("files") or [],
        "inject": "\n\n".join(blocks),
    }
    return payload


def stats() -> dict:
    """自检 / 看板用的计数（不触发向量化，纯粹看数据面）。"""
    tasks = load_tasks()
    with_template = sum(1 for t in tasks if (t.get("template") or "").strip())
    with_script = sum(1 for t in tasks if t.get("scripts"))
    try:
        from . import embed
        emb = embed.signature()
    except Exception:                      # noqa: BLE001
        emb = "none"
    return {"tasks": len(tasks), "with_template": with_template,
            "with_script": with_script, "embed": emb,
            "embed_disabled": bool(_CACHE["emb_off"]),
            "cached": bool(_CACHE["tasks"])}

