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
import os
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
    # ★ 2026-11-23：**跨计划**累加的连续验证失败次数。每轮修复都是新 plan_id，
    #   所以 verify.tries 会归零；这个字段不归零，用来兜住无限重启。
    "fail_chain": 0,
    "reports": [],       # 每个功能完成后的 markdown 报告（新的在前）
    "budget": 0,         # 本轮连续自动迭代已完成的功能数（上限见 _MAX_AUTO_ROUNDS）
}
_FILE = _si._DATA / "task.json"

# 用户 2026-11-19 选择：「连续自动迭代最多跑 10 个功能就停」。
_MAX_AUTO_ROUNDS = 10
# 同一个功能验证不过时的重试上限。用户选的是「继续重试直到通过」，
# 但必须有兜底 —— 否则一个死循环功能会把队列永远卡住。
# ★ 2026-11-20 用户：「假如一个功能过于复杂呢？」→ 曾改为 30 轮。
# ★ 2026-11-23 用户问「验证没问题之后会自动停止不陷入无限重启吗」→ 查出真缺口：
#   `tries` 只在 **同一 plan_id** 内累加（见 mark_verify），而每轮修复都
#   `run_audit(create_plan=True)` 生成**全新 plan_id**（self_iterate 用时间戳当 id，
#   绝不复用）→ tries 每轮归零，30 这个上限在实际链路里**永远触发不了**。
#   所以真正管用的护栏是下面的 _MAX_FAIL_CHAIN（跨计划累加）。
#   这里降到 5：同计划内 5 次仍不通过，也没必要再试。
_MAX_VERIFY_TRIES = 5
# ★ 2026-11-23 用户：「然后验证没问题之后会自动停止不陷入无限重启吗」
#   → 连续失败链上限：**跨计划**累加的连续验证失败次数。
#   达到就**不再自动生成修复任务**，停下来等用户决定 —— 这是防「改→重启→验证→
#   不过→再改」无限烧 token 的最终护栏。任何一次验证通过即清零。
_MAX_FAIL_CHAIN = 3
# ★ 2026-11-22 用户要求：「务必确保反复验证修改之后反复验证完全没问题之后
#   才显示『已验证』」。所以通过一次不算数 —— 必须**连续**这么多轮都通过。
#   每轮 = 语法编译 + 代码自检 + 真跑一遍回归（run_disk，从磁盘重载新代码）。
_VERIFY_CONFIRM = 2

# 评分结论的中文说法（跑分汇报用）
_VERDICT_CN = {"improved": "进步", "regressed": "退步", "flat": "持平",
               "rebased": "重建基准", "unknown": "无可比", "skip": "跳过",
               "busy": "上一次还在评", "error": "评分出错"}

# 队列里「等待中」的任务最多几个（★ 2026-11-20 用户明确要求：不能超过 10 个）。
# 注意这和上面的 _MAX_AUTO_ROUNDS（本轮已完成功能数）是两回事：
#   · _MAX_QUEUE      = 队列能排多长（满了拒绝新任务）
#   · _MAX_AUTO_ROUNDS= 一轮连续做完几个就停下来等你点「继续」
_MAX_QUEUE = 10

# 单个功能最长允许跑多久（秒，含反复修）。用户要求「设上限但要可调」，
# 所以真正生效的值从 settings.json 的 iter_task_timeout_min 读，这里是默认值。
_DEFAULT_TASK_TIMEOUT_MIN = 30
# 自动重启前等的秒数：给前端一次轮询把「正在重启」事件收下来的机会。
_RESTART_DELAY = 1.5
# 撞上「另一个执行占着锁」时，隔多久把任务重新提上来（秒）。
_LOCK_RETRY_DELAY = 20

# ★ 2026-11-20 修（用户报「卡住不动了」）：
#   重启后 _STATE["running"] 会从 task.json 恢复出来，但那条的 worker 线程
#   已经随**旧进程**消失了 —— 它是个「孤儿」。boot_resume 原来只判断
#   `busy = bool(running)`，看见孤儿就以为还有活干，直接 return，
#   结果队列里排着的任务永远轮不到，界面上就是一张卡死不动、秒数一直涨的卡片。
#   判据：started_at 早于本进程启动时间 ⇒ 必是上一个进程留下的孤儿。
_BOOT_AT = time.time()
# ★ 2026-11-20 用户要求「重启时要能继续验证」：
#   验证线程同一时刻只允许一个。原来 boot_resume 直接调 _verify_worker()，
#   如果那时恰好有任务在跑，验证会和「改代码」同时进行 —— 验的是改到一半的文件，
#   还会和 _next() 抢着起任务。用这个标记 + _start_verify() 统一入口。
_VERIFYING = [False]

# ★ 2026-11-22 用户报「推荐迭代完自动重启之后没有继续」：
#   _BOOT_AT 只能识别「上个进程留下的孤儿」，识别不出**本进程内**线程死掉
#   的任务（worker 异常退出 / 被硬杀，finally 没跑到）。那种情况下 running
#   记录会一直留着，submit 的 dup 判断永远返回「已在跑」，
#   于是同一个计划再也提交不进去，界面上看就是「重启之后没继续」。
#   这里登记「这个 tid 的 worker 是否还在」，用线程对象自己判存活。
_LIVE = {}


def _run_alive(run) -> bool:
    """running 里的那个任务，它的 worker 线程是否真的还活着（不是僵尸）。

    判僵尸的三种情况：
      ① 没有 running 记录 —— 本来就没任务；
      ② started_at 早于本进程启动时间 —— 上一个进程留下的孤儿；
      ③ 本进程没登记过它的 worker，或线程已 not is_alive() —— 线程死了但记录没清。
    """
    if not run:
        return False
    try:
        if float(run.get("started_at") or 0) < _BOOT_AT - 0.5:
            return False
    except (TypeError, ValueError):
        return False
    tid = str(run.get("tid") or "")
    th = _LIVE.get(tid)
    if th is None:
        # 刚 submit、还没登记完的极短窗口：给 20 秒宽限，别把刚起的任务误杀。
        try:
            return (time.time() - float(run.get("started_at") or 0)) < 20
        except (TypeError, ValueError):
            return False
    try:
        return bool(th.is_alive())
    except Exception:                                   # noqa: BLE001
        return False


def done_titles(limit=60):
    """已经跑完/在跑/在排的功能名（给 featradar 查重用，避免重复推荐）。"""
    out = []
    with _LOCK:
        for h in (_STATE.get("history") or [])[:int(limit or 60)]:
            t = str(h.get("title") or "").strip()
            if t:
                out.append(t)
        for r in (_STATE.get("reports") or [])[:int(limit or 60)]:
            t = str(r.get("title") or "").strip()
            if t:
                out.append(t)
        for q in (_STATE.get("queue") or []):
            t = str(q.get("title") or "").strip()
            if t:
                out.append(t)
        run = _STATE.get("running")
        if run and str(run.get("title") or "").strip():
            out.append(str(run.get("title")).strip())
    # 去重但保持顺序
    seen, res = set(), []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        res.append(t)
    return res


