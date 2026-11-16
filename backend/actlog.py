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
from collections import Counter

try:
    from . import config
except ImportError:  # 允许作为顶层模块被直接导入（如验收断言）
    import config
import logging
logger = logging.getLogger(__name__)

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
        logger.debug("silent-except", exc_info=True)
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
        logger.debug("silent-except", exc_info=True)
    return out


def _write(path: str, rows: list) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        logger.debug("silent-except", exc_info=True)


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
           result: str = "", note: str = "", evidence=None) -> None:
    """记一次工具动作。任何异常都吞掉 —— 账本坏了绝不能影响对话本身。

    evidence（2026-11-17 改动证据卡）：可选 dict，形如
      {"fail_kind": "...", "count": N, "regression": "pass|fail|skip"}
    会原样落到账本行里，前端动作卡片据此渲染「针对哪个真实失败 + 计数 + 回归结果」。
    """
    try:
        row = {"t": time.strftime("%m-%d %H:%M"),
               "n": str(name or "?")[:40],
               "a": _brief(args),
               "ok": bool(ok)}
        if isinstance(evidence, dict) and evidence:
            # 只留白名单字段，避免把大对象塞进账本
            ev = {}
            for k in ("fail_kind", "count", "regression", "evidence"):
                if k in evidence and evidence[k] is not None:
                    ev[k] = evidence[k]
            if ev:
                row["ev"] = ev
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
        logger.debug("silent-except", exc_info=True)


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


