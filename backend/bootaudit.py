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


def _path() -> str:
    return config.data("data", "boot_audit.json")


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
    return {"ok": True, "last": l, "history": history(10), "skip": skip,
            "debounce_sec": DEBOUNCE_SEC, "delay_sec": DELAY_SEC,
            "text": l.get("text") or "还没跑过重启自检"}


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


def _reason_from_handoff() -> str:
    """从交接卡里取这次重启的原因，写进记录里方便回溯。

    交接卡（handoff.py）在重启前会写下 reason（如「前端保存会话（实时落盘）」
    或「自我迭代改完代码」），拿它当标签，比空着有用。
    """
    try:
        from . import handoff as _h
        d = _h._read()                                   # noqa: SLF001
        return str(d.get("reason") or "")[:120]
    except Exception:                                    # noqa: BLE001
        return ""


def should_run(reason: str = "") -> dict:
    """★ 2026-11-16 重启原因白名单：**只有用户手动重启才自检**。

    为什么：自我迭代改完代码 → 重启 → 重启后自检 → 自检又触发迭代 →
    又重启…… 用户看到的就是「程序被反复修改、反复重启」。
    切断点就在这里：自动迭代 / 冷启动一律跳过，只有用户手动重启才真跑。

    参数
    ----
    reason : 重启原因类别（selfdev_iterate / user_restart / cold_start）。
             传空串时自动从交接卡读（handoff.reason_kind()）。

    返回
    ----
    dict：``{"audit": bool, "kind": str, "why": str}``
      · audit=True  → 该跑自检
      · audit=False → 跳过（why 里写清为什么）

    ⚠️ 判不出来时**放行**（audit=True）：宁可多跑一次自检，也不要因为
       判据坏了就永远不查 —— 自检本身只查不改，多跑一次没有副作用。
    """
    # ★ 判据**只认 bootgate 的磁盘标记**，reason 只当日志用。
    #   原因：交接卡的 reason 文本分不出「谁点的重启」——自动迭代重启和
    #   用户点「立即重启」，写进交接卡的字面完全一样。
    #
    #   现行规则（2026-11-16 用户第二次更正）：
    #     · 点了「新版本已就绪」弹窗的「立即重启」→ **自检**（唯一入口）
    #     · 抽屉「一键重启」/「只重启服务」/ 自动迭代后重启 / 冷启动
    #       → **一律不自检**
    #     · decide() 抛异常 → **不自检**（宁可漏查一次，也不要在无人值守时转圈）
    try:
        from . import bootgate as _bg
        d = _bg.decide()
        if not d.get("kind"):
            d["kind"] = "unknown"
        return d
    except Exception as e:                               # noqa: BLE001
        logger.warning("重启闸门判断失败，按「无标记」处理（不自检）：%s", e)
        return {"audit": False, "kind": "gate_error",
                "why": "闸门异常，按无标记冷启动处理，不自检"}


def start_delayed(delay: float = None) -> dict:
    """启动钩子里调用：起一个后台线程，延迟几秒后跑一次自检。

    **绝不阻塞、绝不抛异常** —— 服务能不能起来，和自检跑没跑成没关系。
    """
    if _ONCE.get("started"):
        return {"ok": True, "already": True}
    _ONCE["started"] = True

    # ★★ 2026-11-16 用户要求：不是每次启动都要自检。
    #   「自动迭代更新重启不要自检 / 关闭到打开也不要自检，否则可能无限自检」。
    #   判据**只认 bootgate 标记**（data/boot_gate.json 里的 mark）：
    #     · 没有标记 / 标记过期 → 冷启动 → 跳过
    #     · 有标记 + auto_iter=True → 自动迭代重启 → 跳过
    #     · 有标记 + auto_iter=False → 用户手动重启 → 才真跑
    #   判据自己出问题时**不自检**（按无标记处理）—— 用户明确要求
    #   「无标记的冷启动默认不自检」，闸门坏了等于判不出标记，就不该自检。
    try:
        dec = should_run(_reason_from_handoff())
    except Exception as e:                                # noqa: BLE001
        logger.warning("重启原因判断失败，按无标记处理（不自检）：%s", e)
        dec = {"audit": False, "why": "判据异常，按无标记冷启动处理，不自检",
               "kind": "gate_error"}

    if not dec.get("audit"):
        _record_skip(dec)
        logger.info("[bootaudit] 本次不自检：%s", dec.get("why"))
        return {"ok": True, "skipped": True, "kind": dec.get("kind"),
                "why": dec.get("why")}

    d = DELAY_SEC if delay is None else float(delay)

    def _worker() -> None:
        try:
            time.sleep(max(0.0, d))
        except Exception:                                # noqa: BLE001
            pass
        try:
            run(reason=_reason_from_handoff())
        except Exception:                                # noqa: BLE001
            logger.warning("重启后自检没跑成（不影响使用）", exc_info=True)

    try:
        threading.Thread(target=_worker, name="boot-audit", daemon=True).start()
        return {"ok": True, "delay": d}
    except Exception as e:                               # noqa: BLE001
        logger.warning("重启后自检线程起不来：%s", e)
        return {"ok": False, "error": str(e)}