def _start_verify(wait_busy=True):
    """起一个验证线程（同一时刻只允许一个）。返回是否真的起来了。

    ★ 用户要求「重启之后立即进行验证」，但**不能**和正在跑的任务并发：
      有任务在跑时先等它跑完（最多等一个任务超时），再验证。
    """
    with _LOCK:
        if _VERIFYING[0]:
            return False
        if not _STATE.get("verify"):
            return False
        _VERIFYING[0] = True

    def _run():
        try:
            if wait_busy:
                deadline = time.time() + max(60, int(task_timeout_sec() or 1800))
                waited = False
                while time.time() < deadline:
                    with _LOCK:
                        r = _STATE.get("running")
                        if not r or float(r.get("started_at") or 0) < _BOOT_AT - 0.5:
                            break
                    if not waited:
                        _emit_sys("有待验证的功能，等当前任务跑完再验证（不打断它）…",
                                  state="restarting")
                        waited = True
                    time.sleep(2)
            _verify_worker()
        except Exception as e:                          # noqa: BLE001
            logger.warning("验证线程异常：%s: %s", type(e).__name__, e)
            _emit_sys("自动验证出错：%s: %s（不影响应用使用）"
                      % (type(e).__name__, e), state="error")
        finally:
            _VERIFYING[0] = False

    try:
        threading.Thread(target=_run, daemon=True, name="iter-verify").start()
    except Exception as e:                              # noqa: BLE001
        # 线程起不来（极少见，如资源耗尽）必须复位标记 —— 否则 _VERIFYING
        # 永远是 True，以后再也起不了验证，队列会静默停住。
        _VERIFYING[0] = False
        logger.warning("验证线程起不来：%s: %s", type(e).__name__, e)
        _emit_sys("自动验证启动失败：%s: %s" % (type(e).__name__, e), state="error")
        return False
    return True


def task_timeout_sec():
    """单个功能的最长执行时间（秒）。settings.json 里 iter_task_timeout_min 可改。

    用户 2026-11-20：「设上限但要可调」—— 默认 30 分钟，设成 0 表示不限制。
    读不到设置就用默认值，绝不因为设置文件坏了把迭代卡死。
    """
    try:
        v = _si.load_settings().get("iter_task_timeout_min", _DEFAULT_TASK_TIMEOUT_MIN)
        mins = float(v)
    except Exception:                                   # noqa: BLE001
        mins = float(_DEFAULT_TASK_TIMEOUT_MIN)
    if mins <= 0:
        return 0
    return int(mins * 60)


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
                # ★ 连续失败链必须跨重启保留：否则「改→重启→验证不过」每重启一次
                #   计数就归零，护栏又失效了 —— 这正是无限重启的入口。
                _STATE["fail_chain"] = int(d.get("fail_chain") or 0)
                # 队列恢复：把上次「正在跑」的那个放回队首（它没跑完）
                q = list(d.get("queue") or [])
                run = d.get("running")
                hist = _STATE["history"] or []
                # ★ 2026-11-20 用户要求「重启后能继续任务或验证，别重复做」：
                #   原来这里**无条件**把 running 放回队首。可 running 有两种死法：
                #     ① 真没跑完（进程被杀）→ 必须放回队首接着跑；
                #     ② 其实已经跑完、只是占位没清（老版本的 bug 留下的）→
                #        放回去就会**把做完的功能重跑一遍**，白烧一轮。
                #   判据：history 里有同 tid 的结束记录 ⇒ 它已经完成了，不再排队。
                #   （boot_resume 里那段「已结束的不重跑」其实永远走不到 —— 本函数
                #    在 import 时就跑了，早把 running 置成 None，所以必须修在这里。）
                if run and run.get("plan_id"):
                    _tid = str(run.get("tid") or "")
                    _done = bool(_tid) and any(
                        str(h.get("tid") or "") == _tid for h in hist)
                    if _done:
                        logger.warning("上次的 running 其实已结束（history 有记录），"
                                       "不再重跑：%s", run.get("plan_id"))
                    else:
                        q.insert(0, {"tid": run.get("tid"),
                                     "plan_id": run.get("plan_id"),
                                     "source": run.get("source") or "auto",
                                     "title": run.get("title") or "",
                                     "at": run.get("started_at") or time.time(),
                                     "max_n": run.get("max_n") or 0,
                                     # ★ 2026-11-20：内容快照要跟着恢复，
                                     #   否则重启后卡片只剩一个 plan_id。
                                     "brief": run.get("brief") or {},
                                     "resumed": True})
                # 去重：同 tid 在队列里只留一条（旧数据可能重复写盘）
                _seen = set()
                _q2 = []
                for x in q:
                    k = str(x.get("tid") or "") or str(x.get("plan_id") or "")
                    if k and k in _seen:
                        continue
                    if k:
                        _seen.add(k)
                    _q2.append(x)
                _STATE["queue"] = _q2
                _STATE["running"] = None
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _save():
    """写盘。失败不影响执行（历史丢了也比任务跑不完强）。

    ★ 2026-11-23：只读进程（诊断脚本、静态检查、回归子进程）绝不能写这个文件。
      它们 import 本模块时 `_load()` 会把 running 当孤儿清掉，只要顺手 `_save()`
      一次，正在跑的真实任务记录就从磁盘上消失了 —— 实测踩过：一条正在跑的
      任务被诊断进程的 import 清成了 null。设 MM_ITER_READONLY=1 时直接返回。
    """
    if os.environ.get("MM_ITER_READONLY") == "1":
        return
    try:
        with _LOCK:
            d = {"seq": _STATE["seq"],
                 "events": _STATE["events"][-_MAX_EVENTS:],
                 "history": _STATE["history"][:_MAX_HISTORY],
                 # ★ 队列必须落盘：否则重启后等待中的任务全丢
                 "queue": _STATE["queue"],
                 "running": _STATE["running"],
                 "verify": _STATE.get("verify"),
                 "fail_chain": int(_STATE.get("fail_chain") or 0),
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
def mark_verify(plan_id, title="", files=None, tid="", source="direct"):
    """登记「刚改完、还没验证」—— 供**非队列路径**使用。

    ★ 2026-11-23 用户报：「前面的任务有没有进行跑分验证？」—— 没有。
    根因：`/api/selfiter/execute` 直接调 `execute_approved`，**不经过 `_worker`**，
    所以从不写 `_STATE["verify"]`；重启后 `boot_resume` 也就无从接着验证。
    结果「连续两轮验证 → 跑分 → 退步反思 → 写日志」整条链只在队列路径生效。

    这里让直接执行也登记，链路与队列路径完全一致（同一份 verify 记录、
    同一个 `_verify_worker`），不另造一套。

    返回 {ok, verify, why}。没有改动文件时不登记 —— 没改东西无需验证。
    """
    files = [str(f) for f in (files or []) if f]
    if not files:
        return {"ok": False, "why": "这次没有改动任何文件，无需验证"}
    pid = str(plan_id or "")
    with _LOCK:
        prev = _STATE.get("verify") or {}
        rec = {
            "tid": tid or ("direct_%s" % (pid or int(time.time()))),
            "plan_id": pid,
            "title": str(title or "")[:120],
            "files": files[:20],
            # 同一功能反复修时轮次要累加，不能每轮清零
            "tries": int(prev.get("tries") or 0) if prev.get("plan_id") == pid else 0,
            "at": time.time(),
            "source": str(source or "direct"),
        }
        _STATE["verify"] = rec
    _save()
    try:
        _emit(rec["tid"], "status",
              text="已登记待验证：%s（%d 个文件）—— 重启后自动验证、跑分"
                   % (rec["title"] or pid or "未命名", len(rec["files"])),
              state="running", verified=False)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"ok": True, "verify": rec}

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
            # ★ 2026-11-20 用户要「两者都要」：卡片显示「队列 7/10」+ 列出排队任务。
            #   所以除了 queue 明细，还要给上限和剩余位。
            "max_queue": _MAX_QUEUE,
            "queue_free": max(0, _MAX_QUEUE - len(q)),
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