def _load_events(path: str = "") -> list:
    """读 task.json 里的 events 列表；读不到/格式不对一律返回 []。"""
    try:
        p = path or os.path.join(config.data_root(), "data", "self_iterate", "task.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                ev = d.get("events") or []
            elif isinstance(d, list):
                ev = d
            else:
                ev = []
            return ev if isinstance(ev, list) else []
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return []


def _event_ts(e) -> float:
    """把事件里的时间戳尽量解析成 epoch 秒；解析不出来返回 0。

    兼容几种常见写法：ts 是数字（秒/毫秒）、ts 是 'YYYY-mm-dd HH:MM:SS'、
    t 是 'mm-dd HH:MM'（actlog 那种短格式，缺年份按今年算）。
    """
    if not isinstance(e, dict):
        return 0.0
    v = e.get("ts") or e.get("t") or e.get("time") or e.get("at") or 0
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000.0 if x > 1e11 else x
    s = str(v or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            t = time.strptime(s, fmt)
            if fmt.startswith("%m"):
                t = time.struct_time((time.localtime().tm_year,) + tuple(t)[1:])
            return time.mktime(t)
        except Exception:
            continue
    return 0.0


def fail_stats(window: int = 100, path: str = "") -> dict:
    """聚合自动迭代的失败类型（fail_kind），按次数降序返回 {fail_kind: count}。

    数据源：task.json 的 events 里带 fail_kind 字段的事件。
    取最近 window 条（按事件顺序），忽略没有 fail_kind 的事件。
    path 可显式指定 task.json 路径（测试用）；为空时用默认位置。
    任何异常都吞掉 —— 看板坏了不能影响主流程。
    """
    try:
        n = int(window)
    except (TypeError, ValueError):
        n = 100
    if n <= 0:
        n = 100
    events = _load_events(path)
    kinds = []
    for e in events:
        if isinstance(e, dict):
            k = e.get("fail_kind")
            if isinstance(k, str) and k.strip():
                kinds.append(k.strip())
    if n and len(kinds) > n:
        kinds = kinds[-n:]
    c = Counter(kinds)
    return dict(c.most_common())


def aggregate_fail_kinds(since_hours: int = 24, path: str = "") -> dict:
    """按**时间窗**聚合 events 里的 fail_kind 计数，返回 {fail_kind: count}。

    为什么单独开一个（2026-11-17 改动证据卡）：
      fail_stats() 只按**条数**截断（最近 100 条），跟时间无关 —— 一个 3 个月前
      反复出现的 fail_kind 会一直算作「有」，幽灵修复照样放行。这里按**时间窗**
      过滤，只统计 since_hours 小时内真实发生过的失败，计数为 0 就说明这段时间
      根本没发生过，属于凭空捏的失败。

    参数：
      since_hours  时间窗（小时），<=0 表示不限时间（退化成 fail_stats 的语义）
      path         显式指定 task.json 路径（测试用）

    返回：{fail_kind: count}，按次数降序。任何异常都返回 {} —— 账本坏了不能
          影响主流程（调用方据此判断「查不到」还是「真的是 0」）。
    """
    try:
        h = float(since_hours)
    except (TypeError, ValueError):
        h = 24.0
    cutoff = 0.0
    if h > 0:
        cutoff = time.time() - h * 3600.0
    events = _load_events(path)
    kinds = []
    for e in events:
        if not isinstance(e, dict):
            continue
        k = e.get("fail_kind")
        if not isinstance(k, str) or not k.strip():
            continue
        if cutoff > 0:
            ts = _event_ts(e)
            # 时间戳解析不出来（0）时按「算它一次」处理：宁可放行也不要
            # 因为格式不认识就把真实失败判成幽灵修复。
            if ts and ts < cutoff:
                continue
        kinds.append(k.strip())
    c = Counter(kinds)
    return dict(c.most_common())


def fail_kind_summary(window_hours: float = 24, path: str = "") -> dict:
    """按**时间窗**聚合 events 里的 fail_kind 计数，返回 {fail_kind: count}。

    纯聚合、不依赖 LLM —— 前两次「改动证据卡」失败都是模型生成非法 JSON，
    这里把「查账本」这一步做成确定性函数，调用方拿到 dict 直接用，
    不需要再让模型去拼 JSON。

    与 aggregate_fail_kinds 的区别：本函数是面向「最近 N 小时」的便捷入口，
    参数名 window_hours 更直观；内部复用 aggregate_fail_kinds 的时间窗逻辑，
    避免两套时间过滤代码各写一遍、日后改一处漏一处。

    参数：
      window_hours  时间窗（小时），<=0 表示不限时间（退化成全量聚合）
      path          显式指定 task.json 路径（测试用）

    返回：{fail_kind: count}，按次数降序。任何异常都返回 {} —— 聚合坏了
          不能影响主流程（调用方据此判断「查不到」还是「真的是 0」）。
    """
    try:
        return aggregate_fail_kinds(since_hours=window_hours, path=path)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return {}


def aggregate_fail_kind(window: int = 100, path: str = "") -> dict:
    """从真实事件聚合 {fail_kind: count}，不猜常量（改动证据卡的数据源）。

    与 fail_stats() 的区别：fail_stats 是「按条数截断后聚合」，本函数是
    「按真实事件聚合」的显式入口 —— 语义上等价于 fail_stats(window, path)，
    但单独命名，便于 self_iterate 消费时一眼看出「这是证据卡的数据源」，
    也便于后续在此处扩展（比如按时间窗、按会话过滤）而不动 fail_stats。

    参数：
      window  取最近多少条带 fail_kind 的事件（<=0 表示不限）
      path    显式指定 task.json 路径（测试用）

    返回：{fail_kind: count}，按次数降序。任何异常都返回 {} —— 证据卡
          数据源坏了不能影响主流程。
    """
    try:
        return fail_stats(window=window, path=path)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return {}


def build_evidence(fail_kind: str = "", window: int = 100,
                   regress_result: str = "", path: str = "") -> dict:
    """组装「改动证据卡」：针对哪个真实失败 + 计数 + 回归结果。

    为什么单独开一个（2026-11-17 复核入口独立交付）：
      历史「改动证据卡」失败 2 次，都是**占位式修补** —— 给未定义的 count
      赋一个常量（比如 count=1），前端渲染出来像是「有 1 次真实失败」，
      实际上根本没查账本。这次改成从 actlog 真实聚合 fail_kind 计数：
      查不到就返回 None（前端显示「无证据」），绝不猜常量。

    参数：
      fail_kind       失败分类名（空 → 直接返回 None，调用方据此跳过证据展示）
      window          取最近多少条带 fail_kind 的事件（<=0 表示不限）
      regress_result  回归结果（"pass" / "fail" / "skip"，空 → 不填）
      path            显式指定 task.json 路径（测试用）

    返回：
      dict 形如 {"fail_kind": "timeout", "count": 3, "regression": "pass"}
      或 None（fail_kind 为空 / 账本里查不到该 fail_kind / 任何异常）。

    ⚠️ 关键语义：count 为 0 时**返回 None 而不是 {"count": 0}** ——
       「查不到」和「真的是 0」在证据卡上要区分开：前者说明这条动作
       没有真实失败支撑（可能是幽灵修复），后者说明账本坏了。
       调用方拿到 None 就不展示证据，而不是展示一个假的 0。
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return None
    try:
        counts = aggregate_fail_kind(window=window, path=path)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return None
    if not isinstance(counts, dict):
        return None
    n = counts.get(fk)
    if not isinstance(n, int) or n <= 0:
        # 账本里没有这个 fail_kind 的真实记录 → 不猜常量，返回 None
        return None
    ev = {"fail_kind": fk, "count": n}
    rr = str(regress_result or "").strip().lower()
    if rr in ("pass", "fail", "skip"):
        ev["regression"] = rr
    return ev


def count_fail_kind(fail_kind: str, days: int = 7, path: str = "") -> int:
    """统计某个 fail_kind 在最近 days 天内的真实发生次数。

    为什么单独开一个（2026-11-17 用户要求「改动证据卡」）：
      selfdev.patch 在动手前要确认「这个失败真的发生过」。fail_stats() 只按
      **条数**截断（最近 100 条），跟时间无关 —— 一个 3 个月前反复出现的
      fail_kind 会一直算作「有」，幽灵修复照样放行。这里按**时间窗**过滤，
      计数为 0 就说明近 7 天根本没发生过，属于凭空捏的失败。

    参数：
      fail_kind  失败分类名（空 → 直接返回 0，调用方据此跳过校验）
      days       时间窗（天），<=0 表示不限时间（退化成 fail_stats 的语义）
      path       显式指定 task.json 路径（测试用）

    返回：命中条数（int）。任何异常都返回 0 —— 账本坏了不能挡住正常改动，
          但调用方要能区分「查不到」和「真的是 0」，所以另给 count_fail_kind_ex。
    """
    n, _ok = count_fail_kind_ex(fail_kind, days=days, path=path)
    return n


def count_fail_kind_ex(fail_kind: str, days: int = 7, path: str = "") -> tuple:
    """同 count_fail_kind，但返回 (count, ok)。

    ok=False 表示**账本读不出来**（文件缺失/损坏），此时 count 恒为 0，
    调用方应当**放行**而不是当成幽灵修复拒绝 —— 账本坏了不能挡住正常改动。
    ok=True 且 count==0 才是真正的「近 N 天没发生过」。
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return 0, True
    try:
        p = path or os.path.join(config.data_root(), "data", "self_iterate", "task.json")
        if not os.path.isfile(p):
            return 0, False
        events = _load_events(p)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return 0, False
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    cutoff = 0.0
    if d > 0:
        cutoff = time.time() - d * 86400.0
    hit = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        k = e.get("fail_kind")
        if not isinstance(k, str) or k.strip() != fk:
            continue
        if cutoff > 0:
            ts = _event_ts(e)
            # 时间戳解析不出来（0）时按「算它一次」处理：宁可放行也不要
            # 因为格式不认识就把真实失败判成幽灵修复。
            if ts and ts < cutoff:
                continue
        hit += 1
    return hit, True


def append_watch_event(session: str, event: dict) -> None:
    """追加一条「盯事助手」事件到会话账本（与工具动作共用同一 jsonl）。

    为什么复用 actlog 的落盘：盯事助手（watch）需要「这件事我盯过、上次
    看到什么状态」的持久证据，而 actlog 已经是按会话隔离、滚动覆盖、
    重启后仍在的账本。单独再开一套存储只会多一份要维护的路径。

    事件行约定（尽量小，避免把大对象塞进账本）：
      {"t": "mm-dd HH:MM", "w": {…白名单字段…}}
    白名单字段：kind / key / state / prev / note / ts / count
    任何异常都吞掉 —— 盯事助手坏了绝不能影响对话本身。
    """
    try:
        if not isinstance(event, dict) or not event:
            return
        ev = {}
        for k in ("kind", "key", "state", "prev", "note", "ts", "count"):
            if k in event and event[k] is not None:
                v = event[k]
                if isinstance(v, str):
                    v = _first_line(v, 120)
                ev[k] = v
        if not ev:
            return
        row = {"t": time.strftime("%m-%d %H:%M"), "w": ev}
        path = _path(session)
        rows = _read(path)
        rows.append(row)
        _write(path, rows[-MAX_KEEP:])
    except Exception:
        logger.debug("silent-except", exc_info=True)


def recent_watch_events(session: str, n: int = 20) -> list:
    """取最近 n 条「盯事助手」事件（按写入顺序，旧→新）。

    只返回带 "w" 字段的行，工具动作行（带 "n"）会被跳过 —— 两者共用
    同一个 jsonl，但语义不同，调用方不该被工具动作污染。
    任何异常都返回 []。
    """
    try:
        k = int(n)
    except (TypeError, ValueError):
        k = 20
    if k <= 0:
        k = 20
    out = []
    for r in _read(_path(session)):
        if isinstance(r, dict) and isinstance(r.get("w"), dict):
            out.append(r)
    return out[-k:]


def clear(session: str) -> None:
    try:
        os.remove(_path(session))
    except Exception:
        logger.debug("silent-except", exc_info=True)
