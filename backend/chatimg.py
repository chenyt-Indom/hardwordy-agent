"""对话图片台账 —— 让用户上传的图片**跨轮仍然可见**。

## 为什么要有这个文件（2026-11-13 用户报）

用户原话：「我之前需要传图片上去时图片只能当轮使用，下一轮模型就查不到了」。

根因是**两处都没存**：

1. 前端 `history.push({role:"user", content: promptText})` —— 历史里**只有文字**，
   图片走独立的 `images_b64` 字段单独发一次；
2. 后端 `attach_images = model_images if _round == 0 else None` ——
   图片只挂在本轮（round 0）的最后一条 user 消息上。

⇒ 下一轮请求里，图片**在任何地方都不存在了**。模型不是"忘了"，是**真的没收到**。

## 做法

每轮把「用户消息内容哈希 → 落盘路径」记进 `data/chat_images/index.json`；
构建上下文时按哈希把图**挂回对应的那条历史消息**。

为什么按「哈希」而不是按「第几条消息」对齐：
    · 前端恢复会话、用户删消息、切换分支都会让序号错位；
    · 哈希跟着内容走，只要那句话还在，图就跟得回去。

两个通道都原生支持逐条附图：
    · Ollama  → `messages[i]["images"] = [b64, ...]`（见 ollama_client.chat）
    · DeepSeek→ `messages[i]["content"] = [{"type":"image_url",...}]`（见 main.chat_deepseek）

## 预算（图片很贵，一张约 1030 token）

只有**最近 `RECENT_FULL` 条**带图消息挂真实图片；
更早的只留一行「文件路径」提示 —— 模型真要处理时可以直接按路径取
（`edit_image(source=路径)` / 放进 PPT），不必让用户重传。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import threading
import time

from . import config

logger = logging.getLogger(__name__)

_INDEX_NAME = "chat_images/index.json"
_LOCK = threading.Lock()

# 最近几条「带图的历史消息」挂真实图片（每张约 1030 token）
RECENT_FULL = 2
# 单条消息最多挂几张
MAX_PER_MSG = 2
# 每个会话最多留几条台账（超出丢最旧）
MAX_PER_SESSION = 60
# 台账最多留多少个会话
MAX_SESSIONS = 30


def _index_path() -> str:
    return config.data(_INDEX_NAME)


def _hash(text: object) -> str:
    """用户消息内容的短哈希（12 位十六进制）。"""
    t = str(text or "").strip()
    if not t:
        return ""
    return hashlib.md5(t.encode("utf-8", "ignore")).hexdigest()[:12]


def _load() -> dict:
    """读台账。结构：{"sessions": {sid: [{"h":..., "p":[...], "ts":...}]}}"""
    try:
        p = _index_path()
        if not os.path.isfile(p):
            return {"sessions": {}}
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("sessions"), dict):
            return {"sessions": {}}
        return d
    except Exception:
        logger.warning("读取对话图片台账失败（按空处理）", exc_info=True)
        return {"sessions": {}}


def _save(d: dict) -> None:
    try:
        p = _index_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        logger.warning("写入对话图片台账失败（不影响本轮）", exc_info=True)


def record(session_id: str, text: object, paths: list) -> None:
    """记下「这句话配了这些图」。paths 是落盘后的本地绝对路径。"""
    sid = str(session_id or "").strip() or "_default"
    good = [str(p) for p in (paths or []) if p and os.path.isfile(str(p))]
    if not good:
        return
    h = _hash(text)
    if not h:
        return
    with _LOCK:
        d = _load()
        sess = d.setdefault("sessions", {})
        ent = sess.setdefault(sid, [])
        # 同一句话重复发、或同一轮被重试 → 更新而不是追加
        for e in ent:
            if e.get("h") == h:
                e["p"] = good[:MAX_PER_MSG]
                e["ts"] = time.time()
                break
        else:
            ent.append({"h": h, "p": good[:MAX_PER_MSG], "ts": time.time()})
        if len(ent) > MAX_PER_SESSION:
            del ent[: len(ent) - MAX_PER_SESSION]
        # 会话太多时丢最久没动的
        if len(sess) > MAX_SESSIONS:
            order = sorted(sess.items(), key=lambda kv: max(
                (e.get("ts", 0) for e in kv[1]), default=0))
            for k, _ in order[: len(sess) - MAX_SESSIONS]:
                sess.pop(k, None)
        _save(d)


def _load_b64(path: str, max_side: int = 1024) -> str:
    """把落盘图片读成**纯 base64（无 data: 前缀）**，长边压到 max_side。

    Ollama 的 `messages[].images` 要的是无前缀 base64；
    DeepSeek 的 image_url 由调用方自己补 `data:image/png;base64,`。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            return ""
        try:
            from PIL import Image as _Image
            with _Image.open(io.BytesIO(raw)) as im:
                if max(im.size) > max_side:
                    im2 = im.convert("RGB")
                    ratio = max_side / float(max(im2.size))
                    im2 = im2.resize((max(1, int(im2.width * ratio)),
                                      max(1, int(im2.height * ratio))))
                    buf = io.BytesIO()
                    im2.save(buf, format="JPEG", quality=88)
                    raw = buf.getvalue()
        except Exception:
            pass          # 不是图片 / 没装 PIL：原样返回
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        logger.warning("读取历史图片失败：%s", path, exc_info=True)
        return ""