def _plan_title(plan_id):
    """从计划里推断一个能给人看的「功能名」。

    ★ 2026-11-20 用户要求「自迭代卡片上说明正在迭代什么功能」——
      但入队时前端只传 plan_id，不传标题，于是卡片一直显示「正在迭代中…」。
      这里退一步自己推断：优先第一条动作标题（那就是实际要做的事），
      其次用计划的 summary（诊断结论，稍抽象但能看懂）。
    """
    try:
        r = _si.get_plan(plan_id)
        p = (r or {}).get("plan") or {}
        for a in (p.get("actions") or []):
            if isinstance(a, dict):
                t = str(a.get("title") or "").strip()
                if t:
                    return t[:60]
        s = str(p.get("summary") or "").strip()
        if s:
            return s[:60]
    except Exception:                                    # noqa: BLE001
        logger.debug("推断计划标题失败", exc_info=True)
    return ""


def _plan_brief(plan_id):
    """把计划里的**任务内容**抽成一份小快照，存进队列项 / running。

    ★ 2026-11-20 用户：「防止任务内容因中断补充重启后丢失」。
      队列原来只存 plan_id 一个引用 —— 计划缓存被删、被覆盖，或 plans.json
      损坏时，队列里那条就只剩一个死 id，用户完全看不出当初要做什么，
      重启后也没法接着做。这里把「要做什么」抄一份进队列项（很小：
      摘要 + 动作标题 + 目标文件），重启后即使计划找不到，也能在卡片上
      说清任务内容、并明确告诉用户「计划文件丢了」而不是静默消失。
    """
    out = {"summary": "", "actions": [], "files": []}
    try:
        r = _si.get_plan(plan_id)
        p = (r or {}).get("plan") or {}
        out["summary"] = str(p.get("summary") or "").strip()[:200]
        for a in (p.get("actions") or [])[:8]:
            if not isinstance(a, dict):
                continue
            t = str(a.get("title") or "").strip()
            if t:
                out["actions"].append(t[:60])
            for f in (a.get("files") or [])[:6]:
                if isinstance(f, str) and f and f not in out["files"]:
                    out["files"].append(f)
    except Exception:                                    # noqa: BLE001
        logger.debug("抽计划快照失败", exc_info=True)
    return out


