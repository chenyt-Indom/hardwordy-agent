# -*- coding: utf-8 -*-
"""重启后自动自检（boot audit）—— 每次重启起来先验一遍自己的代码。

用户要求（2026-11-16）
--------------------
「这个自检给他弄成每次自迭代重启之后自动检验，然后帮我反复验证是否正常」

为什么原来会漏
-------------
应用重启，绝大多数时候就发生在「自我迭代 / 自我改进刚改完代码」之后。
而改代码最怕的是**改完还能启动，但里面悄悄断了**（调用不存在的函数、
前端调了删掉的路由、同名函数覆盖……）。原来有两道检查，中间是空的：

· ``selftest.run_all()`` —— 启动时就跑，但它查的是**服务健康**
  （模型能不能用、端口通不通、ffmpeg 在不在），**不看代码本身**；
· ``selfmaint`` 的 ``selfaudit`` —— 静态扫代码，但每天 05:10 才跑一次。

所以「刚改完代码、刚重启完」这个最该验的时刻，恰恰没人验。本模块补上它。

三条铁律（不守就会出事，别改）
------------------------------
1. **只查不改**：``auto_fix`` 硬编码 False。信任模式开着时，若允许重启自检
   自动改代码，就会变成「自检改代码 → 需要重启 → 重启又自检 → 又改代码」的
   死循环，用户看到的就是「程序被反复修改」。
2. **不建计划**：``create_plan=False``。否则每重启一次就堆一个待办计划，
   计划列表很快会被无意义的重复条目淹掉。
3. **绝不拖累启动**：全程后台线程 + 延迟启动 + 所有异常吞掉。自检哪怕崩了，
   也不能让服务起不来（服务起不来 = 用户完全没法用，比没自检严重得多）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import config

logger = logging.getLogger(__name__)

# 同一进程内只跑一次（startup 钩子理论上只调一次，但万一被重复注册，
# 这里兜一层，避免并发跑两遍自检）
_ONCE = {"started": False}

# 结果文件读写的锁
_LOCK = threading.Lock()

# 防抖：距上次自检不到这么久就跳过。
# 为什么要它：开发调试时会连续点几次重启，每次重启都全量扫一遍 99 个 py
# 纯属浪费（一次 6~7 秒 CPU）。60 秒内重复重启算「同一次」，跳过并记录原因。
DEBOUNCE_SEC = 60.0

# 启动后延迟多久才跑：让 selftest / 调度线程 / 模型预热先起来，
# 别在服务刚能响应的时候抢 CPU（那时候用户正在点界面）。
DELAY_SEC = 6.0

# 保留多少条历史
_HIST_MAX = 20

# 自检结果缓存：同版本 + 24h 内命中则跳过重算
_CACHE_MAX = 50
_CACHE_TTL_SEC = 24 * 3600.0


def _path() -> str:
    return config.data("data", "boot_audit.json")


def _cache_path() -> str:
    """自检结果缓存文件路径（与主结果文件分开，避免互相污染）。"""
    return config.data("data", "boot_audit_cache.json")


def _cache_blank() -> dict:
    return {"records": []}


def _cache_load_raw() -> dict:
    try:
        p = _cache_path()
        if not os.path.isfile(p):
            return _cache_blank()
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return _cache_blank()
        if not isinstance(d.get("records"), list):
            d["records"] = []
        return d
    except Exception:                                    # noqa: BLE001
        logger.debug("bootaudit 缓存读取失败，按空处理", exc_info=True)
        return _cache_blank()


def _cache_save_raw(d: dict) -> None:
    try:
        p = _cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                    # noqa: BLE001
        logger.warning("bootaudit 缓存写盘失败（不影响使用）", exc_info=True)


def _code_version() -> str:
    """当前代码版本指纹：用 backend 目录下 py 文件的 mtime+size 拼一个短哈希。

    目的：同版本 + 24h 内命中缓存就跳过重算。用 mtime+size 而不是内容哈希，
    是因为扫 99 个 py 算 sha 本身就要几百毫秒，得不偿失；mtime+size 足够
    区分「代码有没有被改过」。
    """
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        items = []
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            fp = os.path.join(base, name)
            try:
                st = os.stat(fp)
            except Exception:                            # noqa: BLE001
                continue
            items.append("%s:%d:%d" % (name, int(st.st_mtime), int(st.st_size)))
        raw = "|".join(items)
        import hashlib
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:                                    # noqa: BLE001
        logger.debug("计算代码版本失败，返回空版本", exc_info=True)
        return ""


def load_cache() -> list:
    """读取全部缓存记录（按时间升序）。"""
    try:
        recs = list(_cache_load_raw().get("records") or [])
    except Exception:                                    # noqa: BLE001
        return []
    return recs


def append_cache(record: dict) -> dict:
    """追加一条缓存记录，自动补 id / ts / version，并裁剪到 _CACHE_MAX。"""
    if not isinstance(record, dict):
        return {}
    rec = dict(record)
    rec.setdefault("ts_unix", time.time())
    rec.setdefault("ts", _now())
    rec.setdefault("version", _code_version())
    if not rec.get("id"):
        rec["id"] = "%d-%s" % (int(rec["ts_unix"] * 1000),
                                str(rec.get("version") or "")[:8])
    with _LOCK:
        d = _cache_load_raw()
        recs = list(d.get("records") or [])
        recs.append(rec)
        d["records"] = recs[-_CACHE_MAX:]
        _cache_save_raw(d)
    return rec


def delete_cache(cid: str) -> bool:
    """按 id 删除一条缓存记录，返回是否删掉了。"""
    cid = str(cid or "")
    if not cid:
        return False
    with _LOCK:
        d = _cache_load_raw()
        recs = list(d.get("records") or [])
        kept = [r for r in recs if str(r.get("id")) != cid]
        if len(kept) == len(recs):
            return False
        d["records"] = kept
        _cache_save_raw(d)
    return True


def clear_cache() -> int:
    """清空全部缓存，返回清掉的条数。"""
    with _LOCK:
        d = _cache_load_raw()
        n = len(d.get("records") or [])
        d["records"] = []
        _cache_save_raw(d)
    return n


def _cache_hit(version: str) -> dict:
    """同版本 + 24h 内命中则返回那条记录，否则返回空 dict。"""
    if not version:
        return {}
    now = time.time()
    try:
        recs = load_cache()
    except Exception:                                    # noqa: BLE001
        return {}
    for rec in reversed(recs):
        try:
            if str(rec.get("version") or "") != version:
                continue
            if now - float(rec.get("ts_unix") or 0) > _CACHE_TTL_SEC:
                continue
            return rec
        except Exception:                                # noqa: BLE001
            continue
    return {}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _blank() -> dict:
    return {"last": {}, "runs": []}


def _load() -> dict:
    try:
        p = _path()
        if not os.path.isfile(p):
            return _blank()
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return _blank()
        if not isinstance(d.get("runs"), list):
            d["runs"] = []
        if not isinstance(d.get("last"), dict):
            d["last"] = {}
        return d
    except Exception:                                    # noqa: BLE001
        logger.debug("bootaudit 读取失败，按空处理", exc_info=True)
        return _blank()


def _save(d: dict) -> None:
    try:
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)                               # 原子替换，断电不会留半截文件
    except Exception:                                    # noqa: BLE001
        logger.warning("bootaudit 结果写盘失败（不影响使用）", exc_info=True)


def _verdict(high: int, mid: int, regress_ok) -> str:
    """把结果压成一个三档结论，给前端上色用。

    fail —— 有 high（语法/调用不存在这类，会真崩）或行为回归失败
    warn —— 只有 mid
    pass —— 干净（low/info 不算问题，那两类是提示性质的）
    """
    if high > 0 or regress_ok is False:
        return "fail"
    if mid > 0:
        return "warn"
    return "pass"


def _summ(d: dict) -> str:
    """生成一句人能直接看懂的话。"""
    v = d.get("verdict")
    icon = {"pass": "✅ 通过", "warn": "⚠️ 有警告", "fail": "❌ 未通过"}.get(v, "• 未知")
    parts = ["%s · 扫 %s 个 py + %s 个 js" % (icon, d.get("files", "?"),
                                             d.get("js_files", "?"))]
    parts.append("high %s / mid %s / low %s"
                 % (d.get("high", 0), d.get("mid", 0), d.get("low", 0)))
    if d.get("regress_summary"):
        parts.append("回归 %s" % d["regress_summary"])
    if d.get("seconds") is not None:
        parts.append("%.1f 秒" % float(d["seconds"]))
    return " · ".join(parts)


def run(reason: str = "", force: bool = False) -> dict:
    """跑一次「重启后自检」并把结果落盘。

    参数
    ----
    reason : 这次重启的原因（取自 handoff 交接卡，纯展示用，不影响判定）
    force  : True 绕过防抖（用户手动点「立刻验一次」时用）

    返回的 dict 一定带 ``ok``；``skipped=True`` 表示被防抖拦下，不算失败。
    """
    prev = _load()
    last = prev.get("last") or {}

    # ---- 防抖：刚跑过就别再跑（反复重启时不浪费 CPU）
    if not force:
        try:
            gap = time.time() - float(last.get("ts_unix") or 0)
        except Exception:                                # noqa: BLE001
            gap = 1e9
        if gap < DEBOUNCE_SEC:
            rec = dict(last)
            rec["skipped"] = True
            rec["skip_reason"] = ("距上次重启自检只过了 %d 秒（< %d 秒），本次跳过"
                                  % (int(gap), int(DEBOUNCE_SEC)))
            return {"ok": True, "skipped": True, "reason": rec["skip_reason"],
                    "verdict": last.get("verdict"), "last": last}

    t0 = time.time()
    try:
        from . import self_iterate as _si
        # ⚠️ source 必须区分开：run_audit 内部对「非 manual/after_change」的来源
        #    有 60 秒冷却（防重复建计划）。如果手动点「立刻验一次」也用
        #    after_restart，就会被那层冷却静默拦下 —— 用户点了却什么也没跑，
        #    还显示「通过」，等于骗人。所以：
        #      · 启动自动跑 → after_restart（有冷却，防抖，合理）
        #      · 手动触发   → manual（豁免冷却，点了一定真跑）
        # ★★ auto_fix / create_plan 这两项是铁律，别改成 True ★★
        r = _si.run_audit(deep=False,
                          source=("manual" if force else "after_restart"),
                          create_plan=False, auto_fix=False, verify=False)
    except Exception as e:                               # noqa: BLE001
        rec = {"ts": _now(), "ts_unix": time.time(), "reason": str(reason or "")[:120],
               "ok": False, "verdict": "fail",
               "error": "%s: %s" % (type(e).__name__, e),
               "seconds": round(time.time() - t0, 1),
               "files": 0, "js_files": 0, "high": 0, "mid": 0, "low": 0, "info": 0}
        rec["text"] = "❌ 自检器本身崩了：%s" % rec["error"]
        _record(rec)
        logger.warning("重启后自检异常：%s", rec["error"])
        return {"ok": False, "error": rec["error"], "last": rec}

    if r.get("skipped"):
        # 被 run_audit 内部的并发闸拦下（正常现象：刚好有别的自检在跑）。
        # 带上上次的 verdict —— 否则前端拿到 null 会显示成「未知」，
        # 而用户真正想知道的是「上一次到底有没有问题」。
        return {"ok": True, "skipped": True,
                "reason": str(r.get("error") or "已有一次自检在跑"),
                "verdict": last.get("verdict"), "last": last}

    if not r.get("ok"):
        rec = {"ts": _now(), "ts_unix": time.time(), "reason": str(reason or "")[:120],
               "ok": False, "verdict": "fail",
               "error": str(r.get("error") or "未知错误")[:300],
               "seconds": round(time.time() - t0, 1),
               "files": 0, "js_files": 0, "high": 0, "mid": 0, "low": 0, "info": 0}
        rec["text"] = "❌ 自检失败：%s" % rec["error"]
        _record(rec)
        return {"ok": False, "error": rec["error"], "last": rec}

    a = r.get("audit") or {}
    cnt = a.get("counts") or {}
    high = int(cnt.get("high") or 0)
    mid = int(cnt.get("mid") or 0)
    low = int(cnt.get("low") or 0)
    info = int(cnt.get("info") or 0)

    # 行为回归：run_audit 只在**失败**时才把它挂到 audit["logic_regress"] 上
    rg = a.get("logic_regress") or {}
    regress_ok = None if not rg else bool(rg.get("ok"))
    regress_summary = str(rg.get("summary") or "") if rg else ""

    rec = {"ts": _now(), "ts_unix": time.time(),
           "reason": str(reason or "")[:120], "ok": True,
           "files": a.get("files"), "js_files": a.get("js_files"),
           "total": a.get("total"), "high": high, "mid": mid,
           "low": low, "info": info,
           "audit_seconds": a.get("seconds"),
           "seconds": round(time.time() - t0, 1),
           "regress_ok": regress_ok, "regress_summary": regress_summary}
    rec["verdict"] = _verdict(high, mid, regress_ok)
    rec["text"] = _summ(rec)
    _record(rec)

    logger.info("重启后自检完成：%s", rec["text"])
    return {"ok": True, "last": rec, "verdict": rec["verdict"]}


def _record(rec: dict) -> None:
    with _LOCK:
        d = _load()
        d.pop("skip", None)      # 真的跑过了，「本次未自检」就不再成立
        d["last"] = rec
        runs = list(d.get("runs") or [])
        runs.append(rec)
        d["runs"] = runs[-_HIST_MAX:]
        _save(d)
    # 再同步一份到「迭代想法 → 自我维护 → 重启自检」那一项。
    # 界面上的「上次运行 / 历史 / 立即运行」都是从 selfmaint 读的，
    # 不写过去的话，用户会看到「还没跑过」，而实际刚跑完。
    # 放在 _LOCK 外面：两把锁不嵌套，避免以后有人反向调用时死锁。
    try:
        from . import selfmaint as _sm
        _sm.record_boot(rec)
    except Exception:                                    # noqa: BLE001
        logger.debug("同步到自我维护失败（不影响自检结果）", exc_info=True)


def last() -> dict:
    """最近一次结果（没跑过返回空 dict）。"""
    try:
        return dict(_load().get("last") or {})
    except Exception:                                    # noqa: BLE001
        return {}


def history(limit: int = 10) -> list:
    try:
        runs = list(_load().get("runs") or [])
    except Exception:                                    # noqa: BLE001
        return []
    return runs[-max(1, int(limit or 10)):]


def status() -> dict:
    """给接口 / 前端用：最近结果 + 节流参数 + 历史。

    前端「🌱 迭代想法 → 🛠 自我维护」里显示一行：
        「重启自检：✅ 通过 · 0 high / 0 mid · 6.7 秒」
    """
    l = last()
    try:
        skip = dict(_load().get("skip") or {})
    except Exception:                                     # noqa: BLE001
        skip = {}
    # 缓存统计：条数 + 最近一条缓存时间，给前端展示「缓存命中情况」。
    try:
        cache_recs = load_cache()
    except Exception:                                     # noqa: BLE001
        cache_recs = []
    cache_count = len(cache_recs)
    last_cache_at = ""
    if cache_recs:
        try:
            last_cache_at = str(cache_recs[-1].get("ts") or "")
        except Exception:                                 # noqa: BLE001
            last_cache_at = ""
    # 2026-11-16：重启自检已整体关闭，把这件事作为**恒定字段**告诉调用方，
    # 不再靠「有没有 skip 记录」间接推断（skip 可能被别的路径覆盖）。
    disabled_why = "重启自检已关闭，只保留每天 05:10 的代码自检"
    return {"ok": True, "last": l, "history": history(10), "skip": skip,
            "start_disabled": True, "start_disabled_why": disabled_why,
            "debounce_sec": DEBOUNCE_SEC, "delay_sec": DELAY_SEC,
            "cache_count": cache_count,
            "last_cache_at": last_cache_at,
            # text 是「当前状态」不是「上次结果」：拿上一次的自检结论冒充现在
            # 会让人以为「刚跑过一次」—— 上次结果在 last 里，别混用。
            "text": disabled_why,
            "last_text": l.get("text") or ""}


def _record_skip(dec: dict) -> None:
    """记下「这次启动没自检，以及为什么」。

    ⚠️ 故意**不走 _record**：那个会写 last/runs 并同步给 selfmaint，
    界面上就会显示成「刚运行过」—— 明明没跑却显示跑过，是骗人。
    单独存一个 skip 字段，前端读它显示「本次未自检：冷启动」。
    """
    try:
        with _LOCK:
            d = _load()
            d["skip"] = {"ts": time.time(),
                         "at": _now(),
                         "kind": str(dec.get("kind") or ""),
                         "why": str(dec.get("why") or "")[:200]}
            _save(d)
    except Exception:                                     # noqa: BLE001
        logger.debug("记录「本次未自检」失败（不影响启动）", exc_info=True)


# 2026-11-16：原来这里还有 `should_run()` 和 `_reason_from_handoff()` 两个
# 函数，用来「读 bootgate 标记 → 判断这次启动要不要自检」。用户要求把重启
# 自检整体去掉之后，启动路径恒定跳过，这两个函数再没有人调用，而且留着
# 会让后来的人以为「重启自检还能被打开」。所以整段删除。
# ⚠️ 不要恢复它们：现在「要不要自检」已经不是一个问题 —— 启动永不自检。


def start_delayed(delay: float = None) -> dict:
    """启动钩子里调用：**现在恒定不自检**（2026-11-16 用户要求）。

    用户原话：「把重启自检去掉，只保留每天自检就可以」。
    所以「重启后自检」这条链路整体关闭 —— 无论冷启动、自动迭代后重启、
    还是点任何按钮重启，启动时都**不再扫代码**。

    真正的自检只剩一项：`selfmaint` 里每天 05:10 的「代码自检」
    （走 scheduler 的 _run_selfaudit），与启动无关。

    ⚠️ 为什么不直接删掉这个函数：`main.py` 的启动钩子会调它，
    删了要同时改两处，且启动钩子里的异常会污染启动流程。
    保留函数、只让它恒定跳过，是最小改动、最不容易崩的做法。

    仍然写一条 skip 记录（不写 last/runs），这样 `/api/bootaudit`
    能如实说明「重启自检已关闭」，而不是让界面显示成「还没跑过」。
    """
    # ★ 2026-11-16 用户再次明确：「把重启自检去掉，只保留每天自检」。
    #   这里直接 return —— 函数体保留（run() 仍供每日自检 / 手动触发复用），
    #   但启动流程不再触发任何代码扫描。
    if _ONCE.get("started"):
        return {"ok": True, "already": True}
    _ONCE["started"] = True

    why = "重启自检已关闭，只保留每天 05:10 的代码自检"

    # ---- 先读缓存：同版本 + 24h 内命中则跳过重算，并记一条 skip。
    #      注意：这里只读缓存、不写 last/runs，避免让界面误以为「刚跑过」。
    try:
        ver = _code_version()
        hit = _cache_hit(ver)
    except Exception:                                    # noqa: BLE001
        ver, hit = "", {}
    if hit:
        try:
            _record_skip({"audit": False, "kind": "cache_hit",
                          "why": "同版本 24h 内已有自检结果，跳过重算",
                          "cache_id": hit.get("id"),
                          "cache_ts": hit.get("ts"),
                          "version": ver})
        except Exception:                                # noqa: BLE001
            logger.debug("记录缓存命中失败（不影响启动）", exc_info=True)
        logger.info("[bootaudit] 命中缓存，跳过重算：%s", hit.get("id"))
        return {"ok": True, "skipped": True, "kind": "cache_hit",
                "why": "同版本 24h 内已有自检结果，跳过重算",
                "cache": hit, "version": ver}

    # 写一条 skip（不写 last/runs）：`/api/bootaudit` 才能如实回答
    # 「为什么这次启动没自检」，而不是让调用方以为「还没跑过」。
    try:
        _record_skip({"audit": False, "kind": "disabled", "why": why})
    except Exception:                                    # noqa: BLE001
        logger.debug("记录「重启自检已关闭」失败（不影响启动）", exc_info=True)
    logger.info("[bootaudit] 本次不自检：%s", why)
    return {"ok": True, "skipped": True, "kind": "disabled", "why": why}
