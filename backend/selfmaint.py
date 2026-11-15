# -*- coding: utf-8 -*-
"""自我维护（self-maintenance）—— 反思 / 自检 / 体验评估 / 抽空自检。

用户要求（2026-11-15）
--------------------
「我想要反思自检评估以及抽空代码自检这些内容**彻底成为这个项目的一项功能**，
不要放在自动化里面，而是放在**迭代想法**里面。」

所以这四项**不再是一条用户可建的定时任务**，而是代码里的一等功能：

· **定义在代码里**（下面 _DEFS 是唯一权威来源）—— 不再靠 schedules.json
  记录「它们存在」。文件丢了、换机器、清空任务，都照样在。
· **入口在「🌱 AI 迭代想法」抽屉**的「自我维护」区块，每项一行：
  上次运行时间 / 结果摘要 / 暂停·重启 / 立即运行 / 历史。
· **节律仍然保留**（用户：「保留自动节律，后台照旧按时间跑，只是入口和管理
  挪进迭代想法；可以选择暂停或重启」「抽空自检保留闲时自动触发」）——
  调度循环每次 tick 问一句 due_items()，到点就真跑。
· **状态存自己的文件** data/self_maint.json（节律、启停、上次结果、历史），
  和用户的定时任务（schedules.json）彻底分开，互不干扰。

设计取舍
--------
1. **执行逻辑不搬家**：真正干活的还是 scheduler 里的 _run_selfiter /
   _run_selfaudit / _run_selfexp / _run_selfidle（它们会写会话、写日志 md，
   用户要求「历史结果保留、能翻到」）。本模块只负责**定义 + 状态 + 调度 +
   触发**，不重复实现一份，避免两处行为漂移。
2. **循环导入**：scheduler 顶层 import 本模块；本模块只在函数内 import
   scheduler（延迟导入），所以不会成环。
3. **自愈不推翻用户设置**：文件缺失/损坏时重建默认值；但已存在的项一律
   不动 —— 用户暂停了就保持暂停，改过时间就保持他的时间。
4. **暂停有两层**：总开关在 self_iterate 的 settings.paused（用户要保留的那个
   「自动化 暂停/继续」按钮）；这里是**逐项**启停。两层是「与」的关系：
   任一层说停，自动触发就不跑；但用户手动点「立即运行」永远能跑。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import config
import logging

logger = logging.getLogger(__name__)

# 逐项状态的读写锁（调度线程 + 接口线程会同时动）
_LOCK = threading.Lock()

# ---------------------------------------------------------------- 定义
# 这四项是**代码的一部分**：下面这份清单就是它们的「存在证明」。
#   icon/desc  给前端「自我维护」区块显示
#   at_time    每天 HH:MM 跑一次
#   every_minutes  每 N 分钟跑一次（selfidle 是「抽空」，实际还会过 idle_gate）
#   gate       自动触发时是否要先过 self_iterate.idle_gate（闲时才跑）
#   legacy_name  这一项当年作为「定时任务」时的名字 —— 用来把**旧日志**
#                （文件名是旧 job id 前缀、标题是旧名字）也认领到本项名下，
#                用户要求「以前跑出来的历史结果保留，在迭代想法里能翻到」。
_DEFS = (
    {"kind": "selfiter", "name": "自我反思", "icon": "🔄",
     "desc": "读论文 + 工作记录，构思可加的功能，产出计划等你批准",
     "at_time": "04:30", "every_minutes": 0, "gate": False,
     "legacy_name": "每日自我反思"},
    {"kind": "selfaudit", "name": "代码自检", "icon": "🔍",
     "desc": "静态扫自己的代码，查语法 / 调用 / 裸 except / 重复定义",
     "at_time": "05:10", "every_minutes": 0, "gate": False,
     "legacy_name": "每日代码自检"},
    {"kind": "selfexp", "name": "体验评估", "icon": "📊",
     "desc": "评估最近几轮对话的完成度与效率，不好就想怎么改",
     "at_time": "", "every_minutes": 120, "gate": False,
     "legacy_name": "定时体验评估"},
    {"kind": "selfidle", "name": "重启自检", "icon": "🌙",
     "desc": "每次重启后扫一遍自己的代码，只查不改",
     "at_time": "", "every_minutes": 0, "gate": False, "on_boot": True,
     "legacy_name": "抽空代码自检"},
)

KINDS = tuple(d["kind"] for d in _DEFS)
_BY_KIND = {d["kind"]: d for d in _DEFS}

# 每项保留多少条历史（够了；再久远的去 schedule_logs 的 md 里翻）
_HIST_MAX = 20


def _path() -> str:
    return config.data("data", "self_maint.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 节律计算
def _parse_hhmm(s: str):
    """'04:30' -> (4, 30)；非法返回 None。"""
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:                                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return None


def next_run(it: dict, base: float = None) -> float:
    """算下一次该跑的时间戳（0 = 不排期）。逻辑与 scheduler._next_run 一致。"""
    base = time.time() if base is None else base
    # 重启触发项（on_boot）不排期：返回 0 = 永不自动到点，
    # 由启动钩子调 bootaudit 来触发，见 record_boot()。
    if it.get("on_boot"):
        return 0
    if it.get("every_minutes"):
        try:
            mins = max(1, int(it["every_minutes"]))
        except Exception:                                # noqa: BLE001
            return 0
        return base + mins * 60
    if it.get("at_time"):
        hm = _parse_hhmm(it["at_time"])
        if not hm:
            return 0
        t = time.localtime(base)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                              hm[0], hm[1], 0, 0, 0, -1))
        if target <= base:
            target += 86400
        return target
    return 0


def describe(it: dict) -> str:
    if it.get("on_boot"):
        return "每次重启后"
    if it.get("every_minutes"):
        return "每 %s 分钟" % it["every_minutes"]
    if it.get("at_time"):
        return "每天 %s" % it["at_time"]
    return "未设置节律"


# ---------------------------------------------------------------- 状态存取
def _blank_item(d: dict) -> dict:
    it = {"kind": d["kind"], "enabled": True,
          "at_time": d["at_time"], "every_minutes": d["every_minutes"],
          "on_boot": bool(d.get("on_boot")),
          "last_run": "", "last_status": "", "last_preview": "",
          "last_seconds": 0, "next_run": 0, "runs": []}
    it["next_run"] = next_run(it)
    return it


def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _ensure(raw: dict = None) -> dict:
    """补齐缺失的项，返回完整状态。**已存在的一律不动**（不推翻用户设置）。"""
    raw = _load_raw() if raw is None else raw
    items = raw.get("items")
    if not isinstance(items, dict):
        items = {}
    changed = False
    for d in _DEFS:
        cur = items.get(d["kind"])
        if not isinstance(cur, dict):
            items[d["kind"]] = _blank_item(d)
            changed = True
            continue
        # 老数据缺字段就补默认，但**不改用户已经设过的值**
        base = _blank_item(d)
        for k, v in base.items():
            if k not in cur:
                cur[k] = v
                changed = True
        if not isinstance(cur.get("runs"), list):
            cur["runs"] = []
            changed = True
        # 2026-11-16：selfidle 从「每 20 分钟」改成「每次重启后」。
        # 老数据里存着 every_minutes=20 和排好的 next_run，光改 _DEFS 不生效
        # （_ensure 的规矩是「已存在的值不动」），所以这里显式纠正一次：
        # 节律归零 → due_items / catchup 都不会再自动调度它。
        if d.get("on_boot"):
            if cur.get("every_minutes"):
                cur["every_minutes"] = 0
                changed = True
            if cur.get("next_run"):
                cur["next_run"] = 0
                changed = True
    out = dict(raw)
    out["items"] = items
    if changed:
        _save(out)
    return out


def _save(state: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                    # noqa: BLE001
        logger.debug("selfmaint 写盘失败", exc_info=True)


# ---------------------------------------------------------------- 迁移
def migrate_from_schedules() -> list:
    """把 schedules.json 里遗留的四条内置任务搬进来，并从那边删掉。

    用户要求「定时任务列表里原来那四条内置任务清掉，只留我自己建的任务」。
    迁移是**一次性**的：搬走节律（at_time / every_minutes）和上次运行状态，
    然后从 schedules.json 移除。之后再调就是空操作（找不到 kind 任务）。

    返回被迁移走的 kind 列表。
    """
    from . import scheduler as _sch          # 延迟导入，避免循环
    try:
        jobs = _sch.load_jobs_raw()
    except Exception:                                    # noqa: BLE001
        return []
    legacy = [j for j in (jobs or []) if str(j.get("kind") or "") in KINDS]
    if not legacy:
        return []

    with _LOCK:
        st = _ensure()
        moved = []
        for j in legacy:
            kind = str(j["kind"])
            it = st["items"].get(kind) or _blank_item(_BY_KIND[kind])
            sch = j.get("schedule") or {}
            # 只在**用户改过**（和代码默认不同）时才覆盖，否则保留代码默认
            if sch.get("at_time"):
                it["at_time"] = sch["at_time"]
                it["every_minutes"] = 0
            elif sch.get("every_minutes"):
                it["every_minutes"] = int(sch["every_minutes"])
                it["at_time"] = ""
            if j.get("enabled") is False:
                it["enabled"] = False
            if j.get("last_run"):
                it["last_run"] = str(j["last_run"])
            if j.get("last_status"):
                it["last_status"] = str(j["last_status"])
            if j.get("last_preview"):
                it["last_preview"] = str(j["last_preview"])
            it["next_run"] = float(j.get("next_run") or 0) or next_run(it)
            # 记下旧任务 id：旧日志文件就叫 `<旧id>_<日期>.md`，
            # 有了它「历史能翻到」才成立（见 _legacy_ids）。
            _old = str(j.get("id") or "")
            if _old:
                ids = list(it.get("legacy_ids") or [])
                if _old not in ids:
                    ids.append(_old)
                it["legacy_ids"] = ids
            st["items"][kind] = it
            moved.append(kind)
        _save(st)

    # 从 schedules.json 移除（保留用户自建任务）
    try:
        _sch.save_jobs([j for j in (jobs or [])
                        if str(j.get("kind") or "") not in KINDS])
    except Exception:                                    # noqa: BLE001
        logger.debug("selfmaint 迁移清理 schedules 失败", exc_info=True)
    if moved:
        logger.warning("自我维护已从定时任务迁出：%s", ",".join(moved))
    return moved


# ---------------------------------------------------------------- 对外：状态
def status() -> dict:
    """给「迭代想法」抽屉的「自我维护」区块用。"""
    st = _ensure()
    items = []
    for d in _DEFS:
        it = st["items"][d["kind"]]
        items.append({
            "kind": d["kind"], "name": d["name"], "icon": d["icon"],
            "desc": d["desc"],
            "enabled": bool(it.get("enabled", True)),
            "at_time": it.get("at_time") or "",
            "every_minutes": it.get("every_minutes") or 0,
            "rhythm": describe(it),
            "last_run": it.get("last_run") or "",
            "last_status": it.get("last_status") or "",
            "last_preview": it.get("last_preview") or "",
            "last_seconds": it.get("last_seconds") or 0,
            "next_run": it.get("next_run") or 0,
            "next_text": (time.strftime("%m-%d %H:%M",
                                        time.localtime(it["next_run"]))
                          if it.get("next_run") else ""),
            "run_count": len(it.get("runs") or []),
        })
    return {"ok": True, "items": items}


def _legacy_ids(kind: str) -> list:
    """找出这一项**当年作为定时任务**跑出来的日志 id 前缀。

    为什么要它：用户要求「以前定时跑出来的历史结果保留，在迭代想法里能翻到」。
    旧日志的文件名是 `<旧job id>_<日期>_<时间>.md`，标题行是 `# 旧任务名`；
    新代码只按 `maint_<kind>_` 找，会一条都看不到。
    这里扫一次目录、按标题行认领，结果缓存进 legacy_ids，之后不再扫。
    """
    st = _ensure()
    it = st["items"].get(kind) or {}
    ids = it.get("legacy_ids") or []
    if ids:
        return ids
    want = "# %s" % (_BY_KIND[kind].get("legacy_name") or "")
    if want.strip() == "#":
        return []
    found = []
    try:
        from . import scheduler as _sch
        d = _sch._logs_dir()
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            head = ""
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    head = f.read(120)
            except Exception:                            # noqa: BLE001
                continue
            if head.startswith(want):
                pid = fn.split("_")[0]
                # 只认 8 位十六进制（任务 id 的形状）。
                # 不加这条会把历轮测试留下的 `selftest_xxx.md` / `test_xxx.md`
                # 也当成「历史运行」列出来（实测踩到）。
                if (len(pid) == 8
                        and all(c in "0123456789abcdef" for c in pid)
                        and pid not in found):
                    found.append(pid)
    except Exception:                                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    if found:
        with _LOCK:
            st = _ensure()
            st["items"][kind]["legacy_ids"] = found
            _save(st)
        logger.warning("自我维护 %s 认领到旧日志 id：%s", kind, ",".join(found))
    return found


def history(kind: str, limit: int = 10) -> dict:
    """某一项的最近运行历史（本模块记的摘要 + 落盘 md 的文件名）。"""
    kind = str(kind or "")
    if kind not in _BY_KIND:
        return {"ok": False, "error": "未知的自我维护项：%s" % kind}
    st = _ensure()
    it = st["items"][kind]
    runs = list(it.get("runs") or [])[-max(1, int(limit or 10)):][::-1]
    files = []
    try:
        from . import scheduler as _sch
        d = _sch._logs_dir()
        # 新日志按 maint_<kind>_ 命名；旧日志按当年那个定时任务的 id 命名。
        # 两种都要认，否则用户「以前的历史」在界面上就消失了。
        prefixes = ["maint_%s" % kind] + _legacy_ids(kind)
        for fn in sorted(os.listdir(d), reverse=True):
            if not fn.endswith(".md"):
                continue
            if any(fn.startswith(p) for p in prefixes):
                files.append({"name": fn, "path": os.path.join(d, fn)})
            if len(files) >= limit:
                break
    except Exception:                                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"ok": True, "kind": kind, "name": _BY_KIND[kind]["name"],
            "runs": runs, "files": files}


def set_item(kind: str, enabled=None, at_time=None, every_minutes=None) -> dict:
    """暂停 / 重启 / 改节律。只改传进来的字段。"""
    kind = str(kind or "")
    if kind not in _BY_KIND:
        return {"ok": False, "error": "未知的自我维护项：%s" % kind}
    with _LOCK:
        st = _ensure()
        it = st["items"][kind]
        if enabled is not None:
            it["enabled"] = bool(enabled)
        if at_time is not None:
            it["at_time"] = str(at_time)
            it["every_minutes"] = 0
        if every_minutes is not None:
            try:
                it["every_minutes"] = max(0, int(every_minutes))
            except Exception:                            # noqa: BLE001
                return {"ok": False, "error": "every_minutes 必须是整数"}
            if it["every_minutes"]:
                it["at_time"] = ""
        it["next_run"] = next_run(it)
        st["items"][kind] = it
        _save(st)
        return {"ok": True, "item": it, "rhythm": describe(it)}


# ---------------------------------------------------------------- 调度
def due_items(now: float = None) -> list:
    """到点该跑的 kind 列表（自动触发用）。

    三条护栏：
      · 逐项 enabled=False 的不跑（用户点了暂停）；
      · 总开关 self_iterate.settings.paused 时不跑；
      · next_run 为 0（没设节律）的不跑。
    """
    now = time.time() if now is None else now
    try:
        from . import self_iterate as _si
        if _si.load_settings().get("paused"):
            return []
    except Exception:                                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    st = _ensure()
    out = []
    for d in _DEFS:
        it = st["items"][d["kind"]]
        if not it.get("enabled", True):
            continue
        nr = float(it.get("next_run") or 0)
        if nr and nr <= now:
            out.append(d["kind"])
    return out


def catchup() -> list:
    """应用关着时错过了执行时间 → 启动后补跑一次（只对 enabled 的项）。

    过期不到 5 分钟的不算「错过」，交给正常 tick，不插队。
    """
    st = _ensure()
    now = time.time()
    due = []
    changed = False
    for d in _DEFS:
        it = st["items"][d["kind"]]
        if not it.get("enabled", True):
            continue
        nr = float(it.get("next_run") or 0)
        if nr and nr < now - 300:
            it["next_run"] = now
            changed = True
            due.append(d["kind"])
    if changed:
        _save(st)
    return due


# ---------------------------------------------------------------- 执行
def _record(kind: str, ok: bool, preview: str, seconds: float,
            status_text: str = "") -> None:
    with _LOCK:
        st = _ensure()
        it = st["items"][kind]
        it["last_run"] = _now()
        it["last_status"] = status_text or ("ok" if ok else "error")
        it["last_preview"] = str(preview or "")[:300]
        it["last_seconds"] = round(float(seconds or 0), 1)
        it["next_run"] = next_run(it)
        runs = list(it.get("runs") or [])
        runs.append({"ts": it["last_run"], "ok": bool(ok),
                     "status": it["last_status"],
                     "preview": str(preview or "")[:300],
                     "seconds": it["last_seconds"]})
        it["runs"] = runs[-_HIST_MAX:]
        st["items"][kind] = it
        _save(st)


def record_boot(rec: dict) -> None:
    """把「重启后自检」（bootaudit）的结果记到 selfidle 这一项上。

    为什么要有这个函数
    ----------------
    selfidle 原来是「每 20 分钟定时跑一遍 run_audit」。用户 2026-11-16 说
    「太频繁了，改成每次迭代重启后再自检」。执行体换成了 bootaudit.py
    （每次启动 6 秒后自动跑，只查不改），但**界面上的「上次运行 / 历史」
    还是从 selfmaint 的状态里读**，所以这里做一次桥接：
    bootaudit 跑完 → 调本函数 → selfidle 项就有记录了。

    只写状态，不触发任何执行 —— 跑是调用方（bootaudit）的事。
    """
    try:
        v = str(rec.get("verdict") or "")
        # fail 才算失败；warn（只有 mid）和 pass 都记成 ok
        ok = bool(rec.get("ok", True)) and v != "fail"
        text = str(rec.get("text") or "")
        if not text:
            text = "重启自检：%s" % (rec.get("error") or v or "完成")
        _record("selfidle", ok, text[:300], float(rec.get("seconds") or 0),
                status_text=(v or ("ok" if ok else "error")))
    except Exception:                                    # noqa: BLE001
        # 记录失败绝不能反过来影响自检本身
        logger.debug("record_boot 写入失败（不影响自检）", exc_info=True)


def run_item(kind: str, trigger: str = "manual", force: bool = False) -> dict:
    """真跑一次某一项。

    trigger="manual"（用户点「立即运行」）→ 跳过所有闸，必定真跑；
    trigger="auto"（调度循环 / 补跑）→ 过逐项 enabled 和总开关。

    执行体复用 scheduler 的 _run_*（会写会话 + 写 schedule_logs 的 md），
    所以「历史结果能翻到」这条不用另做一套。
    """
    kind = str(kind or "")
    if kind not in _BY_KIND:
        return {"ok": False, "error": "未知的自我维护项：%s" % kind}
    d = _BY_KIND[kind]

    if trigger != "manual" and not force:
        st = _ensure()
        if not st["items"][kind].get("enabled", True):
            return {"ok": True, "skipped": True, "reason": "这一项已暂停"}
        try:
            from . import self_iterate as _si
            if _si.load_settings().get("paused"):
                return {"ok": True, "skipped": True, "reason": "自动化已暂停"}
        except Exception:                                # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    # selfidle（重启自检）的执行体在 bootaudit.py，不走 scheduler。
    # 这样「自动（重启后）」和「手动点立即运行」走的是同一段代码，
    # 行为完全一致：只查不改、不建计划，避免出现
    # 「手动点一下就被改了代码」这种意料之外的事。
    if kind == "selfidle":
        from . import bootaudit as _ba
        t0 = time.time()
        try:
            r = _ba.run(reason="手动点「立即运行」", force=True)
        except Exception as e:                           # noqa: BLE001
            sec = time.time() - t0
            msg = "%s: %s" % (type(e).__name__, e)
            _record(kind, False, msg, sec, status_text="error: %s" % e)
            return {"ok": False, "error": msg, "kind": kind,
                    "name": d["name"], "seconds": round(sec, 1)}
        sec = time.time() - t0
        if r.get("skipped"):
            return {"ok": True, "skipped": True, "kind": kind, "name": d["name"],
                    "reason": str(r.get("reason") or ""), "seconds": round(sec, 1)}
        if not r.get("ok"):
            return {"ok": False, "error": str(r.get("error") or "自检失败"),
                    "kind": kind, "name": d["name"], "seconds": round(sec, 1)}
        # 注意：结果已由 bootaudit.run() 内部通过 record_boot 写进本项，
        # 这里不能再 _record 一次（会重复记两条历史）。
        last = r.get("last") or {}
        text = str(last.get("text") or "")
        return {"ok": True, "kind": kind, "name": d["name"],
                "answer": text, "seconds": round(sec, 1), "preview": text[:300]}

    from . import scheduler as _sch          # 延迟导入，避免循环
    job = {"id": "maint_%s" % kind, "name": "自我维护·%s" % d["name"],
           "kind": kind, "prompt": "", "session": "", "focus": "",
           # force=True 让 selfexp 内部那些「到点才跑」的闸放行
           "force": bool(force or trigger == "manual"),
           "schedule": {}}

    t0 = time.time()
    try:
        r = _sch._run_once(job) or {}
    except Exception as e:                               # noqa: BLE001
        sec = time.time() - t0
        msg = "%s: %s" % (type(e).__name__, e)
        _record(kind, False, msg, sec, status_text="error: %s" % e)
        return {"ok": False, "error": msg, "kind": kind,
                "name": d["name"], "seconds": round(sec, 1)}

    sec = time.time() - t0
    if r.get("skipped"):
        # 跳过不算失败，也不更新 last_run（免得「上次运行」显示成没跑的那次）
        return {"ok": True, "skipped": True, "kind": kind, "name": d["name"],
                "reason": r.get("reason") or "", "seconds": round(sec, 1)}
    answer = str(r.get("answer") or "")
    _record(kind, True, answer.replace("\n", " ")[:300], sec)
    return {"ok": True, "kind": kind, "name": d["name"],
            "answer": answer, "seconds": round(sec, 1),
            "preview": answer.replace("\n", " ")[:300]}


def run_all_due(now: float = None) -> list:
    """调度循环每 tick 调一次：把到点的项逐个跑掉，返回本次跑了哪些。"""
    done = []
    for kind in due_items(now):
        try:
            r = run_item(kind, trigger="auto")
            done.append({"kind": kind, "ok": r.get("ok"),
                         "skipped": bool(r.get("skipped")),
                         "reason": r.get("reason") or r.get("error") or ""})
        except Exception as e:                           # noqa: BLE001
            logger.warning("自我维护 %s 执行异常：%s", kind, e)
            done.append({"kind": kind, "ok": False, "reason": str(e)})
    return done