def _brief(t):
    if not t:
        return None
    return {"tid": t.get("tid"), "plan_id": t.get("plan_id"),
            "source": t.get("source"), "title": t.get("title"),
            "state": t.get("state"), "started_at": t.get("started_at"),
            "elapsed": round(time.time() - (t.get("started_at") or time.time()), 1),
            "cancel": bool(t.get("cancel")),
            # 任务内容快照（用户 2026-11-20：中断/重启后不能丢内容）
            "brief": t.get("brief") or {},
            "plan_missing": bool(t.get("plan_missing"))}


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

    # ★ 2026-11-22 用户报「推荐迭代完自动重启之后没有继续啊」：
    #   真实原因不是「没继续」—— 队列恢复了、任务也开跑了，但它 0.3 秒就
    #   报「没有已批准的动作」失败（22:37:48 那条事件），计划里 7 条全是 pending。
    #   auto 来源没事（worker 会先 auto_approve_only 自动批准），
    #   而 manual / featradar 等来源没人批准 → 必然白跑一趟，还让人以为卡住了。
    #   所以这里先查一遍：没有已批准动作就**不许进队列**，并说清该怎么办。
    if str(source or "auto") != "auto":
        try:
            _r = _si.get_plan(plan_id)
            _acts = [a for a in (((_r or {}).get("plan") or {}).get("actions") or [])
                     if isinstance(a, dict)]
            if _acts and not any(a.get("status") == "approved" for a in _acts):
                _pd = sum(1 for a in _acts if a.get("status") == "pending")
                return {"ok": False, "state": "rejected",
                        "error": ("这个计划没有已批准的动作（%d 条待批准）—— "
                                  "先点「批准」再执行，否则排进去也是 0 秒失败"
                                  % _pd) if _pd
                                 else "这个计划没有可执行的动作（都已执行或已被拒绝）"}
        except Exception:                               # noqa: BLE001
            logger.debug("检查已批准动作失败（不拦，交给执行器判断）", exc_info=True)

    tid = "t_%s" % _si._pid()
    # ★ 2026-11-20：前端 enqueue 只传 id，不传 title —— 这里自己补，
    #   否则卡片永远只有一句「正在迭代中…」，看不出在做什么功能。
    _ttl = str(title or "").strip() or _plan_title(plan_id)
    item = {"tid": tid, "plan_id": plan_id, "source": str(source or "auto"),
            "title": _ttl, "at": time.time(),
            "max_n": int(max_n or 0),
            # ★ 2026-11-20 用户：「防止任务内容因中断 / 重启后丢失」——
            #   把要做什么抄一份进队列项，别只留一个 plan_id 死引用。
            "brief": _plan_brief(plan_id)}

    with _LOCK:
        run = _STATE["running"]
        # ★ 2026-11-22：僵尸任务（worker 线程已死、记录还留着）必须先回收。
        #   否则下面的 dup 判断会永远返回「已在跑」，这个计划再也提交不进去，
        #   表现就是「重启之后没继续」。
        if run and not _run_alive(run):
            logger.warning("提交时发现僵尸任务（线程已不在），已回收：%s",
                           run.get("plan_id"))
            _STATE["running"] = None
            run = None
        # 同一份计划不要重复排队：动作跑完就是 done 了，再排一次只会
        # 立刻失败（「没有已批准的动作」），白占一个队列位、还让用户以为出了问题。
        if run and run.get("plan_id") == plan_id:
            return {"ok": True, "state": "running", "tid": run["tid"],
                    "ahead": 0, "dup": True}
        for _x in _STATE["queue"]:
            if _x.get("plan_id") == plan_id:
                return {"ok": True, "state": "waiting", "tid": _x["tid"],
                        "ahead": len(_STATE["queue"]), "dup": True}
        # ★ 2026-11-20 用户：「队列里不能超过十个等待的任务」——
        #   满了就直接拒绝并说清楚，不要静默排进去让用户以为接住了。
        #   注意只有「已有任务在跑、新任务要排队」时才算占用队列位；
        #   没有任务在跑时直接开跑，不受这个上限影响。
        if run and len(_STATE["queue"]) >= _MAX_QUEUE:
            return {"ok": False, "state": "rejected",
                    "error": "队列已满 %d 个，先等或取消几个再提交"
                             % _MAX_QUEUE,
                    "queue_len": len(_STATE["queue"]), "max_queue": _MAX_QUEUE}
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
                             "cancel": False, "max_n": item["max_n"],
                             "brief": item.get("brief") or {}}
        # 用户要求：「自迭代卡片上说明正在迭代什么功能」——
        # 有标题就直接写出来，别只给一句「正在迭代中」让人猜。
        _emit(tid, "status",
              text=("正在迭代：%s" % item["title"]) if item.get("title")
                   else "正在迭代中…", state="running")
    _save()

    th = threading.Thread(target=_worker, args=(item,), daemon=True,
                          name="iter-%s" % tid)
    # 先登记再 start：start 之前 is_alive() 还是 False，
    # 若这中间有别的请求进来判存活，会被误判成僵尸、把刚起的任务回收掉。
    with _LOCK:
        _LIVE[str(tid)] = th
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
    # ★ 2026-11-20 修：这个任务「真正在做的功能名」。
    #   item["title"] 是入队时用 _plan_title() 推断的「计划首条动作」，
    #   但一个计划往往有 7~8 条动作，这次实际执行的是后面某一条 ——
    #   action_start 会把 _STATE["running"]["title"] 更新成真正在做的那条。
    #   状态事件 / verify 若还拿 item["title"]，界面上就会出现
    #   「显示在验证 A，实际改的是 B」，用户以为「重启后没验证」。
    #   （实测 11-20 03:22 那次：显示「回复形态闸门」，实际改的是 jobplan/studio.js）
    _cur_title = ""
    # 改了文件且开启自动重启时置 True —— finally 里据此决定「重启」还是「接下一个」
    auto_rst = False
    # ★ 2026-11-20：撞到「另一个执行占着锁」时置 True —— 把任务放回队首稍后重试，
    #   而不是报一张「没有可执行的动作」的假失败卡。
    requeued = False

    # ★ 2026-11-20：只有 running 槽位确实是自己时才干活。
    #   防止被别的路径顶掉之后出现两个 worker 同时改代码。
    with _LOCK:
        _cur = _STATE.get("running")
        if not _cur or str(_cur.get("tid")) != str(tid):
            logger.warning("iter worker %s 的槽位已被别的任务占用，本次直接退出", tid)
            return

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
            elif kind == "action_start":
                # ★ 2026-11-20：一个计划里有多条动作，每开始一条就把卡片标题
                #   换成这条的功能名，用户一眼能看出「现在在迭代哪个功能」。
                _t = str(kw.get("title") or "").strip()
                if _t:
                    with _LOCK:
                        if _STATE.get("running"):
                            _STATE["running"]["title"] = _t[:60]
                    try:
                        _stage("action_start",
                               "第 %s/%s 条：%s" % (kw.get("index") or "?",
                                                   kw.get("total") or "?", _t))
                    except Exception:                    # noqa: BLE001
                        logger.debug("写阶段流水失败（不影响迭代）", exc_info=True)
                    _emit(tid, "status", text="正在迭代：%s" % _t[:60],
                          state="running")

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

        # ★ 2026-11-20：执行锁被另一个执行占着（比如用户手动点了「执行」，
        #   或上一个 worker 还没退干净）。这不是本任务的问题 —— 把它放回队首，
        #   过一会儿重试，别报「这次没有可执行的动作」误导用户。
        if err_all and "已有一个自动执行在进行中" in err_all:
            with _LOCK:
                _STATE["queue"].insert(0, item)
                _STATE["running"] = None
            _save()
            _emit(tid, "step",
                  text="另一个执行正在进行，本任务已放回队列，%d 秒后自动重试…"
                       % _LOCK_RETRY_DELAY)
            _emit(tid, "status", text="等待重试（另一个执行正在进行）", state="waiting")
            threading.Timer(_LOCK_RETRY_DELAY, _next).start()
            requeued = True
            return

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
            # 真正在做的那条动作名（action_start 已把 running.title 更新过）
            with _LOCK:
                _cur_title = (str((_STATE.get("running") or {}).get("title") or "").strip()
                              or str(item.get("title") or "").strip())
            _auto = _should_auto_restart(item.get("source"))
            if _auto:
                with _LOCK:
                    prev = _STATE.get("verify") or {}
                    _STATE["verify"] = {
                        "tid": tid, "plan_id": plan_id,
                        "title": _cur_title,
                        "files": changed[:20],
                        # 同一功能反复修时轮次要累加，不能每轮清零
                        "tries": int(prev.get("tries") or 0)
                                 if prev.get("plan_id") == plan_id else 0,
                        "at": time.time(),
                    }
                _save()
                _emit(tid, "status",
                      text="✅ 任务完成：%s（改了 %d 个文件，马上自动重启验证）"
                           % (_cur_title or "未命名功能", len(changed)),
                      state="running", verified=False)
                _emit(tid, "restart",
                      text="这次改了 %d 个文件，稍后自动重启并验证" % len(changed),
                      files=changed[:12])
                auto_rst = True
            else:
                # ★ 不自动重启 ≠ 不验证：把「待验证」记下来，用户点重启后
                #   boot_resume 会自动接着验证 + 跑分 + 反思，链路不断。
                with _LOCK:
                    prev = _STATE.get("verify") or {}
                    _STATE["verify"] = {
                        "tid": tid, "plan_id": plan_id,
                        "title": _cur_title,
                        "files": changed[:20],
                        "tries": int(prev.get("tries") or 0)
                                 if prev.get("plan_id") == plan_id else 0,
                        "at": time.time(),
                    }
                _save()
                _emit(tid, "restart",
                      text="这次改了 %d 个文件，需要重启才生效 —— 你点「立即重启」后，"
                           "会自动接着验证、跑分、反思" % len(changed),
                      files=changed[:12], manual=True)

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
        # ★ 2026-11-22：注销本任务的存活登记。必须放在 _next()/_auto_restart()
        #   之前 —— 它们要用 _run_alive() 判「还有没有任务在跑」，
        #   注销晚了会让刚结束的任务被当成还活着，接下一个 / 重启都要白等。
        with _LOCK:
            _LIVE.pop(str(tid), None)
        if requeued:
            # 已经放回队列、并挂了一个延迟的 _next —— 这里不能再接下一个，
            # 否则会立刻把同一个任务再捞起来，空转。
            pass
        elif auto_rst:
            # 走「重启 → 自动验证 → 再接下一个」这条链，
            # 这里**不能**调 _next()：否则下一个任务会在重启前抢跑，
            # 跑到一半进程被替换掉，进度就丢了。
            _auto_restart(reason=_cur_title or item.get("title") or plan_id, tid=tid)
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
        # ★ 2026-11-20 修（用户报「卡住不动了」，截图里队列有任务却一直不跑）：
        #   原来 _finish **不清** running，而 _next() 开头是「有 running 就 return」。
        #   于是任务一结束（尤其没改文件、走 _next 这条路的），running 还挂着自己，
        #   _next 看到就以为还有活干，直接返回 —— 队列永远不动，界面卡死。
        #   这里任务确实结束了，必须把占位清掉，下一个才能被提上来。
        if (_STATE.get("running") or {}).get("tid") == tid:
            _STATE["running"] = None
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
    """当前任务结束后：把队列里的下一个提上来接着跑。

    ★ 2026-11-20 修（用户报「好像卡住不动了」）：
      原来这里**无条件**先 `_STATE["running"] = None` 再取队首。
      重启后 boot_resume 会等 2.5 秒再调它，而这两秒半里自迭代很可能已经
      submit 了一个任务（running 已占位）—— 于是正在跑的那个被顶掉，
      变成**两个 worker 同时改代码**：抢同一个执行锁，输的那个立刻报
      「已有一个自动执行在进行中，本次跳过」，界面上就是一张永远停在
      「正在迭代中 37s」的卡片 + 一串「没有可执行的动作」的假失败卡
      （实测 task.json 的 history 里连留了两条这个错误）。
      所以：已经有任务在跑时，这里必须什么都不做。
    """
    nxt = None
    need_verify = False
    with _LOCK:
        run = _STATE.get("running")
        # 孤儿（线程已随旧进程消失）不能挡住队列，直接清掉继续取下一个
        if run and not _run_alive(run):
            logger.warning("发现残留的孤儿/僵尸任务（线程已不在），已回收：%s",
                           run.get("plan_id"))
            _STATE["running"] = None
            run = None
        if run:
            return
        # ★ 2026-11-20 用户要求「重启时要能继续任务或验证」：
        #   有待验证的功能时，**先验证再起下一个任务**。否则会出现
        #   「任务A的改动还没验证，任务B已经在改同一批文件」——
        #   验的是改到一半的代码，报告也就不可信了。
        if _STATE.get("verify"):
            need_verify = True
        elif _STATE["queue"]:
            nxt = _STATE["queue"].pop(0)
            # 老队列项（旧代码提交的）标题是空的 → 提上来时补推断一次，
            # 否则卡片只能显示「正在迭代中…」，看不出在做什么功能。
            if not str(nxt.get("title") or "").strip() and nxt.get("plan_id"):
                nxt["title"] = _plan_title(nxt.get("plan_id"))
            # ★ 2026-11-20：一律用 .get() —— 队列项来自落盘恢复，老数据可能缺字段，
            #   直接 nxt["source"] 会 KeyError 把整个队列卡死（实测复现）。
            _STATE["running"] = {"tid": nxt.get("tid") or "",
                                 "plan_id": nxt.get("plan_id") or "",
                                 "source": nxt.get("source") or "manual",
                                 "title": nxt.get("title") or "",
                                 "state": "running", "started_at": time.time(),
                                 "cancel": False, "max_n": nxt.get("max_n") or 0}
            _emit(nxt.get("tid") or "_sys", "status",
                  text=("前面的任务结束了，开始迭代：%s" % nxt["title"])
                       if nxt.get("title") else "前面的任务结束了，开始执行这个计划…",
                  state="running")
    _save()
    if need_verify:
        # 有待验证的功能：先把验证做完（它会自己接着 _next）
        _start_verify()
        return
    if nxt:
        th = threading.Thread(target=_worker, args=(nxt,), daemon=True,
                              name="iter-%s" % (nxt.get("tid") or "x"))
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
        # ★ 2026-11-22 修一个把「反复验证」拖死的问题（实测：单轮卡 200 秒以上）：
        #   原来调 _si.run_audit()，而它内部**无条件再跑一遍回归**
        #   （self_iterate.py 的 RG.run_disk），之后还要过复核层。
        #   于是每轮验证 = 两次回归 + 一次复核，连续两轮就十几分钟，验证根本走不完。
        #   验证器只需要「静态自检的 high/mid」，直接调 code_audit.audit()（约 7 秒）。
        from . import code_audit as _ca
        a = _ca.audit()
        for f in (a.get("findings") or []):
            if f.get("level") in ("high", "mid"):
                problems.append("自检[%s] %s:%s %s —— %s" % (
                    f.get("level"), f.get("file"), f.get("line"),
                    f.get("kind"), f.get("msg")))
    except Exception as e:                              # noqa: BLE001
        problems.append("代码自检没跑成：%s: %s" % (type(e).__name__, e))

    # ★ 2026-11-22 用户要求「反复验证完全没问题之后才显示已验证」：
    #   语法能编过、自检干净，**不等于**功能没被改坏。必须真跑一遍回归。
    #   用 run_disk 而不是 run()：
    #     ① 进程内 run() 跑的是改动前的旧代码（Python 不会自动重载），
    #        等于在验旧版本，「改坏了」根本验不出来；
    #     ② 回归用例里有相对路径（backend/tools.py），进程工作目录不在项目根
    #        时会整片假失败 —— 实测在临时目录里跑出 5 条假失败，把好改动判成退步。
    #   run_disk 在子进程里从磁盘重新 import，且自带 cwd=项目根。
    #   没跑成（异常）也算问题 —— 没验证到就不能宣布已验证。
    try:
        from . import regress as _rg
        runner = getattr(_rg, "run_disk", None) or _rg.run
        try:
            rr = runner(scope="quick", timeout=300)
        except TypeError:
            rr = runner(scope="quick")
        if not rr.get("ok"):
            problems.append("回归失败 %d/%d 条：%s" % (
                int(rr.get("failed") or 0), int(rr.get("total") or 0),
                str(rr.get("summary") or "")[:300]))
    except Exception as e:                              # noqa: BLE001
        problems.append("回归没跑成：%s: %s" % (type(e).__name__, e))

    return (not problems), problems