def attach(messages: list, session_id: str,
           recent: int = RECENT_FULL) -> list:
    """把历史图片挂回**对应的那条 user 消息**，返回挂上去的 b64 列表。

    · 最近 `recent` 条带图消息 → 挂真实图片（供模型直接"看"）
    · 更早的 → 只追加一行文件路径提示（模型要处理时按路径取，别让用户重传）
    · **最后一条 user 消息不处理** —— 那是本轮，图片由 images_b64 走原路挂，
      这里再挂一次会重复占 token。

    返回值只用来算 token 预算，调用方不必再管。
    """
    if not messages:
        return []
    sid = str(session_id or "").strip() or "_default"
    ent = (_load().get("sessions") or {}).get(sid) or []
    if not ent:
        return []

    by_h: dict = {}
    for e in ent:
        by_h.setdefault(str(e.get("h") or ""), []).append(e)

    u_idx = [i for i, m in enumerate(messages)
             if isinstance(m, dict) and str(m.get("role") or "") == "user"]
    if len(u_idx) <= 1:
        return []                     # 只有本轮一条，没有"历史"可挂
    targets = u_idx[:-1]              # 排除本轮

    used: dict = {}
    matched: list = []                # [(msg_index, [paths])]
    for i in targets:
        h = _hash(messages[i].get("content"))
        if not h:
            continue
        lst = by_h.get(h) or []
        k = used.get(h, 0)
        if k >= len(lst):
            continue
        used[h] = k + 1
        ps = [p for p in (lst[k].get("p") or []) if p]
        if ps:
            matched.append((i, ps))
    if not matched:
        return []

    full = {i for i, _ in matched[-recent:]} if recent > 0 else set()
    got_b64: list = []
    for i, paths in matched:
        msg = messages[i]
        if i in full:
            imgs = []
            for p in paths[:MAX_PER_MSG]:
                b = _load_b64(p)
                if b:
                    imgs.append(b)
            if imgs:
                msg["images"] = imgs
                got_b64.extend(imgs)
                continue
        # 更早的（或读图失败）：只留路径提示
        note = ("（这一轮用户附了图片，原件已存本地：%s —— "
                "需要处理时可直接按该路径使用（如 edit_image 的 source、放进文档/PPT），"
                "不要让用户重新上传。）" % "、".join(paths[:3]))
        cur = msg.get("content")
        if isinstance(cur, str):
            msg["content"] = cur + "\n\n" + note
        elif isinstance(cur, list):
            cur.append({"type": "text", "text": "\n\n" + note})
    return got_b64


def status(session_id: str = "") -> dict:
    """给界面/调试看：这个会话记了几张图。"""
    d = _load().get("sessions") or {}
    if session_id:
        ent = d.get(str(session_id)) or []
        return {"session": session_id, "turns": len(ent),
                "images": sum(len(e.get("p") or []) for e in ent)}
    return {"sessions": len(d),
            "turns": sum(len(v) for v in d.values()),
            "images": sum(len(e.get("p") or []) for v in d.values() for e in v)}


def clear(session_id: str) -> bool:
    """清掉某个会话的台账（用户说「忘掉那些图」时用）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _LOCK:
        d = _load()
        sess = d.get("sessions") or {}
        if sid not in sess:
            return False
        sess.pop(sid, None)
        _save(d)
        return True
