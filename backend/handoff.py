# -*- coding: utf-8 -*-
"""自我迭代交接卡（handoff）：重启之后，还记得刚才改了什么、聊到哪。

为什么需要
----------
改完自己的源码、重启之后是**新进程**：会话历史虽然落在 sessions/，
但长会话会被上下文预算裁掉早期部分；而"这次改了哪几个文件、为什么改"
只留在备份文件名和聊天记录里 —— 重启后的新一轮很可能答不上
"我们刚才在做什么"。

做法（三步，互不依赖）
--------------------
1) 每次自开发工具**写盘成功** → `record_change()` 追加一条
   「文件 + 一句话说明 + 时间」（同一文件连续改动会合并成一条）；
2) **重启前**（模型请求重启 / 用户点「立即重启」）→ `snapshot()`
   把改动清单 + 最近几轮对话要点一起落盘；
3) 之后**每一轮**对话 → `render()` 生成一小段交接卡，注入到
   **最后一条用户消息**上（不能放进系统提示：系统提示一变，后面的
   工具定义全部作废，首字会慢十几秒 —— 见 main.py 里 _attach_live_ctx 的说明）。

存储：data/handoff/last.json（超过 KEEP_DAYS 天不再注入，文件留着不删）
"""
import json
import os
import time

from . import config

DIR = os.path.join(config.data("data"), "handoff")
LAST = os.path.join(DIR, "last.json")

KEEP_DAYS = 7        # 超过这么久就不再注入
MAX_CHANGES = 20     # 落盘保留的改动条数
SHOW_CHANGES = 8     # 注入时最多列几条
MAX_DIALOG = 6       # 注入时最多带几轮对话（一问一答算两条）
TEXT_CAP = 90        # 单条对话注入时的字数上限


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean(s, cap: int = 200) -> str:
    return " ".join(str(s or "").split())[:cap]


# =====================================================================
#  读写
# =====================================================================
def _read() -> dict:
    try:
        with open(LAST, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = LAST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST)          # 原子替换，避免写一半断电把交接卡写坏
    except Exception:
        pass                           # 交接卡写失败**不能影响**正常对话/改代码


# =====================================================================
#  1) 记录一次改动（自开发工具写盘成功后调用）
# =====================================================================
def record_change(rel: str, note: str = "", added: int = 0, removed: int = 0,
                  act: str = "write") -> None:
    rel = _clean(rel, 120)
    if not rel:
        return
    d = _read()
    ch = d.get("changes")
    if not isinstance(ch, list):
        ch = []
    note = _clean(note, 160)
    try:
        added = int(added or 0)
        removed = int(removed or 0)
    except Exception:
        added = removed = 0

    # 同一个文件连续改了好几次 → 合并成一条（否则清单里全是同一个文件名）
    if ch and ch[-1].get("rel") == rel:
        it = ch[-1]
        try:
            it["added"] = int(it.get("added") or 0) + added
            it["removed"] = int(it.get("removed") or 0) + removed
        except Exception:
            pass
        if note:
            it["note"] = note
        it["act"] = act
        it["time"] = _now_str()
    else:
        ch.append({"rel": rel, "note": note, "added": added,
                   "removed": removed, "act": act, "time": _now_str()})

    d["changes"] = ch[-MAX_CHANGES:]
    d["updated"] = time.time()
    _write(d)


def record_from_tool(r: dict, act: str = "write", note: str = "") -> None:
    """给 tools.py 的 `_selfdev_ui` 用的**一行入口**：自己吞掉所有异常。

    交接卡是锦上添花，绝不能因为它让"改代码"这个主流程报错。
    """
    try:
        d = (r or {}).get("diff") or {}
        record_change(str((r or {}).get("rel") or ""), note=note, act=act,
                      added=d.get("added") or 0, removed=d.get("removed") or 0)
    except Exception:
        pass


# =====================================================================
#  2) 重启前快照（把对话要点一起存下来）
# =====================================================================
def _dialog_digest(session: str) -> list:
    """从会话历史里取最近几轮，压成「角色 + 一段话」。"""
    if not session:
        return []
    try:
        from . import sessions as _ss
        msgs = _ss.get_messages(session) or []
    except Exception:
        return []
    out = []
    for m in msgs[-(MAX_DIALOG * 2):]:
        role = str(m.get("role") or "")
        text = _clean(m.get("content"), 400)
        if role not in ("user", "assistant") or not text:
            continue
        out.append({"role": role, "text": text})
    return out[-(MAX_DIALOG * 2):]


def snapshot(session: str = "", reason: str = "") -> dict:
    """重启前调用：把「改动清单 + 最近几轮对话」落盘。

    返回落盘后的内容（方便调用方打日志）。任何异常都不往外抛 ——
    交接卡是"锦上添花"，绝不能因为它导致重启失败。
    """
    try:
        d = _read()
        if session:
            d["session"] = _clean(session, 64)
        if reason:
            d["reason"] = _clean(reason, 120)
        d["ts"] = time.time()
        d["restarted_at"] = _now_str()
        dlg = _dialog_digest(str(d.get("session") or ""))
        if dlg:
            d["dialog"] = dlg
        _write(d)
        return d
    except Exception:
        return {}


# =====================================================================
#  3) 每轮注入
# =====================================================================
def _fmt_change(it: dict) -> str:
    rel = str(it.get("rel") or "")
    note = str(it.get("note") or "").strip()
    try:
        added = int(it.get("added") or 0)
        removed = int(it.get("removed") or 0)
    except Exception:
        added = removed = 0
    line = "- `%s`" % rel
    if added or removed:
        line += "（+%d -%d 行）" % (added, removed)
    if note:
        line += "：%s" % note
    t = str(it.get("time") or "")
    if t:
        line += "　[%s]" % t[5:16]      # 只留「月-日 时:分」
    return line


def render(session: str = "") -> str:
    """生成要注入的交接卡文本；没有可注入的内容就返回空串。"""
    d = _read()
    if not d:
        return ""
    try:
        ts = float(d.get("ts") or 0)
    except Exception:
        ts = 0
    if not ts or (time.time() - ts) > KEEP_DAYS * 86400:
        return ""                          # 太久以前的重启，别再占上下文

    changes = [c for c in (d.get("changes") or []) if isinstance(c, dict)]
    dialog = [m for m in (d.get("dialog") or []) if isinstance(m, dict)]
    if not changes and not dialog:
        return ""

    lines = ["【自我迭代交接卡 · 上次重启 %s】"
             % (d.get("restarted_at") or "（时间未知）")]
    if d.get("reason"):
        lines.append("原因：%s" % str(d["reason"])[:120])

    if changes:
        lines.append("· 那次重启前改过的文件（最近 %d 个）：" % min(len(changes), SHOW_CHANGES))
        lines.extend(_fmt_change(c) for c in changes[-SHOW_CHANGES:])

    if dialog:
        lines.append("· 重启前我们在聊（最近 %d 轮）：" % (len(dialog) // 2 or 1))
        for m in dialog[-MAX_DIALOG * 2:]:
            who = "用户" if m.get("role") == "user" else "你"
            lines.append("  %s：%s" % (who, _clean(m.get("text"), TEXT_CAP)))

    lines.append(
        "⚠️ 这张卡是**上一次会话的背景**，用来接上前文（用户可能正在接着问刚才的事）；"
        "和用户当前这句话冲突时**以当前为准**。不要主动复述这张卡，"
        "除非用户问「刚才聊到哪 / 改了些什么」。")
    return "\n".join(lines)
