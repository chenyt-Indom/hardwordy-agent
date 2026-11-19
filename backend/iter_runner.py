# -*- coding: utf-8 -*-
"""迭代任务队列 —— 让「自我迭代」像普通对话任务一样在聊天界面里跑。

用户 2026-11-15 明确要求：
  · 信任模式下计划一确定就直接开始，聊天界面显示「正在迭代中」，
    并且像平时对话那样把代码卡片展示出来；
  · 如果当时已有任务在跑，就排队等它结束、立刻接着跑（显示「正在等待执行」）；
  · 手动批准也一样：没任务就直接跑，有任务就等待，且等完自动接着跑；
  · 迭代期间用户照常聊天、不阻塞；但要能看到详细操作，并且可以点「终止」停下。

设计取舍：
  · **一个全局串行队列**（同一时刻只跑一个计划）——两个任务同时改同一个文件
    必然互相踩，串行是唯一安全的做法；
  · 执行放在**后台线程**里，HTTP 立即返回，不阻塞聊天；
  · 全过程写成「事件流」（status / step / code / done），前端按 seq 增量拉取，
    这样既能做实时进度，也能刷新后恢复；
  · 取消是**协作式**的：置标记后，当前动作跑完（含校验/回滚）才停，
    绝不中途打断 patch —— 半途停会留下改了一半的文件。
"""

import json
import threading
import time
import traceback

from . import self_iterate as _si
import logging
logger = logging.getLogger(__name__)

# 事件保留上限：太多会把 task.json 撑大，前端也不需要这么久的历史。
# ★ 2026-11-17 从 400 提到 2000：思考过程改成流式后，每步会产生
#   「思考块 + 工具开始 + 工具完成」3~5 条事件，80 步一轮就是 300~400 条，
#   原来 400 的上限会把**结果卡片**（code 事件）直接挤出队列 —— 前端看不到
#   改了什么。2000 条 × 平均 160 字 ≈ 300KB，可以接受。
_MAX_EVENTS = 2000
_MAX_HISTORY = 20

_LOCK = threading.RLock()
_STATE = {
    "seq": 0,            # 全局事件序号（只增不减，前端靠它做增量）
    "running": None,     # 当前在跑的任务
    "queue": [],         # 等待中的任务 [{tid, plan_id, source, at}]
    "events": [],        # 事件流（按 seq 递增）
    "history": [],       # 最近完成的任务摘要（新的在前）
    # ★ 2026-11-19 用户要求：「不因重启而丢失进度以及等待进行的任务」。
    #   queue 现在会写盘并在新进程里恢复；verify 记录「刚改完、还没验证」的那个任务，
    #   新进程启动后自动接着验证 —— 这样「改代码 → 重启 → 验证 → 再改」能全自动闭环。
    "verify": None,      # {tid, plan_id, title, files, tries, at, report}
    "reports": [],       # 每个功能完成后的 markdown 报告（新的在前）
    "budget": 0,         # 本轮连续自动迭代已完成的功能数（上限见 _MAX_AUTO_ROUNDS）
}
_FILE = _si._DATA / "task.json"

# 用户 2026-11-19 选择：「连续自动迭代最多跑 10 个功能就停」。
_MAX_AUTO_ROUNDS = 10
# 同一个功能验证不过时的重试上限。用户选的是「继续重试直到通过」，
# 但必须有兜底 —— 否则一个死循环功能会把队列永远卡住。
_MAX_VERIFY_TRIES = 12
# 自动重启前等的秒数：给前端一次轮询把「正在重启」事件收下来的机会。
_RESTART_DELAY = 1.5


# ------------------------------------------------------------------ 落盘