def _score_after_verify(tid, v):
    """验证通过后真跑一次评分，并与上一版比对。

    ★ 2026-11-22 用户要求：「除了这些还有进行跑分以及反思还有日志记录」。
      验证全过只说明「没改坏」，还要看**分数**：
        · 低于上一版 → scoregate 记低分日志 + 沉淀教训 + 起自动修复（反思改进）
        · 持平/进步  → 只记录，不打扰
    ★ 2026-11-23 用户要求：「跑分不能低于上个版本，不然还得重新改进」。
      所以这里传 block_on_drop=False —— **故意不让 scoregate 自己起修复线程**，
      它只负责记退步日志 + 沉淀教训；真正的修复由本模块统一走
      「生成修复计划 → submit(source=auto) → 自动重启 → 重新验证 → 重新跑分」
      那条链。否则两条修复线并行改代码会互相打架。
    """
    try:
        from . import scoregate as _sg
        sc = _sg.gate(force=True, block_on_drop=False,
                      note="迭代验证通过后自动跑分：%s" % (v.get("title") or ""))
        return sc if isinstance(sc, dict) else {}
    except Exception as e:                              # noqa: BLE001
        return {"ran": False, "verdict": "error",
                "msg": "评分没跑成：%s: %s" % (type(e).__name__, e)}


