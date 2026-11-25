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
import re
import threading
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
# 🧾 改动证据卡的计数口径（2026-11-17 用户要求）：fail_kind 的「真实计数」
#   从**最近 300 条日志**里统计 —— 不猜常量、不沿用调用方传进来的占位数。
#   注意 MAX_KEEP 只有 120（单会话滚动覆盖），所以"近 300 条"必须跨会话拼，
#   见 _recent_session_rows()；task.json 的事件也按"最近 300 条"截断。
COUNT_WINDOW = 300

# ★ 2026-11-23 修并发丢记录（实测丢 96.5%）：
#   record() 是「读全量 → 追加一条 → 整份覆盖写回」，原来**没有任何锁**。
#   两个线程同时进来：A 读到 50 条、B 也读到 50 条，各自 +1 后都写回 51 条，
#   后写的把先写的覆盖掉 —— 实测 8 线程写 200 条只剩 7 条。
#   后果不只是账本少几条：证据卡上的「针对 XX 失败 N 次」靠 count_fail_kind_recent
#   统计账本，账本丢了，N 就是错的。
#   两道锁：_LOCK 管同进程多线程；_file_lock 管跨进程（服务进程 + 回归/诊断子进程）。
_LOCK = threading.RLock()


class _file_lock:
    """跨进程排他锁（Windows 用 msvcrt，POSIX 用 fcntl）。

    只锁账本文件，拿不到锁就短暂重试；超时后**照常继续** ——
    宁可偶尔丢一条，也绝不能让记账把对话卡住。
    """

    def __init__(self, path: str, timeout: float = 2.0):
        self.path = path + ".lock"
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._fh = open(self.path, "a+b")
            t0 = time.time()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except Exception:
                    if time.time() - t0 > self.timeout:
                        return self
                    time.sleep(0.01)
        except Exception:
            logger.debug("silent-except", exc_info=True)
            return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                if os.name == "nt":
                    import msvcrt
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:                 # noqa: BLE001
                        # 解锁失败只可能是句柄已失效，关掉文件也会自动释放锁
                        logger.debug("unlock failed", exc_info=True)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return False


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
    落到账本行的 "ev" 字段（白名单），前端改动卡片据此渲染
    「针对 XX 失败 N 次 · 回归 通过/失败」。

    ⚠️ count 不以调用方传的为准：只要带了 fail_kind，就先用
      count_fail_kind_recent(fail_kind, window=COUNT_WINDOW=300) 从**近 300 条日志**
      统计真实次数并覆盖 —— 这样卡片上的 N 永远和 actlog 统计一致；
      只有账本读不出来时才退回调用方给的数（不猜常量）。
    """
    try:
        row = {"t": time.strftime("%m-%d %H:%M"),
               "n": str(name or "?")[:40],
               "a": _brief(args),
               "ok": bool(ok)}
        # 🧾 改动证据卡（2026-11-17 / 本次补全）：写入时**必须**带上 fail_kind 与
        #   「从近 COUNT_WINDOW(300) 条日志统计出来的真实计数」，前端改动卡片据此
        #   渲染「针对 XX 失败 N 次 · 回归 通过/失败」。三条硬规矩：
        #     ① fail_kind：优先取 evidence 里的，evidence 没给就看 args（工具参数
        #        里也可能带 fail_kind），都没有就不写 ev —— 不编一个分类出来；
        #     ② count：一律以**账本统计**为准（count_fail_kind_recent，近 300 条），
        #        调用方传进来的数只当"账本查不到时的兜底"，绝不让卡片上的数字
        #        和 actlog 统计对不上；
        #     ③ regression / phantom 等字段按白名单落盘，避免把大对象塞进账本。
        ev = {}
        if isinstance(evidence, dict):
            # ★ 2026-11-24：白名单加上 anchor_miss / fallback —— patch 锚点没命中
            #   而走降级重写时，这两个字段要真的落盘，否则「这类失败还剩多少」
            #   根本统计不出来（args 里带了但 row["a"] 只保留 rel）。
            for k in ("fail_kind", "count", "regression", "phantom", "rel",
                      "history", "line", "lines", "root_cause",
                      "anchor_miss", "fallback"):
                if k in evidence and evidence[k] is not None:
                    ev[k] = evidence[k]
        elif isinstance(evidence, str) and evidence.strip():
            ev["note"] = _first_line(evidence, 120)
        _fk = str(ev.get("fail_kind") or "").strip()
        if not _fk and isinstance(args, dict):
            _fk = str(args.get("fail_kind") or "").strip()
        if _fk:
            ev["fail_kind"] = _fk
            try:
                _n = int(count_fail_kind_recent(_fk, window=COUNT_WINDOW,
                                                session=session))
            except Exception:
                logger.debug("silent-except", exc_info=True)
                _n = -1
            _given = ev.get("count")
            if _n >= 0:
                ev["count"] = _n          # 账本能读 → 数字以账本为准
            elif isinstance(_given, int) and not isinstance(_given, bool) and _given >= 0:
                ev["count"] = int(_given)  # 账本读不出来 → 用调用方给的（不猜常量）
            else:
                ev.pop("count", None)
        elif "count" in ev:
            # 没有 fail_kind 时 count 没法核对，落一个假数字进账本只会误导前端
            ev.pop("count", None)
        _rr = str(ev.get("regression") or "").strip().lower()
        if _rr in ("pass", "fail", "skip"):
            ev["regression"] = _rr
        else:
            ev.pop("regression", None)
        if ev:
            row["ev"] = ev
        if note:
            row["m"] = _first_line(note, 90)
        elif not ok:
            row["m"] = _first_line(result, 90)
        path = _path(session)
        # ★ 读-改-写必须串行：先线程锁，再跨进程文件锁
        with _LOCK, _file_lock(path):
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


def count_rows(session: str = "") -> int:
    """当前账本行数 —— 给「本轮起点」打标记用。

    用法（见 main.py 回复出口 + reply_gate.build_delivery_checklist）：
        mark = actlog.count_rows(session)   # 本轮开始
        ... 这一轮的工具动作陆续落账 ...
        build_delivery_checklist(session=session, since=mark)   # 只统计本轮

    读不到账本时返回 0（当作「从第一行起」），任何异常都不往外抛。
    """
    try:
        return len(_read(_path(session)))
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return 0


def list_rows(session: str = "", limit: int = 0, since: int = 0) -> list:
    """按落盘顺序返回账本行（只读；给「本轮交付清单」这类统计用）。

    since > 0 表示跳过前 since 行（本轮起点）；limit > 0 表示只取末尾 limit 行。
    账本读不出来/格式不对一律返回 []，绝不抛异常。
    """
    try:
        rows = _read(_path(session))
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []
    if not isinstance(rows, list):
        return []
    try:
        since = max(0, int(since or 0))
    except Exception:
        since = 0
    rows = rows[since:]
    try:
        limit = int(limit or 0)
    except Exception:
        limit = 0
    if limit > 0:
        rows = rows[-limit:]
    return rows


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


def _recent_session_rows(window: int = COUNT_WINDOW) -> list:
    """读账本目录下**最近 window 条**会话日志行（跨会话，按文件修改时间排序）。

    为什么要跨会话：每个会话只留 MAX_KEEP(120) 条，单会话日志未必攒得出 300 条，
    「近 300 条日志」只能把各会话账本按时间拼起来再取尾部。
    任何异常都返回已读到的部分（不抛）—— 账本坏了不能影响主流程。
    """
    rows = []
    try:
        d = _dir()
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
        names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)))
        for n in names:
            rows.extend(_read(os.path.join(d, n)))
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        k = int(window)
    except (TypeError, ValueError):
        k = COUNT_WINDOW
    if k <= 0:
        k = COUNT_WINDOW
    return rows[-k:]


def record_act(name: str, ok: bool = True, summary: str = "",
               session: str = "brand", result: str = "", detail: str = "",
               files=None, **extra) -> None:
    """兼容封装：把 record_act(name, ok=, summary=) 转成 record(session, name, ...)。

    2026-11-22：brand.py 的品牌解锁 / 写入留痕调用的是 `record_act`，而 actlog
    只暴露 `record(session, name, ...)` —— 静态检查因此报 high（调用了不存在的
    函数）。brand.py 受品牌密码保护、不适合为这点小事去解锁，所以在账本这边补
    一层薄封装。任何异常都吞掉：账本坏了绝不能拦住写入本身。

    ★ 2026-11-24「改动证据卡 · 补齐到 selfdev 提案入口」：
      `selfdev.patch` 的幽灵修复闸门是用
      `record_act("selfdev_phantom", summary=…, result=…, detail=…, files=…,
                  phantom=True, fail_kind=…, count=0)` 留痕的 —— 而老签名
      只认 (name, ok, summary, session)，多出来的 phantom / fail_kind / count
      会抛 TypeError 被外层 except 吞掉，**一条 phantom 事件都没落盘**，
      事后谁也查不出「拦了没有、拦了谁」（见 record_phantom 的注释）。
      所以这里把 evidence 通道打开：detail/files/phantom/fail_kind/count/…
      都转成 record() 的 evidence，真写进账本的 ev 字段。
    """
    try:
        ev = {}
        for k in ("fail_kind", "count", "regression", "phantom", "rel",
                  "history", "line", "lines", "root_cause", "days",
                  "anchor_miss", "fallback"):
            if k in extra and extra[k] is not None:
                ev[k] = extra[k]
        if ev.get("phantom"):
            ev["phantom"] = True
        args = {}
        rels = [f for f in (files or []) if isinstance(f, str) and f]
        if rels:
            args["rel"] = rels[0]
            ev.setdefault("rel", rels[0])
        # ★ 2026-11-23：record() 只在 note 有值或 ok=False 时才写摘要字段 m，
        #   而这里原来只传了 result —— 于是「验证通过」「跑分 155.09」这类
        #   ok=True 的记录在账本里只剩 {"t","n","a","ok"}，看不到任何内容。
        #   摘要必须同时传 note，日志才看得出验了什么、分数多少。
        msg = str(summary or "") or str(detail or "") or str(result or "")
        record(session, name, args, ok=bool(ok),
               result=str(result or detail or summary or ""), note=msg,
               evidence=ev or None)
    except Exception:                                     # noqa: BLE001
        logger.debug("record_act 兼容封装写账本失败", exc_info=True)


def count_fail_kind_recent(fail_kind: str, window: int = COUNT_WINDOW,
                           session: str = "", path: str = "") -> int:
    """从**最近 window 条日志**统计某个 fail_kind 的真实发生次数（证据卡计数口径）。

    这是「改动证据卡」上那个 N 的唯一来源 —— 2026-11-17 用户要求：
      卡片必须显示「针对 XX 失败 N 次」，且 N 要和 actlog 统计一致，不许猜常量。

    数据源（两处都是日志，谁有数用谁，都没有就是 0，不编）：
      1) task.json 的 events 里带 fail_kind 的事件，取最近 window 条
         （即 aggregate_fail_kind / fail_stats 的语义）；
      2) 会话账本行（data/actlog/*.jsonl）里 ev.fail_kind / fail_kind / w.fail_kind
         命中的行，取最近 window 条。给「只写了账本、没有 task.json」的场景兜底。

    参数：
      fail_kind  失败分类名（空 → 直接 0）
      window     看最近多少条日志（默认 COUNT_WINDOW = 300）
      session    非空 → 只统计该会话账本；空 → 跨会话取最近的
      path       显式指定 task.json 路径（测试用）

    返回 int，任何异常都返回 0。
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return 0
    try:
        n = int(aggregate_fail_kind(window=window, path=path).get(fk) or 0)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        n = 0
    if n > 0:
        return n
    # events 里没有 → 退回会话账本行统计（同样的"最近 window 条"口径）
    try:
        try:
            k = int(window)
        except (TypeError, ValueError):
            k = COUNT_WINDOW
        if k <= 0:
            k = COUNT_WINDOW
        rows = _read(_path(session))[-k:] if session else _recent_session_rows(k)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return n
    c = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        k2 = ""
        ev = r.get("ev")
        if isinstance(ev, dict):
            k2 = str(ev.get("fail_kind") or "")
        if not k2:
            k2 = str(r.get("fail_kind") or "")
        if not k2 and isinstance(r.get("w"), dict):
            k2 = str(r["w"].get("fail_kind") or "")
        if k2.strip() == fk:
            c += 1
    return c


def top_fail_kinds(n: int = 10, path: str = "") -> list:
    """从现有 actlog 记录里按 fail_kind 聚合，返回 top N 失败类型及次数。

    数据源：task.json 的 events 里带 fail_kind 字段的事件（与 fail_stats 同源）。
    返回：[{"fail_kind": str, "count": int}, ...]，按次数降序，最多 n 条。
    任何异常都返回 [] —— 看板坏了不能影响主流程。
    """
    try:
        k = int(n)
    except (TypeError, ValueError):
        k = 10
    if k <= 0:
        k = 10
    try:
        counts = fail_stats(window=0, path=path)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []
    if not isinstance(counts, dict):
        return []
    out = []
    for fk, c in counts.items():
        if isinstance(fk, str) and fk.strip() and isinstance(c, int) and c > 0:
            out.append({"fail_kind": fk.strip(), "count": c})
    out.sort(key=lambda x: (-x["count"], x["fail_kind"]))
    return out[:k]


def build_evidence(fail_kind: str = "", window: int = COUNT_WINDOW,
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


def _fk_norm(s) -> str:
    """fail_kind 归一：去空白 + 全角括号/空格转半角，便于中英文与全半角互认。

    ★ 2026-11-25 修（真实误拒，不是推测）：模型提案里的 fail_kind 常常写**中文**
    分类名（实测 plans.json：fail_kind="跑分退步（低于上一版）"），而
    fail_attribution.jsonl 里同一件事是 kind="score_regress" +
    kind_zh="跑分退步（低于上一版）"。原来的聚合只认 kind 字段，
    于是「按中文名查英文键」永远得 0 → 真实修复被当成幽灵拒绝。
    实测 11-25 03:45 / 05:46 两轮自动迭代就是这样整轮失败。
    """
    t = str(s or "").strip()
    if not t:
        return ""
    t = t.replace("（", "(").replace("）", ")").replace("　", " ")
    return " ".join(t.split())


def _fk_keys(rec) -> set:
    """一条失败记录可能带的全部 fail_kind 别名（英文 kind / 中文 kind_zh）。

    同一条记录里 kind 与 fail_kind 往往相同，用 set 去重，避免重复计数。
    """
    if not isinstance(rec, dict):
        return set()
    out = set()
    for k in ("kind", "kind_zh", "fail_kind", "fail_kind_zh"):
        v = _fk_norm(rec.get(k))
        if v:
            out.add(v)
    return out


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


def _count_fail_attr(fail_kind: str, cutoff: float = 0.0) -> int:
    """数「失败归因账本」fail_attribution.jsonl 里某个 kind 的条数。

    为什么必须合并这个来源（2026-11-23 实测出来的真实缺陷）：
      task.json 的 events 里**几乎不存在** fail_kind 字段 —— 实测 2000 条事件
      里只有 1 条带 fail_kind，而且还是 phantom_reject 事件自己写进去的。
      真实的失败分类记在 fail_attribution.jsonl 的 kind 字段（近 7 天 18 条：
      score_regress 7 / no_edit 3 / patch_not_found 2 / model_error 2 …）。
      只读 events 会让计数恒为 0，于是「幽灵修复」闸门把**所有带 fail_kind 的
      真实修复**全判成幽灵拒掉（实测 16:40:53 fail_kind=model 被拒），
      反而那些不带 fail_kind 的凭空动作能放行 —— 闸门方向完全反了。
    """
    try:
        p = os.path.join(config.data_root(), "data", "self_iterate",
                         "fail_attribution.jsonl")
        if not os.path.isfile(p):
            return 0
        n = 0
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:                       # noqa: BLE001
                    continue
                if not isinstance(r, dict):
                    continue
                # ★ 2026-11-25：不再只比对 kind —— 模型写中文 kind_zh 也要能命中
                if _fk_norm(fail_kind) not in _fk_keys(r):
                    continue
                if cutoff > 0:
                    ts = _event_ts(r)
                    if ts and ts < cutoff:
                        continue
                n += 1
        return n
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return 0


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
        # ★ 2026-11-25：中文 kind_zh / 英文 kind 都算同一个失败分类
        if _fk_norm(fk) not in _fk_keys(e):
            continue
        if cutoff > 0:
            ts = _event_ts(e)
            # 时间戳解析不出来（0）时按「算它一次」处理：宁可放行也不要
            # 因为格式不认识就把真实失败判成幽灵修复。
            if ts and ts < cutoff:
                continue
        hit += 1
    # ★ 2026-11-23 修：events 里的 fail_kind 在生产里几乎不存在（见
    #   _count_fail_attr 的说明），必须把失败归因账本一并数进来，
    #   否则闸门恒判 0 → 真实修复全被拒、凭空动作反而放行。
    #   path 显式指定时（测试用）不读生产账本，保证用例可重复。
    if not path:
        hit += _count_fail_attr(fk, cutoff)
    return hit, True


def _attr_counts(cutoff: float = 0.0) -> dict:
    """把失败归因账本 fail_attribution.jsonl 按 kind 聚合成 {kind: count}。

    与 _count_fail_attr 同源，只是这里要**一次拿到所有分类**的计数
    （提案前的证据卡要显示「哪个失败、几次」，不是只查一个）。
    任何异常都返回已读到的部分 —— 账本坏了不能影响主流程。
    """
    out = Counter()
    try:
        p = os.path.join(config.data_root(), "data", "self_iterate",
                         "fail_attribution.jsonl")
        if not os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:                       # noqa: BLE001
                    continue
                if not isinstance(r, dict):
                    continue
                _ks = _fk_keys(r)
                if not _ks:
                    continue
                if cutoff > 0:
                    ts = _event_ts(r)
                    if ts and ts < cutoff:
                        continue
                for _k in _ks:
                    out[_k] += 1
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return dict(out)


def fail_kind_counts(days: int = 7, path: str = "") -> dict:
    """按**时间窗**聚合所有 fail_kind 的真实计数，返回 {fail_kind: count}。

    这是「改动证据卡 / 幽灵修复闸门」的统一数据源：
      1) task.json 的 events 里带 fail_kind 的事件（按 days 天过滤）；
      2) 失败归因账本 fail_attribution.jsonl 的 kind 字段
         （生产里真实的失败分类几乎都在这里，events 里几乎没有 —— 见
          _count_fail_attr 的说明）；
      3) 会话账本行（data/actlog/*.jsonl）里带 fail_kind 的行。
    三处合并求和，谁有数算谁，都没有就是空 dict（不编常量）。

    参数：
      days  时间窗（天），<=0 表示不限时间
      path  显式指定 task.json 路径（测试用；给了就不读生产归因账本，
            保证用例可重复）

    任何异常都返回已读到的部分 —— 账本坏了不能影响主流程。
    """
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    cutoff = (time.time() - d * 86400.0) if d > 0 else 0.0
    out = Counter()
    try:
        for e in _load_events(path):
            if not isinstance(e, dict):
                continue
            # ⚠️ 这里**只能取 fail_kind 字段**：events 里的 kind 是事件类型
            #   （step / code / think …），拿它当失败分类会把统计表灌满噪音。
            _ks = set()
            for _kk in ("fail_kind", "fail_kind_zh"):
                _v = _fk_norm(e.get(_kk))
                if _v:
                    _ks.add(_v)
            if not _ks:
                continue
            if cutoff > 0:
                ts = _event_ts(e)
                if ts and ts < cutoff:
                    continue
            for _k in _ks:
                out[_k] += 1
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    if not path:
        try:
            for k, n in _attr_counts(cutoff).items():
                out[k] += int(n or 0)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    try:
        for r in _recent_session_rows(COUNT_WINDOW):
            if not isinstance(r, dict):
                continue
            ev = r.get("ev") if isinstance(r.get("ev"), dict) else {}
            _ks = set()
            for _kk in ("fail_kind", "fail_kind_zh"):
                _v = _fk_norm(ev.get(_kk) or r.get(_kk))
                if _v:
                    _ks.add(_v)
            if not _ks:
                continue
            if ev.get("phantom"):
                # 幽灵事件自己带 fail_kind，但它**不代表真实发生过** ——
                # 数进去会让闸门自己把自己放行（自证循环）。
                continue
            for _k in _ks:
                out[_k] += 1
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {k: int(v) for k, v in out.items() if v > 0}


def record_phantom(fail_kind: str, rel: str = "", reason: str = "",
                   session: str = "", days: int = 7, extra: dict = None) -> dict:
    """记一条「幽灵修复被拒」事件到账本（含 fail_kind 名），供事后审计/断言。

    为什么单独一个入口（2026-11-24「提案入口加真实失败证据校验」）：
      拒绝幽灵提案时必须留下**可查证的证据**，否则「拦了没有、拦了谁」
      谁也说不清。原来 selfdev 用的是 `record_act(... phantom=True ...)`
      —— 而 record_act 只接受 (name, ok, summary, session)，多传的
      phantom/fail_kind 会抛 TypeError 被外层 except 吞掉，事件**一条都没落盘**。
      这里用 record() 的 evidence 通道落盘：ev={"fail_kind":…, "phantom":True}。
    """
    fk = str(fail_kind or "").strip()
    msg = str(reason or "").strip() or ("幽灵修复被拒：fail_kind=%s" % (fk or "?"))
    ev = {"fail_kind": fk, "phantom": True}
    if rel:
        ev["rel"] = str(rel)
    try:
        ev["count"] = int((extra or {}).get("count") or 0)
    except (TypeError, ValueError):
        ev["count"] = 0
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in ev and v is not None:
                ev[k] = v
    try:
        record(str(session or "default"), "selfdev_phantom",
               {"rel": str(rel or ""), "fail_kind": fk}, ok=False,
               result=msg, note=msg, evidence=ev)
    except Exception:                                   # noqa: BLE001
        logger.debug("record_phantom 写账本失败", exc_info=True)
        return {}
    return {"ok": True, "fail_kind": fk, "phantom": True,
            "session": str(session or "default"), "summary": _first_line(msg, 90),
            "evidence": {"fail_kind": fk, "phantom": True, "count": ev.get("count", 0)}}


def phantom_events(session: str = "", limit: int = 50) -> list:
    """读回「幽灵修复被拒」事件（phantom=true），供断言 / 面板查证。

    参数：
      session  非空 → 只查该会话账本；空 → 跨会话（按账本文件时间）
      limit    最多返回多少条（最新的在后）

    返回：[{"session","t","name","fail_kind","summary","rel"}, ...]
    """
    rows = []
    try:
        if session:
            files = [(_path(session), session)]
        else:
            d = _dir()
            names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
            names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)))
            files = [(os.path.join(d, n), n[:-6]) for n in names]
        for p, ses in files:
            for r in _read(p):
                if not isinstance(r, dict):
                    continue
                ev = r.get("ev") if isinstance(r.get("ev"), dict) else {}
                if not ev.get("phantom"):
                    continue
                rows.append({"session": ses,
                             "t": r.get("t", ""),
                             "name": r.get("n", ""),
                             "fail_kind": str(ev.get("fail_kind") or ""),
                             "rel": str(ev.get("rel") or ""),
                             "summary": r.get("m", "")})
    except Exception:                                   # noqa: BLE001
        logger.debug("phantom_events 读账本失败", exc_info=True)
        return []
    try:
        k = int(limit)
    except (TypeError, ValueError):
        k = 50
    if k > 0:
        rows = rows[-k:]
    return rows


def _any_ledger_file(path: str = "") -> bool:
    """账本到底有没有（task.json / 归因账本 / 会话账本 任一存在）。"""
    try:
        p = path or os.path.join(config.data_root(), "data", "self_iterate",
                                 "task.json")
        if os.path.isfile(p):
            return True
        if os.path.isfile(os.path.join(config.data_root(), "data", "self_iterate",
                                       "fail_attribution.jsonl")):
            return True
        d = _dir()
        return bool(os.path.isdir(d) and any(n.endswith(".jsonl")
                                            for n in os.listdir(d)))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return False


def propose_patch(fail_kind: str = "", rel: str = "", find: str = "",
                  replace: str = "", note: str = "", description: str = "",
                  session: str = "", days: int = 7, count=None,
                  path: str = "", **extra) -> dict:
    """**提案入口的证据闸门**：这个改动针对的失败，账本里真的发生过吗？

    用户要求（2026-11-24，对应 weakness「幽灵修复 / Phantom Guardrails」）：
      「从 actlog 取该 fail_kind 的真实计数，计数为 0 则拒绝提案并记 phantom
        事件（含 fail_kind 名）；计数 >0 则把计数写进提案元数据，后续改动结果
        卡片展示。」

    为什么放在 actlog 而不是 selfdev（说明偏离计划的原因）：
      `backend/selfdev.py` 是回撤机制自身，**写入工具硬性禁止改写**（改了就没法
      回滚了，见 lessonloop.patch_with_fallback 的注释）。所以提案闸门落在账本
      模块：它本来就是「真实失败计数」的唯一数据源，放在这里反而少一层跨模块
      取数；selfdev / self_iterate 两边的提案与执行入口都调它，口径只有一份。

    判定（单步可验证，没有隐藏分支）：
      · fail_kind 为空      → 放行（不是针对某个失败分类的修复，无需证据）；
      · 真实计数 > 0        → 放行，count / days / 来源写进 meta（结果卡片展示）；
      · 真实计数 == 0       → 拒绝（reason=phantom），并记一条 phantom 事件到
                              账本（含 fail_kind 名），供事后审计与断言查证。

    参数：
      fail_kind      本次改动针对的失败分类
      rel/find/replace/note/description  提案本身（原样带回，便于执行与审计）
      session        账本会话（phantom 事件记到这个会话；空 → "default"）
      days           计数时间窗（天）
      count          调用方已知的真实计数（离线回放/断言用；给了就不再查账本）
      path           显式指定 task.json（测试用）

    返回 dict（ok/accepted 双口径，避免各调用方约定不一致时互相误解）：
      ok / accepted    True=提案通过；False=被拒
      rejected/blocked True=被拒（与 accepted 互斥）
      phantom          True=按幽灵修复拒绝
      reason           "" / "phantom"
      error            给模型看的中文拒绝原因
      fail_kind/count/days  提案元数据
      evidence        结构化证据 {fail_kind,count,days,phantom,source}
      evidence_text   一句话证据，前端卡片可直接渲染
      meta            提案元数据（计数就在这里，随提案一起落盘）
      proposal        规范化后的提案本体（rel/find/replace/note/meta）
    """
    fk = str(fail_kind or "").strip()
    rel = str(rel or "").strip()
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    # 别名兼容：不同调用方对「已知计数」的叫法不一样
    if count is None:
        for k in ("evidence_count", "real_count", "fail_count", "count_hint"):
            if extra.get(k) is not None:
                count = extra.get(k)
                break
    meta = {"fail_kind": fk, "count": 0, "days": d, "phantom": False,
            "source": "given" if count is not None else "ledger"}
    if not fk:
        # 没带 fail_kind：不是「针对某个失败的修复」，跳过证据校验（不判幽灵）。
        meta["note"] = "未指定 fail_kind，跳过真实失败证据校验"
        return {"ok": True, "accepted": True, "approved": True,
                "rejected": False, "blocked": False, "phantom": False,
                "reason": "", "error": "", "fail_kind": "", "count": 0,
                "days": d,
                "evidence": {"fail_kind": "", "count": 0, "phantom": False,
                             "days": d, "source": "none"},
                "evidence_text": "", "meta": meta,
                "proposal": {"rel": rel, "find": find, "replace": replace,
                             "note": note, "description": description,
                             "meta": meta}}
    n = -1
    if count is not None:
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = -1
    if n < 0:
        try:
            _cnts = fail_kind_counts(days=d, path=path)
            # ★ 2026-11-25：聚合表的键已归一（全角括号→半角），取值也必须归一。
            #   原来直接拿原文 fk 去取 —— 实测「跑分退步（低于上一版）」账本里有
            #   18 条，这里仍取到 0，整轮迭代被判幽灵修复。
            n = int(_cnts.get(_fk_norm(fk)) or 0)
            if n <= 0:
                # 兜底：老账本里可能存着未归一的写法（大小写/空格差异）。
                # 只在两边都够长（>=3 字）时做包含比对，避免「model」这种短词乱配。
                _want = _fk_norm(fk)
                if len(_want) >= 3:
                    for _k, _v in _cnts.items():
                        _k = str(_k or "")
                        if len(_k) >= 3 and (_want in _k or _k in _want):
                            n = max(n, int(_v or 0))
        except Exception:                               # noqa: BLE001
            logger.debug("propose_patch 查账本失败", exc_info=True)
            n = 0
    n = max(0, int(n))
    meta["count"] = n
    readable = _any_ledger_file(path)
    evidence_text = "针对 fail_kind: %s，近 %d 天 %d 次" % (fk, d, n)
    if n <= 0:
        # ---- 幽灵修复：拒绝提案 + 记 phantom 事件（含 fail_kind 名）----
        reason = ("拒绝：提案 fail_kind「%s」近 %d 天真实失败计数为 0，属于幽灵修复。"
                  "请先确认这个失败真的出现过（actlog / task.json / "
                  "fail_attribution.jsonl 里有记录），再动手改代码。"
                  % (fk, d))
        if not readable:
            reason += "（账本里查不到任何记录：可能账本还没生成，或该分类从未发生过）"
        meta["phantom"] = True
        meta["ledger_readable"] = bool(readable)
        try:
            record_phantom(fk, rel=rel, reason=reason, session=session, days=d,
                           extra={"count": 0})
        except Exception:                               # noqa: BLE001
            logger.debug("记 phantom 事件失败", exc_info=True)
        return {"ok": False, "accepted": False, "approved": False,
                "rejected": True, "blocked": True, "phantom": True,
                "reason": "phantom", "error": reason,
                "fail_kind": fk, "count": 0, "days": d,
                "evidence": {"fail_kind": fk, "count": 0, "phantom": True,
                             "days": d, "source": "ledger"},
                "evidence_text": evidence_text + ("（账本里查不到该分类）"
                                                 if not readable else ""),
                "meta": meta,
                "proposal": {"rel": rel, "find": find, "replace": replace,
                             "note": note, "description": description,
                             "meta": meta}}
    # ---- 计数 >0：放行，把计数写进提案元数据（结果卡片据此展示）----
    meta["ledger_readable"] = True
    return {"ok": True, "accepted": True, "approved": True,
            "rejected": False, "blocked": False, "phantom": False,
            "reason": "", "error": "", "fail_kind": fk, "count": n, "days": d,
            "evidence": {"fail_kind": fk, "count": n, "phantom": False,
                         "days": d, "source": "ledger"},
            "evidence_text": evidence_text, "meta": meta,
            "proposal": {"rel": rel, "find": find, "replace": replace,
                         "note": note, "description": description,
                         "meta": meta}}


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


def list_events(session: str = "", kind: str = "", limit: int = 0) -> list:
    """读出账本里的事件行（工具动作行 + 盯事 / 盯梢事件行）。

    为什么需要它（2026-11-22 盯梢小助手）：以前只能按会话读（timeline /
    recent_watch_events），要回答「刚刚到底有没有新汇报」「一共汇报了几次」
    必须跨会话看总数。这里给一个**平铺**的读法：

        session  给定 → 只读那本账；留空 → 读 data/actlog 下所有会话账本
        kind     按事件类型过滤：'watch' 只留盯事行（带 w 字段），
                 'tool' 只留工具动作行（带 n 字段），留空全要
        limit    >0 时只返回最近这么多条（旧 → 新）

    返回原始行（dict）列表，按写入顺序（旧 → 新）。任何异常返回 []。
    """
    try:
        k = str(kind or "").strip().lower()
        out = []
        if session:
            paths = [_path(session)]
        else:
            paths = []
            try:
                for name in sorted(os.listdir(_dir())):
                    if name.endswith(".jsonl"):
                        paths.append(os.path.join(_dir(), name))
            except Exception:
                logger.debug("silent-except", exc_info=True)
        for p in paths:
            for r in _read(p):
                if not isinstance(r, dict):
                    continue
                is_watch = isinstance(r.get("w"), dict)
                is_tool = bool(r.get("n"))
                if k == "watch" and not is_watch:
                    continue
                if k == "tool" and not is_tool:
                    continue
                out.append(r)
        try:
            n = int(limit or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            out = out[-n:]
        return out
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []


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


# 阶段名 → 前端时间线卡片用的中文标题 / 状态（2026-11-17 时间线卡片）
# 用户反复问「为什么没反应」「我都批准了肯定立刻执行啊」—— 根因是批准后
# 系统在干什么完全不可见。这里把每个阶段映射成一句人话 + 一个状态，
# 前端 featpanel.js 直接渲染成时间线，用户一眼看到「它现在在干什么、卡在哪」。
_STAGE_META = {
    "queued":      ("已入队", "running"),
    "agent_step":  ("正在执行", "running"),
    "verify_pass": ("验证通过", "done"),
    "verify_fail": ("验证失败", "failed"),
    "rolled_back": ("已回滚", "failed"),
    "approved":    ("已批准", "done"),
    "executing":   ("开始执行", "running"),
    "verified":    ("验证通过", "done"),
    "blocked":     ("被拦下", "failed"),
    "failed":      ("执行失败", "failed"),
    "done":        ("已完成", "done"),
}


def record_stage(plan_id: str, stage: str, detail: str = "",
                 fail_kind: str = "", count=None, regression: str = "") -> None:
    """记录计划的一个阶段事件（批准 → 执行 → 验证），供前端时间线卡片读取。

    为什么单独开一个（2026-11-17 时间线卡片）：
      工具动作账本 record() 记的是「动了什么」，但前端时间线卡片要的是
      「这个计划走到哪一步了」—— 批准、开始执行、验证通过是三个不同语义的
      里程碑，混在工具动作里既不好筛也不好渲染。这里按 plan_id 单独落一份
      阶段流水，前端按 plan_id 拉取即可画出时间线。

    落盘位置：data/actlog/stages/<plan_id>.jsonl，与工具动作账本同根但分目录，
    互不干扰（工具动作按会话隔离，阶段事件按计划隔离）。

    参数：
      plan_id     计划 ID（空 → 直接忽略，调用方无需判空）
      stage       阶段名（如 "queued" / "agent_step" / "verify_pass" /
                  "verify_fail" / "rolled_back"）
      detail      可选细节（会压成一行，最多 200 字符）
      fail_kind   可选失败分类（改动证据卡：与 task.json events 里的
                  fail_kind 同名，前端据此把阶段事件和真实失败对上号）
      count       可选计数（该 fail_kind 在账本里的真实发生次数；
                  None → 不落盘，绝不猜常量）
      regression  可选回归结果（"pass" / "fail" / "skip"，其它值忽略）

    落盘字段（前端时间线卡片直接消费）：
      t / ts / stage / detail / title / state
      title 是给人看的中文标题，state 是 done/running/failed 之一，
      前端不用再自己维护一份映射表。
      可选证据字段：fail_kind / count / regression（仅当传入且合法时落盘）。

    任何异常都吞掉 —— 阶段日志坏了绝不能影响主流程。
    """
    try:
        pid = "".join(c for c in str(plan_id or "") if c.isalnum() or c in "-_")
        st = str(stage or "").strip()
        if not pid or not st:
            return
        d = os.path.join(_dir(), "stages")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        path = os.path.join(d, pid + ".jsonl")
        title, state = _STAGE_META.get(st, (st, "running"))
        row = {"t": time.strftime("%m-%d %H:%M:%S"),
               "ts": time.time(),
               "stage": st[:40],
               "title": title,
               "state": state}
        if detail:
            row["detail"] = _first_line(detail, 200)
        # 改动证据卡：把「针对哪个真实失败 + 计数 + 回归结果」一并落到阶段流水，
        # 前端时间线卡片可直接渲染，不必再回查 task.json。
        fk = str(fail_kind or "").strip()
        if fk:
            row["fail_kind"] = fk[:80]
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            row["count"] = count
        rr = str(regression or "").strip().lower()
        if rr in ("pass", "fail", "skip"):
            row["regression"] = rr
        rows = _read(path)
        rows.append(row)
        _write(path, rows[-MAX_KEEP:])
    except Exception:
        logger.debug("silent-except", exc_info=True)


def get_stages(plan_id: str, n: int = 0) -> list:
    """读取某个计划的阶段事件（按写入顺序，旧→新），供前端时间线卡片渲染。

    参数：
      plan_id  计划 ID（空 → 返回 []）
      n        最多返回多少条（<=0 表示全部）

    返回：[{"t": "mm-dd HH:MM:SS", "ts": epoch, "stage": str,
            "title": str, "state": str, "detail": str?}, ...]
    老记录里没有 title/state 的，这里按 _STAGE_META 补齐，前端不用兼容两套。
    任何异常都返回 [] —— 时间线卡片坏了不能影响主流程。
    """
    try:
        pid = "".join(c for c in str(plan_id or "") if c.isalnum() or c in "-_")
        if not pid:
            return []
        path = os.path.join(_dir(), "stages", pid + ".jsonl")
        rows = _read(path)
        try:
            k = int(n)
        except (TypeError, ValueError):
            k = 0
        if k > 0:
            rows = rows[-k:]
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            st = str(r.get("stage") or "")
            title, state = _STAGE_META.get(st, (st, "running"))
            if not r.get("title"):
                r["title"] = title
            if not r.get("state"):
                r["state"] = state
            out.append(r)
        return out
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []


def timeline(limit: int = 20, session: str = "") -> list:
    """按 ts 倒序返回改动事件，字段固定为 ts/status/summary/diff_summary/regress_pass。

    数据源：会话账本（工具动作行 + 盯事助手事件行）与计划阶段流水，
    统一归一化成固定字段，缺失字段用默认值兜底并在返回中标注。

    参数：
      limit    最多返回多少条（<=0 时按 20 处理）
      session  会话 ID（空 → 只聚合阶段流水；非空 → 同时聚合该会话账本）

    返回：[{"ts": float, "status": str, "summary": str,
            "diff_summary": str, "regress_pass": bool|None,
            "missing": [缺失字段名...]}, ...]，按 ts 倒序。
    空数据返回 []，任何异常也返回 [] —— 时间线坏了不能影响主流程。
    """
    try:
        k = int(limit)
    except (TypeError, ValueError):
        k = 20
    if k <= 0:
        k = 20
    out = []
    try:
        # 1) 会话账本：工具动作行（带 n）与盯事事件行（带 w）
        if session:
            for r in _read(_path(session)):
                if not isinstance(r, dict):
                    continue
                ts = _event_ts(r)
                if isinstance(r.get("w"), dict):
                    w = r["w"]
                    status = str(w.get("state") or "info")
                    summary = "%s %s" % (w.get("kind") or "watch",
                                          w.get("key") or w.get("note") or "")
                    summary = " ".join(summary.split())
                    out.append({"ts": ts, "status": status,
                                "summary": summary, "diff_summary": "",
                                "regress_pass": None})
                elif r.get("n"):
                    ok = r.get("ok") is not False
                    status = "ok" if ok else "fail"
                    summary = "%s %s" % (r.get("n") or "?", r.get("a") or "")
                    summary = " ".join(summary.split())
                    diff_summary = str(r.get("m") or "")
                    out.append({"ts": ts, "status": status,
                                "summary": summary,
                                "diff_summary": diff_summary,
                                "regress_pass": None})
        # 2) 计划阶段流水：data/actlog/stages/*.jsonl
        sdir = os.path.join(_dir(), "stages")
        try:
            names = os.listdir(sdir)
        except Exception:
            names = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            for r in _read(os.path.join(sdir, name)):
                if not isinstance(r, dict):
                    continue
                ts = _event_ts(r)
                status = str(r.get("state") or "running")
                summary = "%s %s" % (r.get("title") or r.get("stage") or "",
                                      r.get("detail") or "")
                summary = " ".join(summary.split())
                rr = str(r.get("regression") or "").strip().lower()
                regress_pass = None
                if rr == "pass":
                    regress_pass = True
                elif rr == "fail":
                    regress_pass = False
                out.append({"ts": ts, "status": status,
                            "summary": summary, "diff_summary": "",
                            "regress_pass": regress_pass})
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []
    # 按 ts 倒序（ts 解析不出来的排最后，保持稳定）
    out.sort(key=lambda x: x.get("ts") or 0.0, reverse=True)
    out = out[:k]
    # 缺失字段兜底并标注
    for item in out:
        missing = []
        if not item.get("ts"):
            missing.append("ts")
        if not item.get("status"):
            item["status"] = "unknown"
            missing.append("status")
        if not item.get("summary"):
            item["summary"] = ""
            missing.append("summary")
        if item.get("diff_summary") is None:
            item["diff_summary"] = ""
            missing.append("diff_summary")
        if item.get("regress_pass") is None:
            missing.append("regress_pass")
        item["missing"] = missing
    return out


# ------------------------------------------------------------------ 历史声称回扫
# ★ 2026-11-24 用户要求「历史「已完成」记录回扫：声称新建的文件必须真实存在」：
#   账本里的 detail 经常写着「新建 backend/xxx.py」，可下一次回忆「我建过什么」
#   时只能看到这行字 —— 文件其实从没落盘。于是模型拿自己的旧记录当证据，
#   越回忆越离谱。这里把这类「声称」当作**待验证断言**，逐条核对磁盘：
#   对不上的记一条 claim_mismatch 事件（落 data/actlog/claim_mismatch.jsonl）。
_CLAIM_RE = re.compile(r"(?:新建|新增)\s*[：:=\-—]?\s*"
                       r"([^\s，,。；;、:：!！?？（）()\[\]{}<>\"'“”‘’|]+)")
_CLAIM_SESSION = "claim_mismatch"     # 事件落到单独一本账，不污染会话账本
_CLAIM_GAP = 300.0                    # 同一路径 5 分钟内只记一次（面板会反复轮询）
_CLAIM_KEEP = 200                     # 单进程最多记多少条，防止异常路径刷爆账本
_CLAIM_SEEN = {}


def _claim_roots() -> list:
    """「新建 <path>」里的相对路径按哪几个根去解析（项目根 / backend / cwd）。"""
    roots = []
    try:
        roots.append(os.path.dirname(os.path.dirname(
            os.path.abspath(config.__file__))))
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        roots.append(os.getcwd())
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        logger.debug("silent-except", exc_info=True)
    out = []
    for r in roots:
        if r and r not in out:
            out.append(r)
    return out


def _clean_claim_path(s) -> str:
    """把正则抓到的片段清理成路径：去引号/括号/句末标点，压掉空白。"""
    t = " ".join(str(s or "").split())
    t = t.strip("「」【】()[]{}<>\"'“”‘’《》")
    return t.rstrip("。，,;；:：!！?？")


def _looks_like_path(s: str) -> bool:
    """是不是一个「文件路径」的写法 —— 必须有目录分隔符或扩展名。

    只写「新增功能 / 新增记录」这类中文词不算声称新建文件，不参与核对。
    """
    if not s or len(s) > 200:
        return False
    if "/" in s or "\\" in s:
        return True
    # 有扩展名才算文件名（排除 "3.14" 这种纯数字）
    return bool(re.search(r"\.[A-Za-z0-9]{1,10}$", s))


def _claim_file_exists(p: str, roots=None) -> bool:
    """声称的文件是否真实存在：绝对路径直接用，相对路径逐个根试（不抛异常）。"""
    s = _clean_claim_path(p)
    if not s:
        return False
    cands = []
    if os.path.isabs(s):
        cands.append(s)
    else:
        rel = s.replace("\\", os.sep).replace("/", os.sep)
        for r in (roots if roots is not None else _claim_roots()):
            cands.append(os.path.join(r, rel))
        cands.append(rel)
    for c in cands:
        try:
            if os.path.exists(c):
                return True
        except Exception:
            logger.debug("silent-except", exc_info=True)
    return False


def _claim_texts(row) -> list:
    """一行记录里可能带「新建/新增 <path>」声称的文本字段。"""
    out = []
    if isinstance(row, dict):
        for k in ("detail", "m", "a"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
    return out


def _claim_rows(window: int, session: str = "") -> list:
    """取要回扫的记录：会话账本（或跨会话最近 window 条）+ 计划阶段流水。"""
    rows = []
    try:
        if session:
            rows = _read(_path(session))[-window:]
        else:
            rows = _recent_session_rows(window)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    # 阶段流水单独落盘（data/actlog/stages/*.jsonl），detail 字段就在那里
    try:
        sdir = os.path.join(_dir(), "stages")
        for name in sorted(os.listdir(sdir)):
            if name.endswith(".jsonl"):
                rows.extend(_read(os.path.join(sdir, name))[-window:])
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return rows


def _note_claim_mismatch(item: dict) -> None:
    """把一次「声称新建但文件不存在」记成 claim_mismatch 事件（异常全吞）。"""
    p = str((item or {}).get("path") or "")
    if not p:
        return
    try:
        now = time.time()
        if now - float(_CLAIM_SEEN.get(p) or 0.0) < _CLAIM_GAP:
            return
        if len(_CLAIM_SEEN) >= _CLAIM_KEEP:
            return
        _CLAIM_SEEN[p] = now
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        note = ("声称新建但文件不存在：%s（记录时间 %s）"
                % (p, str((item or {}).get("t") or "?")))
        record(_CLAIM_SESSION, "claim_mismatch", {"path": p, "rel": p}, ok=False,
               result=note, evidence={"fail_kind": "claim_mismatch"})
        append_watch_event(_CLAIM_SESSION,
                           {"kind": "claim_mismatch", "key": p, "state": "miss",
                            "note": note, "count": 1})
    except Exception:
        logger.debug("silent-except", exc_info=True)


def audit_history_claims(window: int = COUNT_WINDOW, session: str = "",
                         path: str = "", record_events: bool = True,
                         roots=None) -> dict:
    """回扫历史记录：凡是写着「新建/新增 <path>」的，核对该文件是否真实存在。

    为什么需要（2026-11-24 用户要求）：
      账本里「已完成」的记录会被下一轮当成事实引用 —— detail 里一句
      「新建 backend/foo.py」看着像证据，其实文件从没落盘。这里把这类声称
      当断言逐条验证，对不上的记 claim_mismatch 事件，前端体检卡显示
      「历史记录不一致：N 条」，用户一眼知道旧记录不能全信。

    参数：
      window         扫最近多少条日志（<=0 按 COUNT_WINDOW=300 处理）
      session        非空 → 只扫该会话账本；空 → 跨会话扫最近的
      path           显式指定单个账本文件（测试用；给了就只扫这个文件）
      record_events  是否把不一致写进 claim_mismatch 事件（默认写）
      roots          相对路径的解析根（测试用；默认项目根 / cwd / backend）

    返回 dict（任何异常都返回 ok=False 的空结果，绝不抛）：
      {"ok": True, "ts": str, "window": int, "scanned": int,
       "claims": int,          # 命中「新建/新增 <path>」的次数
       "mismatch": int,        # 文件不存在的不一致条数（同路径只算 1 条）
       "count": int,           # mismatch 的别名，前端/体检卡直接用
       "items": [{"path","claim","t","text","session"}, ...],
       "missing": [路径, ...]}
    """
    out = {"ok": True, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "window": COUNT_WINDOW, "scanned": 0, "claims": 0,
           "mismatch": 0, "count": 0, "items": [], "missing": []}
    try:
        try:
            k = int(window)
        except (TypeError, ValueError):
            k = COUNT_WINDOW
        if k <= 0:
            k = COUNT_WINDOW
        out["window"] = k
        try:
            rows = _read(path)[-k:] if path else _claim_rows(k, session)
        except Exception:
            logger.debug("silent-except", exc_info=True)
            rows = []
        if not isinstance(rows, list):
            rows = []
        out["scanned"] = len(rows)
        items = []
        seen = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            # 自己不核对自己的事件行（note 里也含「新建」二字）
            if str(r.get("n") or "") == "claim_mismatch":
                continue
            for text in _claim_texts(r):
                for m in _CLAIM_RE.finditer(text):
                    p = _clean_claim_path(m.group(1))
                    if not _looks_like_path(p):
                        continue
                    out["claims"] += 1
                    if _claim_file_exists(p, roots):
                        continue
                    if p in seen:
                        continue
                    seen.add(p)
                    items.append({"path": p,
                                  "claim": " ".join(m.group(0).split())[:80],
                                  "t": str(r.get("t") or r.get("ts") or "")[:19],
                                  "text": " ".join(text.split())[:160],
                                  "session": session or ""})
        items = items[:50]
        out["items"] = items
        out["mismatch"] = len(items)
        out["count"] = len(items)
        out["missing"] = [it["path"] for it in items]
        if record_events and items:
            for it in items:
                _note_claim_mismatch(it)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        out["ok"] = False
    return out


def clear(session: str) -> None:
    try:
        os.remove(_path(session))
    except Exception:
        logger.debug("silent-except", exc_info=True)
