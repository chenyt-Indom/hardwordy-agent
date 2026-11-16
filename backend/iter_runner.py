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
}
_FILE = _si._DATA / "task.json"


# ------------------------------------------------------------------ 落盘

def _load():
    """启动时把上次的事件流/历史读回来，刷新页面后还能看到。"""
    try:
        if _FILE.exists():
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            with _LOCK:
                _STATE["seq"] = int(d.get("seq") or 0)
                _STATE["events"] = list(d.get("events") or [])[-_MAX_EVENTS:]
                _STATE["history"] = list(d.get("history") or [])[:_MAX_HISTORY]
                # running/queue 不恢复：进程重启后没有线程在跑它们了
                _STATE["running"] = None
                _STATE["queue"] = []
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _save():
    """写盘。失败不影响执行（历史丢了也比任务跑不完强）。"""
    try:
        with _LOCK:
            d = {"seq": _STATE["seq"],
                 "events": _STATE["events"][-_MAX_EVENTS:],
                 "history": _STATE["history"][:_MAX_HISTORY],
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
        _emit(tid, "status", text="正在迭代中…", state="running")
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
        # 只有**真的改了文件**才提示 —— 一条都没改成却让人重启是噪音。
        if changed and ok_n:
            # ★ 2026-11-16：自动迭代真的改了文件 → 告诉重启闸门「这次重启
            #   是自动迭代引起的」。用户要求这种重启不要自检，否则会
            #   「自检 → 修复 → 重启 → 又自检」无限套娃。
            try:
                from . import bootgate as _bg
                _bg.mark_auto_change(changed, note="自动迭代：%s" % str(item.get("title") or "")[:60])
            except Exception:                            # noqa: BLE001
                logger.debug("标记自动迭代改动失败（不影响迭代）", exc_info=True)
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
            _emit(nxt["tid"], "status", text="前面的任务结束了，开始执行这个计划…",
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


_load()