def _score_md(sc):
    """跑分结果的 markdown 片段（写进报告）。"""
    if not isinstance(sc, dict) or not sc.get("ran"):
        return "- **跑分**：%s" % (str((sc or {}).get("msg") or "没跑成")[:200])
    out = ["- **跑分**：%s → %s（%+.2f，%s）" % (
        sc.get("prev"), sc.get("total"), float(sc.get("delta") or 0),
        _VERDICT_CN.get(str(sc.get("verdict") or ""), sc.get("verdict")))]
    rec = sc.get("rec") or {}
    press = rec.get("press") or {}
    if press.get("items"):
        out.append("- **压分点**：%d 处，共压 %.2f 分" % (
            len(press.get("items") or []), float(press.get("lost") or 0)))
    drop = sc.get("drop") or {}
    if drop:
        out.append("- **退步归因**：%s" % str(drop.get("why") or drop.get("note") or "已记录")[:200])
    return "\n".join(out)


def _log_verify(name, **kw):
    """把验证/跑分写进行为日志（用户要求「还可以写日志」）。

    优先走 actlog（能在体系详情里看到），失败就退回普通日志 —— 记日志这件事
    本身绝不能把验证流程弄挂。
    """
    try:
        from . import actlog as _al
        _al.record_act(name, ok=bool(kw.get("ok", True)),
                       summary=str(kw.get("summary") or "")[:300])
        return
    except Exception:                                   # noqa: BLE001
        logger.debug("actlog 记录失败，退回普通日志", exc_info=True)
    try:
        logger.info("[verify] %s %s", name,
                    " ".join("%s=%s" % (k, v) for k, v in kw.items() if k != "ok"))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


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
    out.append("- **验证**：%s" % (
        "连续 %d 轮全部通过 ✅ 已验证" % _VERIFY_CONFIRM if ok
        else "第 %d 次修复后仍未通过 ❌" % tries))
    out.append("- **验证方式**：改动文件语法编译 + 代码自检（high/mid）"
               "+ 全量回归 + 后端导入与运行状态")
    if ok:
        out.append("- **结果**：语法、代码自检、运行状态全部正常，改动已生效")
        with _LOCK:
            qn = len(_STATE.get("queue") or [])
            done_n = int(_STATE.get("budget") or 0)
        # 注意：报告是在 budget 递增**之前**生成的，这里写「本轮第 N 个」
        # 会永远比 status 事件少 1（2026-11-23 演练实测：报告 0、事件 1）。
        # 轮次以 status 事件为准，报告只写状态，两处口径不再打架。
        out.append("- **状态**：任务完成（已验证）")
        out.append("- **下一步**：%s" % (
            "队列里还有 %d 个功能，自动接着做下一个" % qn if qn
            else "队列已空，本次迭代结束"))
    else:
        out.append("- **结果**：还有 %d 个问题" % len(problems))
        for p in problems[:8]:
            out.append("  - %s" % p)
        out.append("- **下一步**：自动生成修复任务继续修，修完再验证")
    out.append("- **时间**：%s" % _now())
    return "\n".join(out)


# ★ 2026-11-23 用户要求：「非自动迭代的重启就由用户来就可以了，不需要自动重启，
#   这样会打断任务，这些推荐以及待批准这些二级迭代才需要」。
#   所以只有**自动跑起来的**迭代（auto = 自动迭代、featradar = 推荐迭代、
#   approved = 待批准）改完代码才自动重启；manual（用户自己发起的主迭代）
#   重启交给他自己点，避免在用户手头有事时突然断一次服务。
_AUTO_RESTART_SOURCES = ("auto", "featradar", "approved")


def _should_auto_restart(source):
    """这个来源的迭代改完代码后，要不要自动重启。

    · auto      —— 自动迭代（含验证不过后自动生成的修复任务），要
    · featradar —— 推荐迭代，要
    · approved  —— 待批准里批准后执行的二级迭代，要
    · manual    —— 用户自己发起的主迭代，**不要**（自动重启会打断他手头的事）
    """
    try:
        if not bool(_si.load_settings().get("auto_restart_after_iter", True)):
            return False
    except Exception:                                   # noqa: BLE001
        logger.debug("读 auto_restart_after_iter 失败（按默认允许）", exc_info=True)
    return str(source or "").strip().lower() in _AUTO_RESTART_SOURCES


