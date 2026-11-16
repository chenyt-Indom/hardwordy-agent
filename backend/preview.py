# -*- coding: utf-8 -*-
"""记忆预告片（Preview）—— 今日预告模块

用户 2026-11-16 需求：
  · gather_today() 读 memory 未完成项 + scheduler 今日任务 + amap 天气 + 最近搜索；
  · render_preview() 生成 30 秒文案；
  · main.py 加 /api/preview/today；
  · 前端 index.html 加「今日预告」卡片，首次打开自动展开，可关闭并 localStorage 记日期。

设计原则：
  · **绝不抛异常**：任何一路数据源挂了都只是少一段，不能把整个预告搞崩；
  · **不调大模型**：30 秒文案用规则拼装，秒出、不烧 token、离线可用；
  · **可解释**：每段都带来源标记，用户能看出「这条是从哪来的」。
"""

import datetime
import json
import os
import re
import time

from . import config


def _now():
    return datetime.datetime.now()


def _today_key():
    """今天的日期键（YYYY-MM-DD），前端 localStorage 用它判断「今天是否已关闭」。"""
    return _now().strftime("%Y-%m-%d")


# ---------------- 数据源 1：记忆里的未完成项 ----------------
def _memory_pending(limit=6):
    """从长期 + 短期记忆里挑出「未完成 / 待办 / 计划中」的条目。

    记忆是文段式的（一行一条），所以按行扫关键词即可，不需要额外结构。
    关键词宁窄勿宽：只认明确的「待办 / 未完成 / 计划 / 要做 / 还没」这类，
    避免把「已完成」也捞进来。
    """
    out = []
    try:
        from . import memory
        texts = []
        # 长期记忆（跨对话）
        try:
            texts.append(str(memory.get_long() or ""))
        except Exception:
            pass
        # 短期记忆：所有会话都扫一遍（用户可能在不同对话里记了不同的事）
        # 注意：memory 模块没有 list_sessions()，会话列表由 memory.get_sessions()
        # 或 memory.list_conversations() 提供；这里做兼容探测，避免 AttributeError。
        try:
            _list_fn = None
            for _name in ("get_sessions", "list_conversations", "list_sessions"):
                _cand = getattr(memory, _name, None)
                if callable(_cand):
                    _list_fn = _cand
                    break
            sessions = _list_fn() if _list_fn else []
            for s in sessions or []:
                sid = s.get("id") if isinstance(s, dict) else str(s)
                if not sid:
                    continue
                try:
                    texts.append(str(memory.get_short(sid) or ""))
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        return out

    # 明确的「未完成」信号
    pend_words = ("待办", "未完成", "还没", "尚未", "计划", "要做", "待做",
                  "准备", "打算", "明天", "下周", "截止", "deadline", "TODO", "todo")
    # 明确的「已完成」信号 —— 命中就跳过这一行
    done_words = ("已完成", "做完了", "搞定了", "已解决", "已提交", "已发布", "已上线")

    seen = set()
    for t in texts:
        for line in str(t or "").splitlines():
            s = line.strip().lstrip("-·*•0123456789.、)） ")
            if not s or len(s) < 4:
                continue
            if any(w in s for w in done_words):
                continue
            if not any(w in s for w in pend_words):
                continue
            key = re.sub(r"\s+", "", s)[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(s[:120])
            if len(out) >= limit:
                return out
    return out


# ---------------- 数据源 2：定时任务今日要跑的 ----------------
def _scheduler_today(limit=6):
    """今天还会跑的定时任务（按下次运行时间排序）。

    scheduler.list_jobs() 每项大致有：name / enabled / every_minutes / at_time /
    next_run / last_run / last_status。这里只挑「启用中」且「今天还有下一次」的。
    """
    out = []
    try:
        from . import scheduler
        jobs = scheduler.list_jobs() or []
    except Exception:
        return out

    today = _now().strftime("%Y-%m-%d")
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if not j.get("enabled"):
            continue
        nxt = str(j.get("next_run") or "")
        name = str(j.get("name") or "未命名任务")
        # next_run 可能是「YYYY-MM-DD HH:MM」或 ISO；只认日期前缀
        if nxt and not nxt.startswith(today):
            continue
        when = nxt[11:16] if len(nxt) >= 16 else ""
        out.append({"name": name, "when": when})
        if len(out) >= limit:
            break
    return out


# ---------------- 数据源 3：天气（高德） ----------------
def _weather():
    """取本地天气。走项目已有的高德通道；没配 Key / 没网就返回 None。

    ⚠️ 这里**不抛异常**：天气只是锦上添花，挂了不该影响整条预告。
    """
    try:
        from . import web_tools
    except Exception:
        return None
    # 高德天气需要城市 adcode；项目里 map_amap_status 那套有缓存的城市信息。
    # 这里用最保守的方式：调 web_tools 里现成的天气函数（如果有）。
    fn = getattr(web_tools, "amap_weather", None)
    if not callable(fn):
        return None
    try:
        r = fn()
    except Exception:
        return None
    if not isinstance(r, dict) or not r.get("ok"):
        return None
    return {
        "city": str(r.get("city") or ""),
        "text": str(r.get("text") or r.get("weather") or ""),
        "temp": str(r.get("temp") or r.get("temperature") or ""),
        "tip": str(r.get("tip") or ""),
    }


# ---------------- 数据源 4：最近搜索 ----------------
def _recent_searches(limit=5):
    """最近搜过什么（从 actlog 里捞 web_search / map_search 的记录）。

    没有 actlog 就返回空 —— 这一路纯属「让预告更有人味」，缺了不影响。
    """
    out = []
    try:
        from . import actlog
        # actlog 模块没有 recent()；兼容探测常见的读取接口，避免 AttributeError。
        _read_fn = None
        for _name in ("tail", "get_recent", "list_recent", "read", "recent"):
            _cand = getattr(actlog, _name, None)
            if callable(_cand):
                _read_fn = _cand
                break
        if _read_fn is None:
            return out
        try:
            rows = _read_fn(200) or []
        except TypeError:
            rows = _read_fn() or []
    except Exception:
        return out
    seen = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or r.get("action") or "")
        if "search" not in kind.lower():
            continue
        q = str(r.get("detail") or r.get("query") or "").strip()
        if not q or len(q) < 2:
            continue
        key = q[:30]
        if key in seen:
            continue
        seen.add(key)
        out.append(q[:60])
        if len(out) >= limit:
            break
    return out