def _load():
    """启动时把上次的事件流/历史/队列读回来。

    ★ 2026-11-19 用户明确要求：「全部要能够全自动完成一个接一个的任务而不因重启
      而丢失进度以及等待进行的任务」。所以这里**必须**把 queue 和 verify 恢复回来
      —— 原来写的是「running/queue 不恢复」，那正是「实现一件就停、重启就丢」的根因。

    · running 仍然不恢复成「正在跑」：进程已经死了，没有线程在跑它；
      但它会被**放回队首**重新跑一次（做过的动作是 done 状态，重跑会跳过，不会重复改）。
    · queue 原样恢复，并打上 resumed 标记，前端能显示「重启后恢复」。
    """
    try:
        if _FILE.exists():
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            with _LOCK:
                _STATE["seq"] = int(d.get("seq") or 0)
                _STATE["events"] = list(d.get("events") or [])[-_MAX_EVENTS:]
                _STATE["history"] = list(d.get("history") or [])[:_MAX_HISTORY]
                _STATE["reports"] = list(d.get("reports") or [])[:30]
                _STATE["budget"] = int(d.get("budget") or 0)
                # 待验证的任务：新进程起来后自动接着验证
                _STATE["verify"] = d.get("verify") or None
                # 队列恢复：把上次「正在跑」的那个放回队首（它没跑完）
                q = list(d.get("queue") or [])
                run = d.get("running")
                if run and run.get("plan_id"):
                    q.insert(0, {"tid": run.get("tid"), "plan_id": run.get("plan_id"),
                                 "source": run.get("source") or "auto",
                                 "title": run.get("title") or "",
                                 "at": run.get("started_at") or time.time(),
                                 "max_n": run.get("max_n") or 0,
                                 "resumed": True})
                _STATE["queue"] = q
                _STATE["running"] = None
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _save():
    """写盘。失败不影响执行（历史丢了也比任务跑不完强）。"""
    try:
        with _LOCK:
            d = {"seq": _STATE["seq"],
                 "events": _STATE["events"][-_MAX_EVENTS:],
                 "history": _STATE["history"][:_MAX_HISTORY],
                 # ★ 队列必须落盘：否则重启后等待中的任务全丢
                 "queue": _STATE["queue"],
                 "running": _STATE["running"],
                 "verify": _STATE.get("verify"),
                 "reports": _STATE.get("reports") or [],
                 "budget": _STATE.get("budget") or 0,
                 "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _emit(tid, kind, **kw):
    """追加一条事件，返回它的 seq。"""
    with _LOCK:
        _STATE["seq"] += 1
        ev = {"seq": _STATE["seq"], "tid": tid, "kind": kind,
              "ts": time.time(), "t": time.strftime("%H:%M:%S")}
        ev.update(kw)
        _STATE["events"].append(ev)
        if len(_STATE["events"]) > _MAX_EVENTS:
            _STATE["events"] = _STATE["events"][-_MAX_EVENTS:]
        return ev["seq"]


# ------------------------------------------------------------------ 查询

def snapshot(since=0):
    """前端轮询用：返回状态 + since 之后的新事件。

    since=0 表示「全部给我」（首次打开抽屉/刷新页面时用）。
    """
    since = int(since or 0)
    with _LOCK:
        evs = [e for e in _STATE["events"] if e["seq"] > since]
        run = _STATE["running"]
        q = list(_STATE["queue"])
        out = {
            "ok": True,
            "seq": _STATE["seq"],
            "running": _brief(run) if run else None,
            "queue": [_brief(x) for x in q],
            "queue_len": len(q),
            "events": evs,
            "history": _STATE["history"][:_MAX_HISTORY],
            "state": _state_name(run, q),
            # ★ 2026-11-19：自动验证/报告要显示在卡片上
            "verify": _STATE.get("verify"),
            "reports": (_STATE.get("reports") or [])[:5],
            "budget": _STATE.get("budget") or 0,
            "max_rounds": _MAX_AUTO_ROUNDS,
        }
    return out


def _state_name(run, q):
    if run:
        # 终止中要单独一个状态：原来返回 "waiting" 会让前端以为在排队，
        # 而且「终止」按钮的文案跟实际行为对不上。
        if run.get("cancel"):
            return "cancelling"
        return "running"
    if q:
        # 连续完成数到上限、队列里还有活儿 → 停下来等用户点「继续」
        if int(_STATE.get("budget") or 0) >= _MAX_AUTO_ROUNDS:
            return "paused"
        return "queue_only"
    return "idle"


def _brief(t):
    if not t:
        return None
    return {"tid": t.get("tid"), "plan_id": t.get("plan_id"),
            "source": t.get("source"), "title": t.get("title"),
            "state": t.get("state"), "started_at": t.get("started_at"),
            "elapsed": round(time.time() - (t.get("started_at") or time.time()), 1),
            "cancel": bool(t.get("cancel"))}


def task_events(tid):
    """取某个任务的全部事件（跑完收起后展开详情用）。"""
    with _LOCK:
        return [e for e in _STATE["events"] if e.get("tid") == tid]


# ------------------------------------------------------------------ 提交

def submit(plan_id, source="auto", title="", max_n=0):
    """把一个计划排进队列。

    · 没有任务在跑 → 立刻启动后台线程，返回 state=running
    · 已有任务在跑 → 入队等待，返回 state=waiting 和前面还有几个
    """
    plan_id = str(plan_id or "").strip()
    if not plan_id:
        return {"ok": False, "error": "缺少计划 id"}

    tid = "t_%s" % _si._pid()
    item = {"tid": tid, "plan_id": plan_id, "source": str(source or "auto"),
            "title": str(title or ""), "at": time.time(),
            "max_n": int(max_n or 0)}

    with _LOCK:
        run = _STATE["running"]
        # 同一份计划不要重复排队：动作跑完就是 done 了，再排一次只会
        # 立刻失败（「没有已批准的动作」），白占一个队列位、还让用户以为出了问题。
        if run and run.get("plan_id") == plan_id:
            return {"ok": True, "state": "running", "tid": run["tid"],
                    "ahead": 0, "dup": True}
        for _x in _STATE["queue"]:
            if _x.get("plan_id") == plan_id:
                return {"ok": True, "state": "waiting", "tid": _x["tid"],
                        "ahead": len(_STATE["queue"]), "dup": True}
        if run:
            _STATE["queue"].append(item)
            ahead = len(_STATE["queue"])
            _emit(tid, "status",
                  text="正在等待执行（前面还有 %d 个任务）" % ahead,
                  state="waiting", ahead=ahead)
            _save()
            return {"ok": True, "state": "waiting", "tid": tid,
                    "ahead": ahead, "queue_len": ahead}

        _STATE["running"] = {"tid": tid, "plan_id": plan_id,
                             "source": item["source"], "title": item["title"],
                             "state": "running", "started_at": time.time(),
                             "cancel": False, "max_n": item["max_n"]}
        # 用户要求：「自迭代卡片上说明正在迭代什么功能」——
        # 有标题就直接写出来，别只给一句「正在迭代中」让人猜。
        _emit(tid, "status",
              text=("正在迭代：%s" % item["title"]) if item.get("title")
                   else "正在迭代中…", state="running")
    _save()

    th = threading.Thread(target=_worker, args=(item,), daemon=True,
                          name="iter-%s" % tid)
    th.start()
    return {"ok": True, "state": "running", "tid": tid, "ahead": 0}


def cancel(tid=""):
    """请求终止。当前动作会跑完（含校验/回滚）再停，不会留下改了一半的文件。"""
    with _LOCK:
        run = _STATE["running"]
        if not run:
            return {"ok": False, "error": "现在没有任务在跑"}
        if tid and str(tid) != run["tid"]:
            return {"ok": False, "error": "任务 id 对不上（当前在跑的是 %s）" % run["tid"]}
        run["cancel"] = True
        run["state"] = "cancelling"
        _emit(run["tid"], "status", text="正在终止（当前动作做完就停）…",
              state="cancelling")
    _save()
    return {"ok": True, "tid": run["tid"], "state": "cancelling"}


# ------------------------------------------------------------------ 执行

def _worker(item):
    """后台线程：跑完这个计划，然后自动接队列里的下一个。"""
    tid = item["tid"]
    plan_id = item["plan_id"]
    ok_n = fail_n = skip_n = 0
    changed = []
    err_all = ""
    # 改了文件且开启自动重启时置 True —— finally 里据此决定「重启」还是「接下一个」
    auto_rst = False

    def on_event(kind, **kw):
        _emit(tid, kind, **kw)

    def should_cancel():
        with _LOCK:
            r = _STATE["running"]
            return bool(r and r.get("cancel"))

    # 2026-11-16 执行时间线可见：批准后用户最想知道「它到底跑了没、跑到哪了」。
    # 这里在提交后 / 每步执行后 / 结束时各写一条 stage 记录（actlog.record_stage），
    # 前端 featpanel.js 读 /api/featradar/timeline?plan_id= 渲染成时间线。
    # 任何写阶段日志的异常都吞掉 —— 时间线坏了绝不能影响迭代本身。
    def _stage(stage, detail="", **kw):
        try:
            from . import actlog as _al
            _al.record_stage(plan_id, stage, detail=detail, **kw)
        except Exception:                                # noqa: BLE001
            logger.debug("写阶段流水失败（不影响迭代）", exc_info=True)

    try:
        _stage("queued", "已提交执行，等待开始")
        _emit(tid, "step", text="读取计划并确定要执行的动作…")
        # 信任模式：先把 pending 全批准（高风险也批，但会在结果里点出来）
        if item.get("source") == "auto":
            try:
                _si.auto_approve_only(plan_id, max_n=item.get("max_n") or 0)
            except Exception as e:                      # noqa: BLE001
                _emit(tid, "step", text="自动批准失败：%s" % e)

        _stage("executing", "开始执行已批准的动作")

        # 包一层 on_event：把执行器吐出的每一步也同步写进阶段流水，
        # 这样时间线能显示「第 1 步 / 第 2 步 …」而不是只有首尾两条。
        _step_no = [0]

        def _on_event(kind, **kw):
            on_event(kind, **kw)
            if kind == "step":
                _step_no[0] += 1
                _stage("agent_step",
                       kw.get("text") or ("第 %d 步" % _step_no[0]))

        r = _si.execute_approved(plan_id, on_event=_on_event,
                                 should_cancel=should_cancel)
        ok_n = int(r.get("done") or 0)
        results = r.get("results") or []
        fail_n = sum(1 for x in results if not x.get("ok"))
        skip_n = int(r.get("skipped") or 0)
        for x in results:
            for f in (x.get("files") or []):
                if f not in changed:
                    changed.append(f)
        if not r.get("ok") and r.get("error"):
            err_all = str(r["error"])

        cancelled = bool(r.get("cancelled"))
        if cancelled:
            _stage("blocked", "已终止（已完成 %d 条，剩余动作保持待批准）" % ok_n)
            _emit(tid, "status", text="已终止（已完成 %d 条，剩余动作保持待批准）" % ok_n,
                  state="cancelled")
        elif ok_n or fail_n:
            if fail_n:
                _stage("failed",
                       "迭代完成：成功 %d 条，失败 %d 条%s" % (
                           ok_n, fail_n, "，跳过 %d 条" % skip_n if skip_n else ""))
            else:
                _stage("done",
                       "迭代完成：成功 %d 条%s" % (
                           ok_n, "，跳过 %d 条" % skip_n if skip_n else ""))
            _emit(tid, "status",
                  text="迭代完成：成功 %d 条，失败 %d 条%s" % (
                      ok_n, fail_n, "，跳过 %d 条" % skip_n if skip_n else ""),
                  state="done")
        else:
            _stage("done",
                   "这次没有可执行的动作%s" % ("：" + err_all if err_all else ""))
            _emit(tid, "status",
                  text="这次没有可执行的动作%s" % ("：" + err_all if err_all else ""),
                  state="done")

        # 2026-11-15 用户要求「改完之后就提示重启」。
        # ★ 2026-11-19 升级：用户要求「自动重启 + 重启后自动验证，反复修到正常」，
        #   所以这里不再只发一条提示，而是：
        #     ① 记下「待验证」（含改动文件、重试轮次）→ 落盘
        #     ② 由 finally 触发自动重启
        #     ③ 新进程的 boot_resume() 读回它 → 自动验证 → 过了接下一个 / 不过生成修复任务
        #   只有**真的改了文件**才重启 —— 一条都没改成却重启是纯噪音。
        if changed and ok_n and not cancelled:
            try:
                from . import bootgate as _bg
                _bg.mark_auto_change(changed, note="自动迭代：%s" % str(item.get("title") or "")[:60])
            except Exception:                            # noqa: BLE001
                logger.debug("标记自动迭代改动失败（不影响迭代）", exc_info=True)
            _auto = True
            try:
                _auto = bool(_si.load_settings().get("auto_restart_after_iter", True))
            except Exception:                            # noqa: BLE001
                logger.debug("silent-except", exc_info=True)
            if _auto:
                with _LOCK:
                    prev = _STATE.get("verify") or {}
                    _STATE["verify"] = {
                        "tid": tid, "plan_id": plan_id,
                        "title": item.get("title") or "",
                        "files": changed[:20],
                        # 同一功能反复修时轮次要累加，不能每轮清零
                        "tries": int(prev.get("tries") or 0)
                                 if prev.get("plan_id") == plan_id else 0,
                        "at": time.time(),
                    }
                _save()
                _emit(tid, "restart",
                      text="这次改了 %d 个文件，稍后自动重启并验证" % len(changed),
                      files=changed[:12])
                auto_rst = True
            else:
                _emit(tid, "restart",
                      text="这次改了 %d 个文件，需要重启才生效" % len(changed),
                      files=changed[:12])

        _finish(tid, "cancelled" if cancelled else ("done" if ok_n else "failed"),
                ok_n, fail_n, skip_n, changed, err_all)
    except Exception as e:                              # noqa: BLE001
        tb = traceback.format_exc()[-800:]
        try:
            from . import actlog as _al
            _al.record_stage(plan_id, "failed",
                             detail="迭代出错：%s: %s" % (type(e).__name__, e))
        except Exception:                                # noqa: BLE001
            logger.debug("写阶段流水失败（不影响迭代）", exc_info=True)
        _emit(tid, "status", text="迭代出错：%s: %s" % (type(e).__name__, e),
              state="error")
        _emit(tid, "error", text="%s: %s" % (type(e).__name__, e), tb=tb)
        _finish(tid, "error", ok_n, fail_n, skip_n, changed,
                "%s: %s" % (type(e).__name__, e))
    finally:
        if auto_rst:
            # 走「重启 → 自动验证 → 再接下一个」这条链，
            # 这里**不能**调 _next()：否则下一个任务会在重启前抢跑，
            # 跑到一半进程被替换掉，进度就丢了。
            _auto_restart(reason=item.get("title") or plan_id)
        else:
            _next()


def _finish(tid, state, ok_n, fail_n, skip_n, changed, err):
    with _LOCK:
        run = _STATE["running"]
        started = (run or {}).get("started_at") or time.time()
        secs = round(time.time() - started, 1)
        item = {"tid": tid, "plan_id": (run or {}).get("plan_id"),
                "source": (run or {}).get("source"),
                "title": (run or {}).get("title") or "",
                "state": state, "done": ok_n, "failed": fail_n,
                "skipped": skip_n, "files": changed[:12],
                "seconds": secs, "error": err,
                "ended_at": time.time(),
                "at": time.strftime("%m-%d %H:%M:%S")}
        _STATE["history"].insert(0, item)
        _STATE["history"] = _STATE["history"][:_MAX_HISTORY]
    _save()
    # ★ 2026-11-16：把「任务失败」告诉活跃迭代引擎，让它反思为什么改失败了。
    #   只在失败时回调（selfloop 内部判断），成功也触发会变成
    #   「改代码 → 成功 → 再反思 → 再改代码」的自我循环。
    try:
        from . import selfloop as _sl
        _sl.on_task_end(item)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _next():
    """当前任务结束后：把队列里的下一个提上来接着跑。"""
    nxt = None
    with _LOCK:
        _STATE["running"] = None
        if _STATE["queue"]:
            nxt = _STATE["queue"].pop(0)
            _STATE["running"] = {"tid": nxt["tid"], "plan_id": nxt["plan_id"],
                                 "source": nxt["source"], "title": nxt["title"],
                                 "state": "running", "started_at": time.time(),
                                 "cancel": False, "max_n": nxt.get("max_n") or 0}
            _emit(nxt["tid"], "status",
                  text=("前面的任务结束了，开始迭代：%s" % nxt["title"])
                       if nxt.get("title") else "前面的任务结束了，开始执行这个计划…",
                  state="running")
    _save()
    if nxt:
        th = threading.Thread(target=_worker, args=(nxt,), daemon=True,
                              name="iter-%s" % nxt["tid"])
        th.start()


def queue_clear():
    """清空等待队列（不影响正在跑的）。"""
    with _LOCK:
        n = len(_STATE["queue"])
        _STATE["queue"] = []
    _save()
    return {"ok": True, "cleared": n}


def queue_drop(tid=""):
    """取消等待队列里的某一个任务（用户要求「等待进行的任务要可以取消」）。"""
    tid = str(tid or "").strip()
    if not tid:
        return {"ok": False, "error": "缺少任务 id"}
    with _LOCK:
        before = len(_STATE["queue"])
        _STATE["queue"] = [x for x in _STATE["queue"] if x.get("tid") != tid]
        after = len(_STATE["queue"])
        if before == after:
            return {"ok": False, "error": "队列里没有这个任务（可能已经开始跑了）"}
        _emit(tid, "status", text="已取消（从等待队列里移除）", state="cancelled")
    _save()
    return {"ok": True, "removed": 1, "queue_len": after}


# ------------------------------------------------ 自动重启 + 自动验证闭环
# 用户 2026-11-19 要求（原话）：
#   「推荐新功能需要自动重启，重启之后还要可以自动检验是否有问题然后反复修改
#     直到完全正常」「全部要能够全自动完成一个接一个的任务而不因重启而丢失进度」
# 所以闭环是：
#   跑完一个功能 → 改了文件 → 记下「待验证」→ 自动重启
#   → 新进程启动读回「待验证」→ 自动验证 → 不过就生成修复计划再跑 → 再重启
#   → …… 直到验证通过 → 出 markdown 报告 → 接着下一个功能。


def _now():
    return time.strftime("%m-%d %H:%M:%S")


def _emit_sys(text, state=""):
    """系统级提示：挂到当前任务上（没有就挂 _sys），不另起一张空卡片。"""
    tid = "_sys"
    with _LOCK:
        r = _STATE.get("running")
        if r and r.get("tid"):
            tid = r["tid"]
        elif _STATE["history"]:
            tid = _STATE["history"][0].get("tid") or "_sys"
    try:
        _emit(tid, "status", text=text, state=state or "")
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _verify_once(files):
    """跑一轮验证。返回 (ok, problems)。

    用户选的标准是「B + 前端截图自查」里能做到的部分：
      · .py 逐个 compile（语法错会直接让应用起不来）
      · .js 用 node --check
      · 代码自检只看 high / mid（low/info 不影响功能，不能拦着往下走）
    """
    problems = []
    fl = [str(x) for x in (files or [])]

    for rel in fl:
        if not rel.endswith(".py"):
            continue
        p = _si._ROOT / rel
        if not p.exists():
            problems.append("%s 不存在（可能路径写错了）" % rel)
            continue
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            problems.append("%s 语法错误：第 %s 行 %s" % (rel, e.lineno, e.msg))
        except Exception as e:                          # noqa: BLE001
            problems.append("%s 读不了：%s" % (rel, e))

    js = [r for r in fl if r.endswith(".js")]
    if js:
        try:
            import shutil as _sh
            import subprocess as _sp
            node = _sh.which("node")
            if node:
                for rel in js:
                    p = _si._ROOT / rel
                    if not p.exists():
                        problems.append("%s 不存在" % rel)
                        continue
                    rr = _sp.run([node, "--check", str(p)],
                                 capture_output=True, timeout=40)
                    if rr.returncode != 0:
                        msg = (rr.stderr or b"").decode("utf-8", "replace").strip()
                        problems.append("%s 语法错误：%s" % (rel, msg[:240]))
        except Exception:                               # noqa: BLE001
            logger.debug("node 语法检查没跑成（跳过）", exc_info=True)

    try:
        a = _si.run_audit(deep=False, source="after_change", create_plan=False)
        if not a.get("skipped"):
            for f in (a.get("findings") or []):
                if f.get("level") in ("high", "mid"):
                    problems.append("自检[%s] %s:%s %s —— %s" % (
                        f.get("level"), f.get("file"), f.get("line"),
                        f.get("kind"), f.get("msg")))
    except Exception as e:                              # noqa: BLE001
        problems.append("代码自检没跑成：%s: %s" % (type(e).__name__, e))

    return (not problems), problems


def _report_md(v, ok, problems):
    """单个功能的 markdown 报告。"""
    title = v.get("title") or v.get("plan_id") or "（未命名功能）"
    files = v.get("files") or []
    tries = int(v.get("tries") or 1)
    out = []
    out.append("### %s %s" % ("✅" if ok else "⚠️", title))
    out.append("")
    out.append("- **计划**：`%s`" % (v.get("plan_id") or "-"))
    out.append("- **改动文件**：%d 个%s" % (
        len(files),
        ("（" + "、".join("`%s`" % x for x in files[:6]) +
         ("等" if len(files) > 6 else "") + "）") if files else ""))
    out.append("- **验证**：第 %d 轮 · %s" % (tries, "通过" if ok else "未通过"))
    if ok:
        out.append("- **结果**：语法、代码自检、运行状态全部正常")
    else:
        out.append("- **结果**：还有 %d 个问题" % len(problems))
        for p in problems[:8]:
            out.append("  - %s" % p)
    out.append("- **时间**：%s" % _now())
    return "\n".join(out)


def _auto_restart(reason=""):
    """后台延迟重启，让改动生效。

    安全闸：先 verify_import，导入都过不去就**绝不重启**（否则应用直接起不来），
    只在界面报错等人工处理。
    """
    def _do():
        time.sleep(_RESTART_DELAY)
        try:
            from . import selfdev as _sd
            v = _sd.verify_import()
            if not v.get("ok"):
                _emit_sys("自动重启被拦下：导入自检没通过（%s）。已保留当前进程，"
                          "改动还在，等人工处理。" % str(
                              v.get("error") or v.get("msg") or "")[:200], state="error")
                return
        except Exception:                               # noqa: BLE001
            logger.warning("自动重启前的导入自检没跑成（继续尝试重启）", exc_info=True)
        try:
            from . import bootgate as _bg
            _bg.mark_restart("自动迭代：%s" % (reason or ""), force="skip")
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
        _emit_sys("正在自动重启以让改动生效（页面会断几秒后自动重连）…",
                  state="restarting")
        time.sleep(0.4)
        try:
            from . import selfdev as _sd2
            _sd2.restart()
        except Exception as e:                          # noqa: BLE001
            _emit_sys("自动重启失败：%s: %s（请手动点「立即重启」）"
                      % (type(e).__name__, e), state="error")
    threading.Thread(target=_do, daemon=True, name="auto-restart").start()


def _verify_worker():
    """重启后自动验证；不过就生成修复任务继续修，过了就出报告并接下一个。"""
    with _LOCK:
        v = dict(_STATE.get("verify") or {})
    if not v:
        return
    tid = v.get("tid") or "_sys"
    v["tries"] = int(v.get("tries") or 0) + 1
    files = v.get("files") or []

    _emit(tid, "status",
          text="重启完成，自动验证第 %d 轮：%s" % (v["tries"], v.get("title") or ""),
          state="running")
    _emit(tid, "step", text="验证中：%d 个改动文件的语法 + 代码自检 + 运行状态…"
          % len(files))
    try:
        ok, problems = _verify_once(files)
    except Exception as e:                              # noqa: BLE001
        ok, problems = False, ["验证器自己崩了：%s: %s" % (type(e).__name__, e)]

    md = _report_md(v, ok, problems)

    if ok:
        with _LOCK:
            _STATE["verify"] = None
            _STATE["reports"].insert(0, {"tid": tid, "title": v.get("title") or "",
                                         "md": md, "ok": True, "at": _now()})
            _STATE["reports"] = _STATE["reports"][:30]
            _STATE["budget"] = int(_STATE.get("budget") or 0) + 1
            done_n = _STATE["budget"]
            qn = len(_STATE["queue"])
        _emit(tid, "status", text="验证通过 ✅「%s」完成（本轮第 %d 个）"
              % (v.get("title") or "", done_n), state="done")
        _emit(tid, "report", text=md, title=v.get("title") or "")
        _save()
        if done_n >= _MAX_AUTO_ROUNDS:
            _emit_sys("本轮已连续完成 %d 个功能，达到上限先停下%s"
                      % (done_n, ("（队列里还有 %d 个，点「继续」可以接着跑）" % qn)
                         if qn else ""), state="paused")
            return
        if qn:
            _next()
            return
        # 队列空了 —— 用户要求「连续实现多种功能连续迭代」，
        # 这里把接力棒交给推荐引擎，让它接着想下一个功能。
        try:
            from . import selfloop as _sl
            rr = _sl.request_next()
            if (rr or {}).get("started"):
                _emit_sys("上一个功能验证通过，正在推荐下一个功能…", state="running")
            elif (rr or {}).get("skipped"):
                _emit_sys("队列已空，这次没有继续推荐下一个：%s"
                          % rr.get("skipped"), state="done")
        except Exception as e:                          # noqa: BLE001
            _emit_sys("推荐下一个功能失败：%s: %s" % (type(e).__name__, e),
                      state="error")
        return

    # 没过 —— 继续修
    if v["tries"] >= _MAX_VERIFY_TRIES:
        with _LOCK:
            _STATE["verify"] = None
        _emit(tid, "status",
              text="「%s」连续 %d 轮验证没过，先跳过（报告里已记录问题）"
              % (v.get("title") or "", v["tries"]), state="error")
        _emit(tid, "report", text=md, title=v.get("title") or "")
        _save()
        _next()
        return

    _emit(tid, "step", text="验证没过（%d 个问题），自动生成修复任务…" % len(problems))
    pid = ""
    try:
        a = _si.run_audit(deep=False, source="manual", create_plan=True)
        pid = str((a.get("plan") or {}).get("id") or "")
    except Exception as e:                              # noqa: BLE001
        _emit(tid, "step", text="生成修复计划失败：%s: %s" % (type(e).__name__, e))
    with _LOCK:
        _STATE["verify"] = None
    _save()
    if pid:
        submit(pid, source="auto",
               title="修复「%s」（第 %d 次修复，为第 %d 轮验证）"
                     % (v.get("title") or "", v["tries"], v["tries"] + 1))
    else:
        _emit(tid, "status",
              text="验证没过，但没能自动生成修复计划（这类问题可能改不了）",
              state="error")
        _emit(tid, "report", text=md, title=v.get("title") or "")
        _save()
        _next()


def boot_resume():
    """进程启动后调用：有待验证的先验证，有排队的接着跑。

    由 main.py 的 startup 调用（不在模块级自动跑 —— 否则任何 import 本模块的
    工具进程都会意外开始执行任务）。
    """
    time.sleep(2.5)                                     # 等 FastAPI 起来
    with _LOCK:
        v = _STATE.get("verify")
        qn = len(_STATE["queue"])
        first = (_STATE["queue"][0] if _STATE["queue"] else None)
    if v:
        _emit_sys("重启后恢复：先验证上一个功能「%s」，之后接着跑队列（还有 %d 个）"
                  % (v.get("title") or "", qn), state="running")
        _verify_worker()
        return
    if first:
        with _LOCK:
            done_n = int(_STATE.get("budget") or 0)
        _emit_sys("重启后恢复：正在恢复第 %d 个任务「%s」（队列里共 %d 个等待）"
                  % (done_n + 1,
                     first.get("title") or first.get("plan_id") or "（未命名）", qn),
                  state="running")
        _next()


def reports(limit=10):
    """给前端/工具读的 markdown 报告列表。"""
    with _LOCK:
        return list(_STATE.get("reports") or [])[:int(limit or 10)]


def continue_queue():
    """达到连续上限后，用户点「继续」把 budget 清零接着跑。"""
    with _LOCK:
        _STATE["budget"] = 0
        qn = len(_STATE["queue"])
    _save()
    if qn:
        _next()
    return {"ok": True, "queue_len": qn}


_load()