def _auto_restart(reason="", tid=""):
    """后台延迟重启，让改动生效。

    安全闸：先 verify_import，导入都过不去就**绝不重启**（否则应用直接起不来），
    只在界面报错等人工处理。

    ★ 2026-11-20 用户要求：「如果需要自动重启时有正在跑的内容，要等当前任务
      完成之后再自动重启，重启之后立即进行验证」。
      所以这里分两步：
        ① 调用方（_worker 的 finally）保证**本任务已经跑完**才进来；
        ② 进来后再确认没有别的任务在跑 —— 有就等它结束，绝不在别人改代码
           改到一半时把进程换掉（那会留下改了一半的文件）。
    """
    def _do():
        time.sleep(_RESTART_DELAY)
        # ① 先把**自己**的 running 清掉（任务已结束，只是状态还没回收）
        with _LOCK:
            run = _STATE.get("running")
            if run and (not tid or run.get("tid") == tid):
                _STATE["running"] = None
        _save()
        # ② 若还有别的任务在跑，等它跑完再重启（最多等一个任务超时）
        deadline = time.time() + max(60, int(task_timeout_sec() or 1800))
        waited = False
        while time.time() < deadline:
            with _LOCK:
                run = _STATE.get("running")
                if not _run_alive(run):
                    break
            if not waited:
                _emit_sys("还有任务在跑，等它跑完再自动重启（不打断当前工作）…",
                          state="restarting")
                waited = True
            time.sleep(2)
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
            # ★ 2026-11-20 用户要求「重启时要能继续任务或验证」：
            #   重启这一步没做成，verify 就永远没人消费 —— 整条链停在这里，
            #   队列里排着的任务也不会再动。所以退一步：**本进程内直接验证**
            #   （改动已经在磁盘上，验证是有意义的），过了就接着跑队列。
            try:
                if _start_verify(wait_busy=False):
                    _emit_sys("改为在本进程内直接验证（不用等重启）…",
                              state="running")
            except Exception:                           # noqa: BLE001
                logger.debug("验证兜底也没起来", exc_info=True)
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
    # ★ 2026-11-22 用户要求「反复验证完全没问题之后才显示已验证」：
    #   一轮通过不算数，必须**连续** _VERIFY_CONFIRM 轮全过。
    #   任何一轮不过就立刻回去修（streak 归零，修完从第 1 轮重新数）。
    need = _VERIFY_CONFIRM
    streak = int(v.get("streak") or 0)
    ok, problems = False, []
    while streak < need:
        _emit(tid, "step",
              text="验证中（连续第 %d/%d 轮）：%d 个改动文件的语法 + 代码自检 + 回归…"
                   % (streak + 1, need, len(files)))
        try:
            _ok, _problems = _verify_once(files)
        except Exception as e:                          # noqa: BLE001
            _ok, _problems = False, ["验证器自己崩了：%s: %s"
                                     % (type(e).__name__, e)]
        if not _ok:
            v["streak"] = 0
            ok, problems = False, _problems
            break
        streak += 1
        v["streak"] = streak
        ok, problems = True, []
        with _LOCK:
            _STATE["verify"] = v
        if streak < need:
            _emit(tid, "step",
                  text="第 %d 轮通过，再确认一轮（连续 %d/%d）…"
                       % (streak, streak, need))
            time.sleep(1.0)

    md = _report_md(v, ok, problems)

    if ok:
        # ★ 2026-11-22 用户要求：「除了这些还有进行跑分以及反思还有日志记录」。
        #   连续验证全过只说明「没改坏」，还要**真跑一次评分**跟上一版比：
        #   低于原来 → scoregate 记低分日志 + 沉淀教训 + 起自动修复（反思改进）。
        _emit(tid, "step", text="连续 %d 轮验证全过，开始跑分并与上一版比对…" % need)
        _sc = _score_after_verify(tid, v)
        md = md + "\n" + _score_md(_sc)
        _log_verify("verify_pass", ok=True,
                    summary="%s 连续 %d 轮验证通过" % (v.get("title") or "", need))
        if _sc.get("ran"):
            _log_verify("score", ok=(str(_sc.get("verdict") or "") != "regressed"),
                        summary="%s 跑分 %s → %s（%s）" % (
                            v.get("title") or "", _sc.get("prev"), _sc.get("total"),
                            _VERDICT_CN.get(str(_sc.get("verdict") or ""), "")))
        if str(_sc.get("verdict") or "") == "regressed":
            _log_verify("reflect", ok=False,
                        summary="分数低于上一版 %s：已记低分日志、沉淀教训并启动反思修复"
                                % _sc.get("delta"))
            _emit(tid, "step",
                  text="⚠️ 分数比上一版低 %+.2f（%s → %s）：已记低分日志、"
                       "沉淀教训并启动反思修复" % (
                           float(_sc.get("delta") or 0),
                           _sc.get("prev"), _sc.get("total")))
        else:
            _emit(tid, "step", text="跑分：%s（%s）" % (
                _sc.get("total") if _sc.get("total") is not None else "没跑成",
                _VERDICT_CN.get(str(_sc.get("verdict") or ""),
                                _sc.get("msg") or "无对比")))
        # ★ 2026-11-23 用户要求：「跑分不能低于上个版本，不然还得重新改进」。
        #   退步 = **验收不通过**：绝不显示「✅ 已验证」，也不 _next() 接下一个
        #   （否则修复和下一个任务并行改代码，互相打架）。
        #   走的是和「验证没过」同一条修复链：生成修复计划 → submit(auto)
        #   → 自动重启 → 重新验证 → 重新跑分，直到涨回去才算完成。
        if str(_sc.get("verdict") or "") == "regressed":
            with _LOCK:
                _STATE["verify"] = None
                _STATE["fail_chain"] = int(_STATE.get("fail_chain") or 0) + 1
                _fc = _STATE["fail_chain"]
                _STATE["reports"].insert(0, {"tid": tid, "title": v.get("title") or "",
                                             "md": md, "ok": False, "at": _now()})
                _STATE["reports"] = _STATE["reports"][:30]
            _emit(tid, "status",
                  text="⚠️ 分数比上一版低 %+.2f（%s → %s）：验收不通过，"
                       "不显示「已验证」，自动生成修复任务重新改进（连续 %d/%d）"
                       % (float(_sc.get("delta") or 0), _sc.get("prev"),
                          _sc.get("total"), _fc, _MAX_FAIL_CHAIN),
                  state="error")
            _emit(tid, "report", text=md, title=v.get("title") or "")
            _save()
            if _fc >= _MAX_FAIL_CHAIN:
                with _LOCK:
                    _STATE["fail_chain"] = 0
                _emit_sys("「%s」连续 %d 次修复后分数仍未涨回去，"
                          "已停止自动修复，等你决定" % (v.get("title") or "", _fc),
                          state="error")
                return
            _emit(tid, "step", text="自动生成修复任务，把掉的分补回来…")
            _pid2 = ""
            try:
                _a2 = _si.run_audit(deep=False, source="manual", create_plan=True)
                _pid2 = str((_a2.get("plan") or {}).get("id") or "")
            except Exception as _e2:                     # noqa: BLE001
                _emit(tid, "step", text="生成修复计划失败：%s: %s"
                      % (type(_e2).__name__, _e2))
            if _pid2:
                submit(_pid2, source="auto",
                       title="把分数补回来「%s」（第 %d 次，%s → %s）"
                             % (v.get("title") or "", _fc,
                                _sc.get("prev"), _sc.get("total")))
            else:
                _emit_sys("分数退步但没能自动生成修复计划，请手动处理",
                          state="error")
            return

        with _LOCK:
            _STATE["verify"] = None
            # ★ 验证通过 ⇒ 连续失败链清零，护栏重新计数
            _STATE["fail_chain"] = 0
            _STATE["reports"].insert(0, {"tid": tid, "title": v.get("title") or "",
                                         "md": md, "ok": True, "at": _now()})
            _STATE["reports"] = _STATE["reports"][:30]
            _STATE["budget"] = int(_STATE.get("budget") or 0) + 1
            done_n = _STATE["budget"]
            qn = len(_STATE["queue"])
        _emit(tid, "status",
              text="✅ 任务完成（已验证）：%s（本轮第 %d 个）"
                   % (v.get("title") or "未命名功能", done_n),
              state="done", verified=True)
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
        # ★ 2026-11-23 用户要求：「队列空闲时一般如果已验证的话就结束了」。
        #   所以队列一空就收工，**不再**把接力棒交给推荐引擎自动生成新功能
        #   （原来会 request_next() 接着推荐，等于永不收尾）。
        #   推荐引擎自己的定时 tick 不受影响，它仍会按配额/冷却慢慢攒推荐，
        #   攒出来的待批准项等用户批准后才进入队列。
        _emit_sys("队列已空，本次迭代结束（已验证）", state="done")
        return

    # 没过 —— 先累加「跨计划连续失败链」（每轮修复都是新 plan_id，
    # v["tries"] 会归零，只有这个计数能兜住无限重启）
    with _LOCK:
        _STATE["fail_chain"] = int(_STATE.get("fail_chain") or 0) + 1
        _fc = _STATE["fail_chain"]
        _STATE["verify"] = None
    _save()
    _log_verify("verify_fail", ok=False,
                summary="%s 第 %d 轮验证没过（连续失败 %d/%d）：%s" % (
                    v.get("title") or "", int(v.get("tries") or 0), _fc,
                    _MAX_FAIL_CHAIN,
                    "；".join(str(x) for x in (problems or [])[:3]) or "见报告"))
    if _fc >= _MAX_FAIL_CHAIN:
        # 已经停手了，计数归零：否则之后任何无关任务失败一次就会被误拦
        with _LOCK:
            _STATE["fail_chain"] = 0
        _emit(tid, "status",
              text="⚠️ 「%s」连续 %d 次修复后验证仍未通过，已停止自动修复，等你决定"
                   "（不再自动重启，避免无限循环）"
                   % (v.get("title") or "", _fc),
              state="error")
        _emit(tid, "report", text=md, title=v.get("title") or "")
        _save()
        _next()
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
    orphan = None
    with _LOCK:
        v = _STATE.get("verify")
        # ★ 2026-11-20：先回收「孤儿」—— 重启前在跑、但线程已随旧进程消失的任务。
        #   它的 started_at 早于本进程启动时间，不可能还活着。
        run = _STATE.get("running")
        if run and float(run.get("started_at") or 0) < _BOOT_AT - 0.5:
            orphan = run
            _STATE["running"] = None
            # ★ 已结束的任务只是没清占位（history 里有它）→ 直接丢掉，别重跑一遍
            _done = any((h.get("tid") or "") == (run.get("tid") or "")
                        for h in (_STATE.get("history") or []))
            if _done:
                logger.warning("孤儿任务其实早已结束（history 里有记录），不再重跑：%s",
                               run.get("plan_id"))
                orphan = dict(orphan, finished=True)
            else:
                # 真没跑完：放回队首，重启后接着把它跑完 —— 不丢进度
                _STATE["queue"].insert(0, {"tid": run.get("tid"),
                                           "plan_id": run.get("plan_id"),
                                           "source": run.get("source") or "manual",
                                           "title": run.get("title") or "",
                                           "max_n": run.get("max_n") or 0,
                                           "at": run.get("started_at") or time.time(),
                                           "brief": run.get("brief") or {},
                                           "resumed": True})
        qn = len(_STATE["queue"])
        first = (_STATE["queue"][0] if _STATE["queue"] else None)
        # ★ 2026-11-20：这两秒半里自迭代可能已经 submit 并开跑了。
        #   那种情况下**绝不能**再 _next()（会顶掉正在跑的，见 _next 的注释）。
        busy = bool(_STATE.get("running"))

    # ★ 2026-11-20 用户：「防止任务内容因中断补充重启后丢失」——
    #   重启后逐个确认计划文件还在。计划被删 / plans.json 损坏时，队列项
    #   只剩一个死引用；这里标 plan_missing，并保留 brief 里的任务内容，
    #   让用户能看到「原本要做什么」，而不是卡片上一片空白、任务静默消失。
    #   放在锁外做：读 plans.json 可能慢，别占着 _LOCK 卡住执行。
    _missing = 0
    for _x in (_STATE.get("queue") or []):
        _pid = str(_x.get("plan_id") or "")
        if not _pid or _x.get("plan_missing"):
            continue
        try:
            _r = _si.get_plan(_pid)
            if not (_r or {}).get("ok"):
                _x["plan_missing"] = True
                _missing += 1
        except Exception:                               # noqa: BLE001
            logger.debug("检查计划是否还在失败：%s", _pid, exc_info=True)
    if _missing:
        _emit_sys("队列里有 %d 个任务找不到对应的计划文件（可能被删了），"
                  "任务内容已保留在卡片上，可以取消后重新提交" % _missing,
                  state="waiting")
    # ★ 2026-11-22 用户报：「没有自动拉起任务啊」——
    #   上一版这里把「没有已批准动作」的任务**直接剔出队列**，结果重启后
    #   队列被清空、任务静默消失，用户看到的就是「没拉起」。
    #   改为：**保留任务**，只打 need_approve 标记并说明卡在哪；
    #   任务照常被 _next() 拉起，卡片上显示「等待批准」，点批准就能接着跑。
    _need = 0
    for _x in (_STATE.get("queue") or []):
        if str(_x.get("source") or "") == "auto" or _x.get("plan_missing"):
            continue
        try:
            _r2 = _si.get_plan(str(_x.get("plan_id") or ""))
            _a2 = [a for a in (((_r2 or {}).get("plan") or {}).get("actions") or [])
                   if isinstance(a, dict)]
            if _a2 and not any(a.get("status") == "approved" for a in _a2):
                _x["need_approve"] = sum(1 for a in _a2
                                         if a.get("status") not in ("done", "skipped"))
                _need += 1
            else:
                _x.pop("need_approve", None)
        except Exception:                               # noqa: BLE001
            logger.debug("恢复时检查已批准动作失败（不标记，交给执行器）", exc_info=True)
    if _need:
        _save()
        _emit_sys("队列里有 %d 个任务还没有已批准的动作 —— 任务已保留，"
                  "在卡片上点「批准」即可让它接着跑" % _need, state="waiting")
    if orphan:
        _save()
        _name = orphan.get("title") or orphan.get("plan_id") or "未命名任务"
        if orphan.get("finished"):
            _emit(orphan.get("tid") or "_sys", "status",
                  text="「%s」上次已经跑完，只是占位没清，已回收" % _name,
                  state="done")
        else:
            _emit(orphan.get("tid") or "_sys", "status",
                  text="上次重启时「%s」还没跑完，已放回队列接着做" % _name,
                  state="running")
    if v:
        # ★ 2026-11-23 用户要求「重启后卡片自动弹出来」：这条提示原来走 _emit_sys，
        #   会挂到**上一个任务**的 tid 上（history[0]），用户看到的是一张旧卡片上多一行字，
        #   新任务卡片迟迟不出现。改成挂到 verify 自己的 tid —— 前端 feedEnsure(tid)
        #   会立刻把这张卡片建出来，验证过程也接在同一张卡上。
        _emit(v.get("tid") or "_sys", "status",
              text="重启后恢复：先验证上一个功能「%s」，之后接着跑队列（还有 %d 个）"
                   % (v.get("title") or "", qn), state="running")
        # ★ 2026-11-20：走 _start_verify()，它会「有任务在跑就先等它跑完」
        #   再验证 —— 原来直接 _verify_worker()，会和正在跑的任务并发改/验同一批文件。
        if not _start_verify(wait_busy=True):
            _emit_sys("验证线程已经在跑了，不重复启动", state="running")
        return
    if busy:
        # 已经在跑了：只把队列情况说出来，不碰 running
        _emit_sys("重启后恢复：已有任务在跑，队列里还有 %d 个等待，会依次接着跑" % qn,
                  state="running")
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
        busy = bool(_STATE.get("running"))
    _save()
    # 已经有任务在跑就只清计数 —— 不能再起一个（同 _next 的双 worker 问题）
    if qn and not busy:
        _next()
    return {"ok": True, "queue_len": qn, "running": busy}


# ★ 2026-11-23：只读进程（诊断脚本、静态检查、回归子进程）import 本模块时
#   不要加载状态 —— `_load()` 会把 running 当孤儿清掉，之后任何一次 `_save()`
#   就会把这个「清掉」写回磁盘，正在跑的任务记录就没了。
#   实测：一条正在跑的任务被诊断进程 import 后从 task.json 里消失了。
#   诊断请设 MM_ITER_READONLY=1（配合 _save 的守卫，全程不碰磁盘）。
if os.environ.get("MM_ITER_READONLY") != "1":
    _load()