# ---------------- 汇总 ----------------
def gather_today():
    """把今天所有该知道的事收成一份结构化数据。

    返回：
      {
        "ok": True,
        "date": "2026-11-16",
        "weekday": "星期日",
        "now": "14:23",
        "pending": [...],      # 记忆里的未完成项
        "schedules": [...],    # 今天还要跑的定时任务
        "weather": {...}|None,
        "searches": [...],     # 最近搜过什么
        "counts": {...},       # 各类条数，前端做徽标用
      }
    """
    now = _now()
    pending = _memory_pending()
    schedules = _scheduler_today()
    weather = _weather()
    searches = _recent_searches()
    return {
        "ok": True,
        "date": now.strftime("%Y-%m-%d"),
        "weekday": "一二三四五六日"[now.weekday()],
        "now": now.strftime("%H:%M"),
        "pending": pending,
        "schedules": schedules,
        "weather": weather,
        "searches": searches,
        "counts": {
            "pending": len(pending),
            "schedules": len(schedules),
            "searches": len(searches),
            "has_weather": bool(weather),
        },
    }


# ---------------- 文案 ----------------
def render_preview(data=None):
    """把 gather_today() 的结果拼成一段「30 秒能读完」的口播文案。

    结构（固定四段，缺的段自动跳过）：
      ① 开场：日期 + 天气一句
      ② 待办：记忆里还没做完的事（最多 3 条，多了说「还有 N 条」）
      ③ 日程：今天还要跑的定时任务
      ④ 收尾：一句轻量的提醒 / 鼓励

    为什么不用大模型：这段文案要**秒出**（用户一打开界面就要看到），
    而且内容全是结构化事实，规则拼装比模型更准、更省、更可控。
    """
    d = data if isinstance(data, dict) else gather_today()
    if not d.get("ok"):
        return "今天的信息暂时取不到，稍后再看看。"

    parts = []

    # ① 开场
    head = "今天是 %s 月 %s 日，星期%s" % (
        int(d["date"][5:7]), int(d["date"][8:10]), d.get("weekday") or "")
    w = d.get("weather")
    if w and (w.get("text") or w.get("temp")):
        bits = []
        if w.get("city"):
            bits.append(w["city"])
        if w.get("text"):
            bits.append(w["text"])
        if w.get("temp"):
            bits.append("%s°C" % w["temp"])
        head += "，" + " ".join(bits)
        if w.get("tip"):
            head += "，" + w["tip"]
    parts.append(head + "。")

    # ② 待办
    pend = d.get("pending") or []
    if pend:
        show = pend[:3]
        seg = "记忆里还有 %d 件事没做完：" % len(pend)
        seg += "；".join(show)
        if len(pend) > len(show):
            seg += "；另外还有 %d 条" % (len(pend) - len(show))
        parts.append(seg + "。")

    # ③ 日程
    sch = d.get("schedules") or []
    if sch:
        items = []
        for s in sch[:3]:
            if s.get("when"):
                items.append("%s 点「%s」" % (s["when"], s.get("name") or ""))
            else:
                items.append("「%s」" % (s.get("name") or ""))
        seg = "今天还有 %d 个定时任务：" % len(sch) + "、".join(items)
        if len(sch) > 3:
            seg += " 等"
        parts.append(seg + "。")

    # ④ 收尾
    if pend or sch:
        parts.append("需要我帮你先处理哪一件，直接说就行。")
    else:
        parts.append("今天没有待办和定时任务，想聊点什么都可以。")

    return "".join(parts)


# ---------------- 对外入口 ----------------
def today():
    """给 /api/preview/today 用：一次返回结构化数据 + 文案 + 日期键。"""
    data = gather_today()
    return {
        "ok": True,
        "date": data.get("date") or _today_key(),
        "data": data,
        "text": render_preview(data),
    }
