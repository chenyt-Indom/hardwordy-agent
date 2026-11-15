# -*- coding: utf-8 -*-
"""对话级「工具动作账本」：这一轮到底动过什么，下一轮还查得到。

为什么需要
----------
会话历史里只存了「用户说的话 + 助手最后那段正文」（见 sessions.py 与前端
persistSession）。工具调用的**真实记录** —— 改了哪个文件、跑了什么命令、
搜了什么 —— 一条都没落盘。

于是出现 2026-11-09 用户报的那个问题：
    用户：「你刚刚改了什么？」
    模型：「没改任何东西。」（手里确实没有任何证据）
更糟的是任务做到一半被轮次上限打断时，正文往往只剩半句
（「先找合适的插入位置：」），连"改了哪个文件"都没写进历史。

做法
----
1) 每次工具执行完 → record() 追加一行（工具名 + 关键参数摘要 + 成败 + 时间）；
2) 每轮对话 → render() 把最近若干条压成一小段，注入到**最后一条用户消息**上
   （和记忆、交接卡同一位置：不动系统提示，不破坏前缀缓存）；
3) 账本按会话隔离，只留最近 MAX_KEEP 条，落盘在 data/actlog/，重启后仍在。

⚠️ 只记**摘要**，绝不落盘工具原文 —— selfdev_write 的 content 动辄几十万字符。
"""
import json
import os
import time

from . import config

MAX_KEEP = 120          # 每个会话最多留多少条（滚动覆盖）
SHOW = 12               # 注入时最多列几条

# 从工具参数里挑"一眼能看出在干什么"的字段
_BRIEF_KEYS = ("rel", "path", "file", "filename", "project", "query", "keyword",
               "url", "cmd", "command", "src", "source", "subject", "title",
               "name", "act", "kind")


def _dir() -> str:
    d = os.path.join(config.data_root(), "data", "actlog")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _path(session: str) -> str:
    safe = "".join(c for c in str(session or "") if c.isalnum() or c in "-_")
    return os.path.join(_dir(), (safe or "default") + ".jsonl")


def _read(path: str) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except Exception:
        pass
    return out


def _write(path: str, rows: list) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        pass


def _brief(args) -> str:
    """从参数里挑一个最能说明"动了什么"的字段，压成一行。"""
    if not isinstance(args, dict):
        return ""
    for k in _BRIEF_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            return "%s=%s" % (k, v[:80])
    for k in ("index", "count", "n"):
        v = args.get(k)
        if isinstance(v, int):
            return "%s=%d" % (k, v)
    return ""


def _first_line(text, cap: int = 80) -> str:
    s = " ".join(str(text or "").split())
    return s[:cap]


def record(session: str, name: str, args=None, ok: bool = True,
           result: str = "", note: str = "") -> None:
    """记一次工具动作。任何异常都吞掉 —— 账本坏了绝不能影响对话本身。"""
    try:
        row = {"t": time.strftime("%m-%d %H:%M"),
               "n": str(name or "?")[:40],
               "a": _brief(args),
               "ok": bool(ok)}
        if note:
            row["m"] = _first_line(note, 90)
        elif not ok:
            row["m"] = _first_line(result, 90)
        path = _path(session)
        rows = _read(path)
        # 连续重复（同一个工具 + 同一个参数）合并成一条，带次数 ——
        # 否则"读了 30 次同一个文件"会把整段上下文吃光。
        if (rows and rows[-1].get("n") == row["n"]
                and rows[-1].get("a") == row["a"]):
            rows[-1]["c"] = int(rows[-1].get("c") or 1) + 1
            rows[-1]["t"] = row["t"]
            rows[-1]["ok"] = row["ok"]
        else:
            rows.append(row)
        _write(path, rows[-MAX_KEEP:])
    except Exception:
        pass


def render(session: str, n: int = SHOW) -> str:
    """生成要注入的工具动作摘要；没有记录就返回空串。"""
    rows = _read(_path(session))
    if not rows:
        return ""
    lines = []
    for r in rows[-max(1, int(n)):]:
        seg = "- [%s] %s" % (r.get("t") or "", r.get("n") or "?")
        if r.get("a"):
            seg += " " + str(r["a"])
        c = int(r.get("c") or 1)
        if c > 1:
            seg += "（×%d）" % c
        if r.get("ok") is False:
            seg += "  ⚠️失败"
        if r.get("m"):
            seg += "  → " + str(r["m"])
        lines.append(seg)
    return ("【本对话真实执行过的工具动作（最近 %d 条）】\n" % len(lines)
            + "\n".join(lines)
            + "\n⚠️ 上面是**真实执行记录**，不是你的猜测。用户问「刚刚改了什么 / "
              "做到哪了」，就照它说清**文件 + 动作 + 成败**；账本里没有的事，"
              "就如实说「这一轮没动过」，不要编。")


def clear(session: str) -> None:
    try:
        os.remove(_path(session))
    except Exception:
        pass
