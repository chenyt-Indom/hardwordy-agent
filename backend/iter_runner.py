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
# ★ 2026-11-23 修：think 事件单独限流。
#   实测 events 里 think 占 66%（2000 条里 1330 条），只覆盖 3.3 小时 ——
#   status / phantom_reject / code 这些**关键事件**被思考碎片挤出了窗口，
#   等于「记录细节」这件事根本没做到。现在 think 最多留 _MAX_THINK 条，
#   其余事件仍按 _MAX_EVENTS 保留，总上限 _MAX_EVENTS + _MAX_THINK。
_MAX_THINK = 600
_EV_HARD_CAP = _MAX_EVENTS + _MAX_THINK


def _trim_events():
    """按 kind 分别限流：think 只留最近 _MAX_THINK 条，总量不超过硬上限。

    必须在 _LOCK 内调用（直接改 _STATE["events"]）。
    """
    evs = _STATE.get("events") or []
    n_think = 0
    for e in evs:
        if isinstance(e, dict) and e.get("kind") == "think":
            n_think += 1
    if n_think > _MAX_THINK:
        drop = n_think - _MAX_THINK
        kept = []
        for e in evs:
            if drop > 0 and isinstance(e, dict) and e.get("kind") == "think":
                drop -= 1
                continue
            kept.append(e)
        evs = kept
    if len(evs) > _EV_HARD_CAP:
        evs = evs[-_EV_HARD_CAP:]
    _STATE["events"] = evs

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
    # ★ 2026-11-23 回合收尾强制证据检查（见文件下方「回合收尾证据」一节）：
    #   regress          = 本轮**真跑过**的回归结果（唯一的通过率证据来源）
    #   regress_round_at = 本轮起点；早于它的回归记录不算「本轮的」
    #   last_closeout    = 最近一次收尾检查的完整结果（前端/诊断可直接读）
    "regress": None,
    "regress_round_at": 0.0,
    "last_closeout": None,
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
# ★ 2026-11-23：撞执行锁最多重试几次。原来没有上限，只要那条执行卡着，
#   就会每 20 秒重试一次、永远不停（实测无限循环）。
_MAX_LOCK_RETRY = 3

# ★ 2026-11-23 本轮需求：**单次改动的文件数上限，从「无限制」降到 3 个**。
#   它和「跑分退步」是同一个根因的两面：
#     · 一次改十几个文件时，分数掉了也归因不到具体是哪个文件弄的；
#     · 出问题要整轮回滚，代价随文件数线性放大；
#     · 本轮新增的静态扣分点散落在多个文件里，评分前的自动修更容易漏。
#   收紧到 3 个之后，「一次改动」小到能被验证、被归因、被回滚。
#   ⚠️ 这只是**本轮改动集**的上限，不是「验证只看 3 个文件」：超出的文件照样
#   做语法/自检（见 _verify_once），但会被明确记成「超上限，留给下一轮」，
#   写进事件与遗留清单 —— 绝不静默丢掉（静默丢文件正是本项目反复踩的坑）。
_MAX_CHANGE_FILES = 3

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
# ★ 2026-11-23 修（用户报「每 20 秒重复一次重试、状态和实际执行对不上」）：
#   原来 _LIVE 是 tid -> 单个线程对象，而 _next() 从队列提任务起的 worker
#   **根本没登记** —— 于是那个任务一过 20 秒宽限就被 _run_alive 判成僵尸：
#   清 running、退回队列、再起一个 worker → 撞执行锁 → 又退回队列 → 20 秒后
#   再来一次，无限空转；而真正在跑的老线程没人知道（卡片不显示、终止也停不了）。
#   现在：① 一个 tid 可以登记多个线程；② 额外判「本进程还有没有任何活着的
#   iter worker」——只要还有一个活的，就绝不许把 running 判死。
_LIVE = {}
_LIVE_LOCK = threading.Lock()

# ★ 2026-11-23：用户点了终止，但任务卡在「等待重试」时 running 槽位是空的，
#   should_cancel() 只看 running.cancel 会返回 False —— 那个 worker 还在跑，
#   按钮就等于失效。这里按 tid 记一份终止意图，worker 退出时清掉。
_cancel_tids = set()


def _live_add(tid, th):
    """登记一个 worker 线程（同一 tid 允许多个，绝不互相覆盖）。"""
    tid = str(tid or "")
    if not tid or th is None:
        return
    with _LIVE_LOCK:
        _LIVE.setdefault(tid, []).append(th)


def _live_drop(tid, th=None):
    """注销登记：给了 th 只摘掉这一个，不给就整个 tid 清空。"""
    tid = str(tid or "")
    with _LIVE_LOCK:
        if th is None:
            _LIVE.pop(tid, None)
            return
        lst = [x for x in (_LIVE.get(tid) or []) if x is not th]
        if lst:
            _LIVE[tid] = lst
        else:
            _LIVE.pop(tid, None)


def _live_alive(tid):
    """这个 tid 是否还有活着的 worker。"""
    with _LIVE_LOCK:
        lst = list(_LIVE.get(str(tid or "")) or [])
    for th in lst:
        try:
            if th.is_alive():
                return True
        except Exception:                               # noqa: BLE001
            continue
    return False


def _live_any():
    """本进程还有没有任何活着的 iter worker（不管 tid 对不对）。"""
    with _LIVE_LOCK:
        all_th = [th for lst in _LIVE.values() for th in lst]
    for th in all_th:
        try:
            if th.is_alive():
                return True
        except Exception:                               # noqa: BLE001
            continue
    return False


def _run_alive(run) -> bool:
    """running 里的那个任务，它的 worker 线程是否真的还活着（不是僵尸）。

    判僵尸的三种情况：
      ① 没有 running 记录 —— 本来就没任务；
      ② started_at 早于本进程启动时间 —— 上一个进程留下的孤儿；
      ③ 本进程没登记过它的 worker，或线程已 not is_alive() —— 线程死了但记录没清。
    """
    if not run:
        return False
    tid = str(run.get("tid") or "")
    # ① 本进程登记过这个 tid 的 worker，且它还活着 → 铁定活着。
    #    ★ 必须排在孤儿判断**之前**：本进程起的线程不可能是「上个进程的孤儿」，
    #    先判孤儿会把一个已经跑了半小时的任务误杀（实测：这是空转的另一条触发路径）。
    if _live_alive(tid):
        return True
    # ② 本进程还有别的活 worker → 确实有执行在进行。宁可让 running 多留一会儿，
    #    也不要回收后再起一个去撞执行锁（那正是「每 20 秒重试一次」的来源）。
    if _live_any():
        return True
    # ③ 一个活线程都没有：started_at 早于本进程启动时间 ⇒ 上个进程留下的孤儿。
    try:
        if float(run.get("started_at") or 0) < _BOOT_AT - 0.5:
            return False
    except (TypeError, ValueError):
        return False
    # ④ 刚 submit、还没登记完的极短窗口给 20 秒宽限，超过就认定线程已经没了。
    try:
        return (time.time() - float(run.get("started_at") or 0)) < 20
    except (TypeError, ValueError):
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
                _STATE["events"] = list(d.get("events") or [])[-_EV_HARD_CAP:]
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
                                     # ★ 2026-11-23 用户要求「记得当前工作干到哪」：
                                     #   cur_action 必须一起带进队列 —— 否则重启
                                     #   瞬间就丢，恢复提示只能说「在跑某任务」，
                                     #   说不出「停在第 4/9 条」。
                                     "cur_action": run.get("cur_action") or {},
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
                 "events": _STATE["events"][-_EV_HARD_CAP:],
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


# ★ 2026-11-23 用户要求「非正常关闭重启之后要记得当前工作干到哪里了」：
#   原来 _emit **只写内存**，只有 submit / cancel / _finish 才 _save()。
#   实测踩到：任务 01:12:10 开始跑，跑到 01:37 干了 25 分钟活，磁盘上
#   task.json 的 mtime 还是 01:12:10 —— 这中间被硬杀 / 关窗口 / 断电，
#   进度全丢，重启后连「做到第几条」都不知道。
#   现在每 _SAVE_MIN_GAP 秒最多落盘一次（事件 2000 条上限，写一次几百 KB，
#   2 秒一次的代价可以接受），关键事件用 _force_save=True 立刻落盘。
_LAST_SAVE = [0.0]
_SAVE_MIN_GAP = 2.0


def _emit(tid, kind, **kw):
    """追加一条事件，返回它的 seq。

    _force_save=True 时立刻落盘（终止、恢复、任务完成这类关键节点用）。
    """
    force = bool(kw.pop("_force_save", False))
    with _LOCK:
        _STATE["seq"] += 1
        ev = {"seq": _STATE["seq"], "tid": tid, "kind": kind,
              "ts": time.time(), "t": time.strftime("%H:%M:%S")}
        ev.update(kw)
        _STATE["events"].append(ev)
        _trim_events()
        seq = ev["seq"]
        due = force or (time.time() - _LAST_SAVE[0] >= _SAVE_MIN_GAP)
        if due:
            _LAST_SAVE[0] = time.time()
    if due:
        _save()                                     # 锁外写盘，别占着 _LOCK 做 IO
    return seq


# ------------------------------------------------------------------ 查询

def _clip_change_files(files, limit=None):
    """按上限裁「单次改动的文件集」。返回 (kept, dropped)。

    去重 + 保序（先出现的优先保留）；limit 不传就用 _MAX_CHANGE_FILES。
    纯函数，不落盘、不发事件 —— 便于测试与复用。
    """
    lim = int(limit if limit is not None else _MAX_CHANGE_FILES)
    if lim <= 0:
        lim = _MAX_CHANGE_FILES
    kept, dropped, seen = [], [], set()
    for f in (files or []):
        s = str(f or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        (kept if len(kept) < lim else dropped).append(s)
    return kept, dropped


def change_scope(files, tid="", where="verify", notify=True):
    """把本轮改动集按上限裁一遍，并把「超上限」**说出来**。

    返回 {"files": kept, "dropped": [...], "all_n": N, "limit": 3}。
    超出的文件不进「本轮改动集」（不参与评分/验证的归因），但会：
      ① 发一条 step 事件点名，界面上看得见；
      ② 由调用方写进遗留清单，下一轮接着改。
    静默丢弃是绝对不允许的 —— 用户会以为「都改完了」。
    """
    kept, dropped = _clip_change_files(files)
    all_n = len(kept) + len(dropped)
    if dropped and notify:
        try:
            _emit(str(tid or "_sys"), "step",
                  text="单次改动文件数超上限（%d > %d）：本轮只认 %s；"
                       "超出的 %s 不静默丢弃，记进遗留清单下一轮再改"
                       % (all_n, _MAX_CHANGE_FILES,
                          "、".join(kept) or "无",
                          "、".join(dropped[:6])
                          + ("…" if len(dropped) > 6 else "")))
        except Exception:                                # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    return {"files": kept, "dropped": dropped, "all_n": all_n,
            "limit": _MAX_CHANGE_FILES}


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
    # ★ 2026-11-23 本轮需求：单次改动文件数上限从「无限制」（原来是 [:20]，
    #   实际等于不限）降到 3 个。超出的进 files_over_cap，由收尾写进遗留清单，
    #   绝不静默丢掉。直接执行路径和队列路径用同一套裁剪。
    _scope = change_scope(files, tid=tid, where="mark_verify")
    files = _scope["files"]
    with _LOCK:
        prev = _STATE.get("verify") or {}
        rec = {
            "tid": tid or ("direct_%s" % (pid or int(time.time()))),
            "plan_id": pid,
            "title": str(title or "")[:120],
            "files": files,
            "files_all": _scope["all_n"],
            "files_over_cap": _scope["dropped"],
            "files_limit": _MAX_CHANGE_FILES,
            # 同一功能反复修时轮次要累加，不能每轮清零
            "tries": int(prev.get("tries") or 0) if prev.get("plan_id") == pid else 0,
            "at": time.time(),
            "source": str(source or "direct"),
        }
        _STATE["verify"] = rec
    _save()
    try:
        _emit(rec["tid"], "status",
              text="已登记待验证：%s（%d 个文件%s）—— 重启后自动验证、跑分"
                   % (rec["title"] or pid or "未命名", len(rec["files"]),
                      "，单次改动上限 %d、超出的 %d 个顺延到下一轮"
                      % (_MAX_CHANGE_FILES, len(_scope["dropped"]))
                      if _scope["dropped"] else ""),
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
    _live_add(tid, th)
    th.start()
    return {"ok": True, "state": "running", "tid": tid, "ahead": 0}


def cancel(tid=""):
    """请求终止。当前动作会跑完（含校验/回滚）再停，不会留下改了一半的文件。

    ★ 2026-11-23 用户报「终止按钮一会有一会没有」并追问「随时都能手动立刻终止」：
      原来这里只处理「正在跑」的任务 —— 而「等待重试（另一个执行正在进行）」时
      `_STATE["running"]` 已被置 None、任务退回了 queue（见 _worker 里的 requeued 分支），
      此时点终止只会回一句「现在没有任务在跑」，等于按钮是假的。
      现在：正在跑 → 置 cancel 标志；还在队列里 → 直接摘掉并标记 cancelled。
    """
    tid = str(tid or "").strip()
    with _LOCK:
        run = _STATE["running"]
        # ① 正在跑、且 tid 对得上（tid 为空＝停当前那个）→ 现有逻辑
        if run and (not tid or str(tid) == str(run["tid"])):
            run["cancel"] = True
            run["state"] = "cancelling"
            _cancel_tids.add(str(run["tid"]))
            _emit(run["tid"], "status", text="正在终止（当前动作做完就停）…",
                  state="cancelling")
            _save()
            return {"ok": True, "tid": run["tid"], "state": "cancelling"}

        # ② 这张卡不在「正在跑」的槽位上 —— 多半在等待队列里（等待重试 / 排队）
        q = _STATE["queue"]
        hit = [x for x in q if str(x.get("tid")) == tid]
        if hit:
            _cancel_tids.add(tid)
            _STATE["queue"] = [x for x in q if str(x.get("tid")) != tid]
            _emit(tid, "status", text="已终止（从等待队列移除，不会再跑）",
                  state="cancelled")
            _save()
            return {"ok": True, "tid": tid, "state": "cancelled",
                    "removed": len(hit)}

        if not run:
            return {"ok": False, "error": "现在没有任务在跑（可能已经结束）"}
        return {"ok": False, "error": "任务 id 对不上（当前在跑的是 %s）" % run["tid"]}


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
    # ★ 2026-11-23：execute_approved 内部已经按来源排程过重启时置 True。
    #   两处都排程 = 连续断两次（用户看到「又莫名其妙重启了」）。
    _exec_scheduled_restart = False
    # ★ 2026-11-20：撞到「另一个执行占着锁」时置 True —— 把任务放回队首稍后重试，
    #   而不是报一张「没有可执行的动作」的假失败卡。
    requeued = False
    # ★ 2026-11-23 本轮需求：单次改动上限 3 个文件，超出的那几个记在这里，
    #   收尾时写进遗留清单（本轮判未闭环 → 下一轮接着改）。
    _over_cap = []

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
        # ★ 2026-11-23：除了 running.cancel，还要认「按 tid 记下的终止意图」——
        #   任务处于「等待重试」时 running 槽位是空的，光看它会返回 False，
        #   用户在界面上点了终止也停不下来。
        if str(tid) in _cancel_tids:
            return True
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
        # ★ 2026-11-23 回合收尾强制证据检查：新的一轮开始，上一轮跑过的回归
        #   结果不再算本轮的证据（否则收尾会拿旧数字充数）。
        _round_begin("task:%s" % str(item.get("title") or plan_id or "")[:60])
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
                            # ★ 2026-11-23 用户要求「重启后记得干到哪」：
                            #   光有 title 只能说「在做哪条」，说不出「第几条 / 共几条」。
                            #   这里把序号也记进 running 并落盘，重启后就能报
                            #   「上次停在 第 3/9 条：xxx，从这里继续」。
                            _STATE["running"]["cur_action"] = {
                                "index": kw.get("index"),
                                "total": kw.get("total"),
                                "title": _t[:60],
                                "action_id": str(kw.get("action_id") or ""),
                                # ★ 2026-11-23：声明要动的文件 + 执行前不存在的
                                #   （＝声称要新建的），收工时用来核对是否真的落地
                                "files": list(kw.get("files") or [])[:20],
                                "new_files": list(kw.get("new_files") or [])[:20]}
                            _STATE["running"]["beat_at"] = time.time()
                    try:
                        _stage("action_start",
                               "第 %s/%s 条：%s" % (kw.get("index") or "?",
                                                   kw.get("total") or "?", _t))
                    except Exception:                    # noqa: BLE001
                        logger.debug("写阶段流水失败（不影响迭代）", exc_info=True)
                    _emit(tid, "status", text="正在迭代：%s" % _t[:60],
                          state="running")

        # 把队列来源透传下去：auto/featradar/approved 自动重启，
        # manual（用户自己拉起的）不重启
        r = _si.execute_approved(plan_id, on_event=_on_event,
                                 should_cancel=should_cancel,
                                 origin=str(item.get("source") or "auto"))
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
            # ★ 2026-11-23：① 用户已点终止 → 绝不放回队列（否则它会自己跑回来）
            if str(tid) in _cancel_tids:
                _emit(tid, "status",
                      text="已终止（等待重试期间被手动终止，不会再跑）",
                      state="cancelled", _force_save=True)
                _emit_closeout(
                    tid, title=_cur_title or str(item.get("title") or ""),
                    plan_id=plan_id,
                    header="**本轮迭代收尾**：用户在等待重试期间终止",
                    problems=["用户手动终止：动作没跑完"],
                    allow_run=False)
                _finish(tid, "cancelled", ok_n, fail_n, skip_n, changed,
                        "用户手动终止（撞执行锁后、重试前被终止）")
                return
            # ② 重试次数上限：原来没有上限，只要那条执行卡着就每 20 秒重试一次、
            #    永远不停（实测无限循环）。超限就明确失败并说明原因，交给人判断。
            _try = int(item.get("_retry") or 0) + 1
            item["_retry"] = _try
            if _try > _MAX_LOCK_RETRY:
                _emit(tid, "error",
                      text="连续 %d 次撞到「另一个执行正在进行」，已停止重试："
                           "那条执行可能卡住了，确认后请手动重新提交" % (_try - 1))
                _emit_closeout(
                    tid, title=_cur_title or str(item.get("title") or ""),
                    plan_id=plan_id,
                    header="**本轮迭代收尾**：撞执行锁 %d 次，已停止重试"
                           % (_try - 1),
                    problems=["撞到「另一个执行正在进行」%d 次：本轮根本没跑起来"
                              % (_try - 1)],
                    allow_run=False)
                _finish(tid, "failed", ok_n, fail_n, skip_n, changed,
                        "撞执行锁重试 %d 次仍失败（另一条执行一直没释放）" % (_try - 1))
                return
            with _LOCK:
                _STATE["queue"].insert(0, item)
                _STATE["running"] = None
            _save()
            _emit(tid, "step",
                  text="另一个执行正在进行，本任务已放回队列，%d 秒后自动重试"
                       "（第 %d/%d 次）…" % (_LOCK_RETRY_DELAY, _try, _MAX_LOCK_RETRY))
            _emit(tid, "status", text="等待重试（另一个执行正在进行）", state="waiting")
            threading.Timer(_LOCK_RETRY_DELAY, _next).start()
            requeued = True
            return

        cancelled = bool(r.get("cancelled"))
        # ★ 2026-11-23 用户要求「手动点终止可以不拉起，但需要记录情况」：
        #   原来终止只写 state=cancelled，history 里 error 是空的 ——
        #   过几天回看根本不知道当时做到哪、为什么停。
        _cancel_reason = ""
        if cancelled:
            with _LOCK:
                _ca = dict((_STATE.get("running") or {}).get("cur_action") or {})
            _where = ""
            if _ca.get("title"):
                _where = "，停在「第 %s/%s 条：%s」" % (
                    _ca.get("index") or "?", _ca.get("total") or "?", _ca.get("title"))
            _cancel_reason = ("用户手动终止（非异常）：已完成 %d 条，失败 %d 条，"
                              "剩余动作保持待批准%s" % (ok_n, fail_n, _where))
            _stage("blocked", "已终止（已完成 %d 条，剩余动作保持待批准）" % ok_n)
            _emit(tid, "status", text=_cancel_reason,
                  state="cancelled", _force_save=True)
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
            # ★ 2026-11-23 本轮需求：单次改动文件数上限 3 个。
            #   超出部分不进「本轮改动集」（评分/归因只看 ≤3 个），但会：
            #   ① 发事件点名；② 进下面的遗留清单 → 本轮判未闭环 → 下一轮接着改。
            _scope = change_scope(changed, tid=tid, where="worker")
            _over_cap = list(_scope["dropped"])
            _auto = True
            # 真正在做的那条动作名（action_start 已把 running.title 更新过）
            with _LOCK:
                _cur_title = (str((_STATE.get("running") or {}).get("title") or "").strip()
                              or str(item.get("title") or "").strip())
            _auto = _should_auto_restart(item.get("source"))
            # execute_approved 里已经按来源排程过重启（auto 来源才会）→ 记下来，
            # finally 里不再排第二次
            try:
                _exec_scheduled_restart = bool(
                    ((r or {}).get("loop") or {}).get("auto_restart", {})
                    .get("scheduled"))
            except Exception:                            # noqa: BLE001
                _exec_scheduled_restart = False
            if _auto:
                with _LOCK:
                    prev = _STATE.get("verify") or {}
                    _STATE["verify"] = {
                        "tid": tid, "plan_id": plan_id,
                        "title": _cur_title,
                        "files": _scope["files"],
                        "files_all": _scope["all_n"],
                        "files_over_cap": _over_cap,
                        "files_limit": _MAX_CHANGE_FILES,
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
                        "files": _scope["files"],
                        "files_all": _scope["all_n"],
                        "files_over_cap": _over_cap,
                        "files_limit": _MAX_CHANGE_FILES,
                        "tries": int(prev.get("tries") or 0)
                                 if prev.get("plan_id") == plan_id else 0,
                        "at": time.time(),
                    }
                _save()
                _emit(tid, "restart",
                      text="这次改了 %d 个文件，需要重启才生效 —— 你点「立即重启」后，"
                           "会自动接着验证、跑分、反思" % len(changed),
                      files=changed[:12], manual=True)

        # ★ 2026-11-23（教训：「没产生实际改动」×2）：
        #   模型可以「只说不做」—— 在回合里读文件、写结论，最后 diff 为空，
        #   却按 ok 收工，评分照跑、重启照触发，用户什么也没得到。
        #   这里做硬检查点：动作声称成功但**一个文件都没变** → 记 no_change、
        #   判失败、不进评分。这是「改完必须真有改动」的底线。
        if ok_n and not changed:
            try:
                from . import actlog as _al3
                _al3.record_stage(plan_id, "no_change",
                                  detail="动作声称成功但 diff 为空（no_change）")
            except Exception:                           # noqa: BLE001
                logger.debug("记 no_change 失败", exc_info=True)
            _emit(tid, "error",
                  text="这一轮没有任何文件改动（no_change）：模型只说了没做，判失败重来")
            ok_n = 0
            fail_n = fail_n or 1
            err_all = ((err_all or "") + " no_change：声称改完但 diff 为空").strip()

        # ★ 2026-11-23 修（记录造假）：动作声称「新建 X」，可 X 根本没生成，
        #   却按 ok 收工 —— 实测 a5「新增 backend/_feature_tests/test_pause_resume.py」
        #   记了 result=ok、changelog 还写 new_file=true，那个文件压根不存在。
        #   这里补一道硬检查：执行前不存在、又被声明为目标的新建文件，
        #   收工时必须真的在磁盘上；缺一个就判失败并记 no_change。
        if ok_n and not cancelled:
            _miss = []
            try:
                from . import self_iterate as _si4
                with _LOCK:
                    _ca3 = dict((_STATE.get("running") or {}).get("cur_action") or {})
                for _x in (_ca3.get("new_files") or []):
                    _rel = str(_x or "").strip()
                    if not _rel:
                        continue
                    if not (_si4._ROOT / _rel.replace("/", os.sep)).is_file():
                        _miss.append(_rel)
            except Exception:                           # noqa: BLE001
                logger.debug("校验新建文件失败（不影响迭代）", exc_info=True)
                _miss = []
            if _miss:
                try:
                    from . import actlog as _al4
                    _al4.record_stage(plan_id, "no_change",
                                      detail="声称新建但文件不存在：%s"
                                             % ", ".join(_miss[:5]))
                except Exception:                       # noqa: BLE001
                    logger.debug("记 no_change 失败", exc_info=True)
                _emit(tid, "error",
                      text="声称新建的文件没有生成（%s）：判失败，不算这一轮的成果"
                           % "、".join(_miss[:3]))
                ok_n = 0
                fail_n = fail_n or 1
                err_all = ((err_all or "") + " no_change：声称新建的文件不存在：%s"
                           % "、".join(_miss[:3])).strip()
        # ★ 2026-11-23 回合收尾强制证据检查（用户要求「不允许静默结束」）：
        #   在发这一轮的最终回复**之前**，强制查两项并写清楚：
        #     ① 本轮回归跑了没、通过率是不是 100%（没跑过就当场拉起一次）；
        #     ② 遗留清单（验证问题 / 失败动作 / 没做完的动作）为空 ——
        #        空也要明说「遗留：无」。
        #   任何一项没满足，回复最后一行就是「⚠️ 本轮未闭环：<原因>」。
        _extra = []
        if cancelled:
            _extra.append("用户手动终止：动作没跑完，本轮不算闭环")
        if ok_n and not changed:
            _extra.append("动作声称成功但一个文件都没改（no_change）")
        if requeued:
            _extra.append("撞到「另一个执行正在进行」，任务已放回队列等重试")
        if _over_cap:
            # ★ 单次改动文件数上限 3：超出的这几个**明写进遗留**，
            #   本轮因此判未闭环（下一轮接着改），而不是悄悄当成改完了。
            _extra.append("单次改动超上限（%d > %d）：%s 未纳入本轮，下一轮再改"
                          % (len(_over_cap) + _MAX_CHANGE_FILES, _MAX_CHANGE_FILES,
                             "、".join(_over_cap[:4])
                             + ("…" if len(_over_cap) > 4 else "")))
        _ttl = _cur_title or str(item.get("title") or "") or "未命名功能"
        _emit_closeout(
            tid, title=_ttl, plan_id=plan_id,
            header="**本轮迭代收尾**：%s（成功 %d 条，失败 %d 条%s）" % (
                _ttl, ok_n, fail_n, "，跳过 %d 条" % skip_n if skip_n else ""),
            problems=[err_all] if err_all else [],
            extra=_extra,
            # 只有真改了文件才值得当场跑一遍回归（约 10 秒）；没改动的一轮
            # 就如实记「本轮没跑过回归」→ 收尾照样判未闭环，不拿旧数字充数。
            allow_run=bool(changed))
        _finish(tid, "cancelled" if cancelled else ("done" if ok_n else "failed"),
                ok_n, fail_n, skip_n, changed,
                _cancel_reason or err_all)
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
        # ★ 2026-11-23 收尾强制证据检查：出错同样是「一轮结束」，照样要给出
        #   回归通过数 + 遗留清单，并以「⚠️ 本轮未闭环：…」结尾（不允许静默结束）。
        _emit_closeout(
            tid, title=_cur_title or str(item.get("title") or ""),
            plan_id=plan_id,
            header="**本轮迭代出错**：%s: %s" % (type(e).__name__, e),
            problems=["迭代出错：%s: %s" % (type(e).__name__, e)],
            extra=["代码可能在改到一半时中断，请人工确认后再重跑"],
            allow_run=False)
        _finish(tid, "error", ok_n, fail_n, skip_n, changed,
                "%s: %s" % (type(e).__name__, e))
    finally:
        # ★ 2026-11-22：注销本任务的存活登记。必须放在 _next()/_auto_restart()
        #   之前 —— 它们要用 _run_alive() 判「还有没有任务在跑」，
        #   注销晚了会让刚结束的任务被当成还活着，接下一个 / 重启都要白等。
        # 只摘掉「我自己这个线程」—— 同一个 tid 可能有另一个 worker 还在跑，
        # 整个 tid 清空会让它立刻被判成僵尸（这正是 11-23 空转的成因）。
        _live_drop(tid, threading.current_thread())
        if not requeued:
            # 任务回队列时**不能**清终止意图 —— 用户可能在「等待重试」期间点了终止，
            # 清掉的话重试时它又会自己跑起来。
            _cancel_tids.discard(str(tid))
        if requeued:
            # 已经放回队列、并挂了一个延迟的 _next —— 这里不能再接下一个，
            # 否则会立刻把同一个任务再捞起来，空转。
            pass
        elif auto_rst:
            # 走「重启 → 自动验证 → 再接下一个」这条链，
            # 这里**不能**调 _next()：否则下一个任务会在重启前抢跑，
            # 跑到一半进程被替换掉，进度就丢了。
            # ★ 2026-11-23：execute_approved 已经排程过就不再排第二次 ——
            #   两次都排 = 连着断两次，用户会以为又莫名重启了。
            if not _exec_scheduled_restart:
                _auto_restart(reason=_cur_title or item.get("title") or plan_id,
                              tid=tid)
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
        # ★ 2026-11-23 修（空转根因）：这里原来**没有**登记 —— 于是 _next 拉起的
        #   任务一过 20 秒宽限就被 _run_alive 判成僵尸，回收 → 重起 → 撞锁 → 退回
        #   队列 → 20 秒后再来，无限循环。先登记再 start。
        _live_add(nxt.get("tid"), th)
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
        # ★ 2026-11-23 回合收尾强制证据检查：真跑过的回归结果必须留痕。
        #   原来这里只把失败塞进 problems，**通过的数字直接丢了** —— 收尾时
        #   就答不出「本轮回归过了几条、通过率多少」，只能含糊地说「没问题」。
        _record_regress(rr, where="verify_once")
        if not rr.get("ok"):
            problems.append("回归失败 %d/%d 条：%s" % (
                int(rr.get("failed") or 0), int(rr.get("total") or 0),
                str(rr.get("summary") or "")[:300]))
    except Exception as e:                              # noqa: BLE001
        problems.append("回归没跑成：%s: %s" % (type(e).__name__, e))

    return (not problems), problems


# ------------------------------------------------------------------ 回合收尾证据
# ★ 2026-11-23 用户要求「回合收尾强制证据检查：回归通过数 + 遗留清单」。
#
# 要解决的不是「验证没跑」，而是**跑完了却静默结束**：一轮结束时的回复里
# 既没有「回归到底过了几条」，也没有「还剩什么事没做完」，用户没法判断
# 这一轮闭环没有，只能自己猜 —— 而猜错的代价是「以为改好了，其实还烂着」。
#
# 所以回合收尾**不允许静默结束**，在发这一轮最终回复之前强制检查两项：
#   ① 本轮是否**真跑过**回归，且通过率 100%（failed=0，且 passed>0）；
#   ② 遗留清单是否为空 —— **空也要明说「遗留：无」**，不能拿沉默表示清白；
# 任何一项没满足，就在最终回复的**最后一行**追加
# 「⚠️ 本轮未闭环：<原因>」（前端 mdToHtml 把它渲染成高亮块，见 app.js）。
#
# 用户备注还要求收尾时把**测试和 RSI 一起拉起来**：
#   · 测试：本轮没有回归证据时**当场拉起一次** quick 回归（run_disk，
#     在子进程里读磁盘上的新代码，约 10 秒），而不是拿上一轮的数字充数；
#   · RSI：当场采样一次并取「最该修的一项」，让「有没有改坏」（回归）和
#     「有没有变好」（RSI）出现在同一条收尾回复里，各说各话才算证据。
_CLOSEOUT_TAG = "⚠️ 本轮未闭环："
# 回归证据算不算「本轮的」：必须晚于本轮起点；再给半小时的宽容期
_CLOSEOUT_TTL = 1800.0
# 收尾回复里最多列几条遗留（完整清单还在任务详情里）
_CLOSEOUT_MAX_LEFT = 5


def _record_regress(rr, tid="", where="verify"):
    """把一次**真跑过**的回归结果记下来（回合收尾通过率的唯一来源）。

    只留结构化数字与失败用例名，不留 items 详情 —— task.json 已经很大，
    收尾要回答的只有「过了几条 / 共几条 / 谁没过」。
    """
    if not isinstance(rr, dict):
        return {}
    try:
        total = int(rr.get("total") or 0)
        passed = int(rr.get("passed") or 0)
        failed = int(rr.get("failed") or 0)
        skipped = int(rr.get("skipped") or 0)
    except (TypeError, ValueError):
        return {}
    rec = {"ok": bool(rr.get("ok")) and failed == 0,
           "total": total, "passed": passed, "failed": failed,
           "skipped": skipped,
           "scope": str(rr.get("scope") or "quick"),
           "seconds": rr.get("seconds"),
           "summary": str(rr.get("summary") or "")[:300],
           "failed_items": [str((x or {}).get("name") or "")
                            for x in (rr.get("failed_items") or [])
                            if isinstance(x, dict)][:5],
           "error": str(rr.get("error") or "")[:200],
           "tid": str(tid or ""), "where": str(where or ""),
           "at": time.time()}
    with _LOCK:
        _STATE["regress"] = rec
    return rec


def _round_begin(where=""):
    """标记「新一轮开始」：之前记的回归结果不再算本轮的证据。

    必须在新一轮动手之前调用，否则上一轮跑过的回归会被当成这一轮的通过率。
    """
    with _LOCK:
        _STATE["regress_round_at"] = time.time()
        _STATE["regress"] = None
    logger.debug("回合开始：%s", where or "-")


def _regress_evidence(tid="", allow_run=True):
    """取本轮回归证据；本轮没跑过就**当场拉起一次**（用户备注要求）。

    allow_run=False 时只读已有记录（例如这一轮压根没改文件，没必要白跑 10 秒，
    但会如实记成「本轮没跑过回归」→ 收尾判未闭环）。
    """
    now = time.time()
    with _LOCK:
        rec = dict(_STATE.get("regress") or {})
        t0 = float(_STATE.get("regress_round_at") or 0)
    if rec:
        _at = float(rec.get("at") or 0)
        if _at >= t0 - 1.0 and (now - _at) <= _CLOSEOUT_TTL:
            rec["fresh"] = True
            return rec
    if not allow_run:
        rec["fresh"] = False
        return rec
    try:
        from . import regress as _rgc
        runner = getattr(_rgc, "run_disk", None) or _rgc.run
        try:
            rr = runner(scope="quick", timeout=300)
        except TypeError:                               # 老签名兜底
            rr = runner(scope="quick")
    except Exception as e:                              # noqa: BLE001
        rr = {"ok": False, "total": 0, "passed": 0, "failed": 0,
              "summary": "回归没跑成：%s: %s" % (type(e).__name__, e)}
    rec = _record_regress(rr, tid=tid, where="closeout")
    rec["fresh"] = True
    return rec


def _regress_verdict(rec):
    """回归证据 → (是否 100% 通过, 一行展示文字, 未通过原因)。"""
    rec = rec or {}
    total = int(rec.get("total") or 0)
    passed = int(rec.get("passed") or 0)
    failed = int(rec.get("failed") or 0)
    skipped = int(rec.get("skipped") or 0)
    if total <= 0:
        why = str(rec.get("error") or rec.get("summary") or "").strip()
        return False, "回归：本轮没跑成（没有用例数，也就没有通过率证据）", \
            "本轮没跑过回归/没跑成%s" % ("：" + why[:120] if why else "")
    base = max(1, total - skipped)          # 跳过的用例不算进通过率分母
    rate = 100.0 * passed / base
    line = "回归 %d/%d 通过（%.0f%%%s）" % (
        passed, base, rate, "，跳过 %d 条" % skipped if skipped else "")
    if failed or passed < base or not rec.get("ok"):
        names = [n for n in (rec.get("failed_items") or []) if n]
        why = "回归通过率 %.0f%%（%d/%d），不是 100%%" % (rate, passed, base)
        if names:
            why += "；没过：%s" % "、".join(names[:3])
        return False, line + " —— 未全过", why
    return True, line, ""


def _leftover_items(plan_id="", problems=None, extra=None):
    """遗留清单：这一轮**本该做完却没做完**的事。空 = 真的没有遗留。"""
    out = []
    for p in (problems or []):
        s = str(p or "").strip()
        if s:
            out.append("验证没过：" + s[:160])
    acts = _plan_actions(plan_id) if plan_id else []
    for a in [x for x in acts if x.get("status") in ("failed", "error")][:3]:
        out.append("动作失败：%s%s" % (
            (a.get("title") or a.get("id") or "?")[:40],
            "（%s）" % a["error"] if a.get("error") else ""))
    undone = [x for x in acts
              if x.get("status") not in ("done", "ok", "skipped", "failed",
                                         "error")]
    if undone:
        out.append("计划里还有 %d 条动作没做完：%s" % (
            len(undone),
            "、".join((x.get("title") or x.get("id") or "?")[:24]
                     for x in undone[:3])))
    for x in (extra or []):
        s = str(x or "").strip()
        if s:
            out.append(s)
    seen, res = set(), []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        res.append(s)
    return res


def _closeout_check(tid="", plan_id="", problems=None, title="",
                    allow_run=True, extra=None):
    """回合收尾的**强制**证据检查。

    返回 {closed, tag, lines, reason, leftover, regress, rsi, at, ...}：
      · closed  —— 两项都满足（回归 100% 通过 + 遗留为空）
      · lines   —— 要拼进最终回复的证据行（含「遗留：无」的明说）
      · tag     —— 没闭环时的那一行「⚠️ 本轮未闭环：<原因>」，闭环时是空串
    调用方**必须**把 lines 拼进回复、把 tag 放在最后一行 —— 这是「不允许
    静默结束」的落地方式。
    """
    rec = _regress_evidence(tid=tid, allow_run=allow_run)
    r_ok, r_line, r_why = _regress_verdict(rec)
    left = _leftover_items(plan_id=plan_id, problems=problems, extra=extra)
    # 用户备注：收尾时把 RSI 一起拉起来（当场采样 + 最该修的一项）。
    # 同一分钟内重复采样会被 lessonloop 去重，不会刷爆账本。
    rsi = ""
    try:
        from . import lessonloop as _ll
        try:
            _ll.rsi_sample(force=True)
        except Exception:                               # noqa: BLE001
            logger.debug("RSI 采样失败（不影响收尾证据）", exc_info=True)
        rsi = str(_ll.rsi_line() or "").strip()
    except Exception:                                   # noqa: BLE001
        logger.debug("拉起 RSI 失败（不影响收尾证据）", exc_info=True)

    reasons = []
    if not r_ok:
        reasons.append(r_why)
    if left:
        reasons.append("遗留 %d 项未清（第一条：%s）" % (len(left), left[0][:80]))
    closed = not reasons
    lines = ["**本轮收尾证据**", "- " + r_line]
    lines.append("- 遗留：" + ("无" if not left else "%d 项" % len(left)))
    for i, s in enumerate(left[:_CLOSEOUT_MAX_LEFT], 1):
        lines.append("  %d. %s" % (i, s))
    if len(left) > _CLOSEOUT_MAX_LEFT:
        lines.append("  …还有 %d 项（完整清单见任务详情）"
                     % (len(left) - _CLOSEOUT_MAX_LEFT))
    if rsi:
        lines.append("- " + rsi)
    ev = {"closed": closed,
           "reason": "；".join(reasons),
           "tag": "" if closed else (_CLOSEOUT_TAG + "；".join(reasons)),
           "lines": lines, "leftover": left,
           "regress": rec, "regress_ok": r_ok, "rsi": rsi,
           "tid": str(tid or ""), "title": str(title or "")[:80],
           "at": time.time(),
           "at_text": time.strftime("%m-%d %H:%M:%S")}
    with _LOCK:
        _STATE["last_closeout"] = ev
    return ev


def _with_closeout(md, ev):
    """把收尾证据拼到这一轮最终回复的末尾；未闭环时最后一行是那条警示。"""
    body = str(md or "").rstrip()
    lines = list((ev or {}).get("lines") or [])
    tag = str((ev or {}).get("tag") or "")
    parts = []
    if body:
        parts.append(body)
    if lines:
        parts.append("\n".join(lines))
    if tag:
        parts.append(tag)
    return "\n\n".join(parts)


def _closeout_md(tid, md, v, problems=None, extra=None, allow_run=True):
    """收尾便捷入口：检查 + 拼接到报告 md 上（验证轮最终回复用）。"""
    ev = _closeout_check(tid=tid,
                         plan_id=str((v or {}).get("plan_id") or ""),
                         problems=list(problems or []),
                         title=str((v or {}).get("title") or ""),
                         allow_run=allow_run, extra=extra)
    return _with_closeout(md, ev), ev


def _emit_closeout(tid, title="", plan_id="", header="", problems=None,
                   extra=None, allow_run=True):
    """把「回合收尾证据 + 未闭环标记」当一条 report 事件发出去。

    所有「这一轮到此为止」的出口都走这里 —— 用户要求「不允许静默结束」，
    所以哪怕是出错 / 被终止，也要有一条带证据的最终回复。任何异常都吞掉：
    收尾提示坏了绝不能反过来影响迭代本身。
    """
    try:
        md, ev = _closeout_md(tid,
                              str(header or "**本轮收尾**").rstrip(),
                              {"plan_id": plan_id, "title": title},
                              problems=problems, extra=extra,
                              allow_run=allow_run)
        _emit(tid, "report", text=md, title=str(title or "")[:80])
        return ev
    except Exception:                                   # noqa: BLE001
        logger.debug("回合收尾证据检查没跑成（不影响迭代）", exc_info=True)
        return {}


# ---------------------------------------------------------------- 静态面优先修
# ★ 2026-11-23 用户要求：「评分前先跑静态检查，对比基线若新增扣分点，先走修复链
#   修掉再评分；并把『新增扣分点优先修』写进修复链 prompt」。
#   动机来自反复出现的实测教训「跑分退步（低于上一版）」：根因常常是这次改动
#   **顺手引入的一处静态扣分点**（`except ...: pass` 静默吞异常、裸 `except:`），
#   等分数掉下来才发现，修复链还得重新猜是哪个改动弄的。于是改成「静态面优先」：
#     ① 评分前先跑一遍 code_audit（约 7~10 秒）；
#     ② 跟基线（上一版评分记下的扣分点 / 本模块快照）逐条比对，挑出**新增**的；
#     ③ 新增的点**先修掉**：只做确定性修复（silent-except 改成留痕日志、裸 except
#        限定成 except Exception）—— 秒钟级、不调 LLM、改完必须仍能 compile；
#     ④ 修不掉的（不在本轮改动文件里、或不是可安全自动修的 kind）不硬改，写进
#        报告 + 修复链 prompt，让修复任务第一件事就是把它修掉；
#     ⑤ 最后才评分 —— 分数反映「静态面已修正后」的状态，不会因为一行
#        `except: pass` 白掉 0.5 分还被判退步。
_STATIC_BASELINE_FILE = "iter_runner_static_baseline.json"
# 允许确定性自动修的静态问题种类（只挑「改法唯一、语义不变」的这两种）。
_AUTOFIX_KINDS = ("silent-except", "bare-except")
# 护栏：这些文件**绝不自动改**（改了就等于自己判自己及格）
_AUTOFIX_DENY = ("backend/selfdev.py", "backend/regress.py",
                 "backend/_feature_tests/")


def _norm_rel(rel):
    """统一成 `backend/xxx.py` 这种相对路径写法，便于比较。"""
    s = str(rel or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s


def _static_baseline_path():
    return os.path.join(str(_si._DATA), _STATIC_BASELINE_FILE)


def _run_static_audit():
    """跑一遍静态自检（约 7~10 秒）。跑不起来返回 {}，绝不抛。"""
    try:
        from . import code_audit as _ca
        return _ca.audit() or {}
    except Exception as e:                              # noqa: BLE001
        logger.warning("静态自检没跑成（评分前预检跳过修复）：%s: %s",
                       type(e).__name__, e)
        return {}


def _point_key(f):
    """扣分点指纹：文件:行:种类（同一条问题在不同轮次必须算出同样的 key）。"""
    return "%s:%s:%s" % (_norm_rel((f or {}).get("file")),
                         int((f or {}).get("line") or 0),
                         str((f or {}).get("kind") or ""))


def _point_brief(f):
    return {"file": _norm_rel(f.get("file")), "line": int(f.get("line") or 0),
            "kind": str(f.get("kind") or ""), "level": str(f.get("level") or ""),
            "msg": str(f.get("msg") or "")[:160], "key": _point_key(f)}


def _static_points(audit=None):
    """把一次自检压成「计分扣分点」列表（noscore 已知无害项不算，与评分口径一致）。"""
    a = audit if isinstance(audit, dict) else _run_static_audit()
    out = []
    for f in (a.get("findings") or []):
        if not isinstance(f, dict):
            continue
        if f.get("noscore"):
            # 与 code_audit / iterloop 的计分口径保持一致：已知无害项不扣分，
            # 也就不该被当成「新增扣分点」去修。
            continue
        out.append(_point_brief(f))
    return out


def _prev_static_points():
    """上一版评分里记下的静态扣分点（最权威的「改动前基线」）。

    返回 None 表示**拿不到**（不是「上一版干净」）—— 两者含义完全不同。
    """
    try:
        from . import scoregate as _sg
        _load = getattr(_sg, "_load", None)
        _prev = getattr(_sg, "_prev_score", None)
        if not callable(_load) or not callable(_prev):
            return None
        prev = _prev(_load() or {})
        if not isinstance(prev, dict):
            return None
        st = prev.get("static")
        if not isinstance(st, dict):
            return None
        fl = st.get("findings")
        if not isinstance(fl, list):
            return None
        return {"keys": {_point_key(f) for f in fl if isinstance(f, dict)},
                "source": "上一版评分", "at": str(prev.get("iso") or "")}
    except Exception:                                   # noqa: BLE001
        logger.debug("读上一版静态明细失败（改看本模块快照）", exc_info=True)
        return None


def load_static_baseline():
    """取静态扣分点基线。

    优先级：① 上一版评分记的静态明细（就是「上一版」）；② 本模块自己的快照。
    keys=None 表示**没有基线可用**（调用方要退化成「按本轮改过的文件判定」，
    绝不能把它当成「基线是 0 个问题」—— 那会把整库的存量问题全算成新增）。
    """
    prev = _prev_static_points()
    if prev is not None:
        return prev
    try:
        with open(_static_baseline_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        keys = (d or {}).get("keys")
        if isinstance(keys, list):
            return {"keys": {str(k) for k in keys}, "source": "本模块快照",
                    "at": str((d or {}).get("at") or "")}
    except Exception:                                   # noqa: BLE001
        logger.debug("读静态基线快照失败（当作没有基线）", exc_info=True)
    return {"keys": None, "source": "", "at": ""}


def record_static_baseline(audit=None, points=None):
    """把当前静态扣分点记成基线（每次评分结束后/首轮无基线时调用）。"""
    pts = points if isinstance(points, list) else _static_points(audit)
    d = {"keys": sorted(str(p.get("key") or _point_key(p)) for p in pts),
         "at": _now(), "n": len(pts)}
    try:
        p = _static_baseline_path()
        d0 = os.path.dirname(p)
        if d0:
            os.makedirs(d0, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("写静态基线快照失败（不影响评分）", exc_info=True)
    return d


def _in_files(rel, files):
    """这条问题是不是落在这些文件里（按相对路径或文件名比，兼容写法差异）。"""
    r = _norm_rel(rel)
    base = os.path.basename(r)
    for f in (files or []):
        c = _norm_rel(f)
        if not c:
            continue
        if r == c or r.endswith("/" + c) or c.endswith("/" + r) \
                or base == os.path.basename(c):
            return True
    return False


def diff_static_points(baseline, now_points, files=None):
    """比对基线，挑出**新增**扣分点。

    没有基线时退一步：把「本轮改过的文件」里的扣分点都算新增 —— 那些文件反正
    是这次动过的，先修它们不会误伤，也比放过一条新引入的静默吞异常强。
    """
    base = (baseline or {}).get("keys")
    pts = [p for p in (now_points or []) if isinstance(p, dict)]
    if base is None:
        added = [p for p in pts if _in_files(p.get("file"), files)]
        nf = len([x for x in (files or []) if str(x or "").strip()])
        return {"added": added, "fixed": [], "base_unknown": True,
                "base_source": "无基线（按本轮改过的 %d 个文件判定）" % nf}
    now_keys = {p.get("key") or _point_key(p) for p in pts}
    added = [p for p in pts if (p.get("key") or _point_key(p)) not in base]
    fixed = sorted(k for k in base if k not in now_keys)
    return {"added": added, "fixed": fixed, "base_unknown": False,
            "base_source": str((baseline or {}).get("source") or "基线")}


def _autofix_allowed(rel):
    """这个文件允许不允许自动改（护栏文件一律不许）。"""
    r = _norm_rel(rel)
    if not r.endswith(".py"):
        return False
    for g in _AUTOFIX_DENY:
        if r == g or r.startswith(g):
            return False
    return r.startswith("backend/")


def _module_has_logger(tree):
    """模块级有没有 logger 名字（决定 silent-except 改成日志还是打 noqa）。"""
    import ast as _ast
    for node in getattr(tree, "body", []) or []:
        if isinstance(node, _ast.Assign):
            for t in node.targets:
                if isinstance(t, _ast.Name) and t.id == "logger":
                    return True
        elif isinstance(node, _ast.AnnAssign):
            t = node.target
            if isinstance(t, _ast.Name) and t.id == "logger":
                return True
    return False


def _rewrite_except_lines(rel, kinds):
    """把文件里可安全自动修的 except 处理改掉，返回改动行数（0=没改）。

    只做两种「语义不变」的机械修法：
      · silent-except（`except X: pass`）→ 有 logger 就改成 logger.debug 留痕，
        没有就按本库既有约定在 except 行尾标 `# noqa`（等价于「显式声明无害」）；
      · bare-except（`except:`）→ 限定成 `except Exception:`，不再连
        KeyboardInterrupt / SystemExit 一起吞。
    改完必须先 compile 通过才落盘；不通过就原样放弃（宁可没修，也不能改坏）。
    """
    import ast as _ast
    r = _norm_rel(rel)
    p = os.path.join(str(_si._ROOT), r.replace("/", os.sep))
    try:
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return 0
    try:
        tree = _ast.parse(src, filename=p)
    except SyntaxError:
        return 0
    lines = src.splitlines(keepends=True)
    has_logger = _module_has_logger(tree)
    edits = {}
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.ExceptHandler):
            continue
        ln = int(getattr(node, "lineno", 0) or 0)
        if ln <= 0 or ln > len(lines):
            continue
        head = lines[ln - 1]
        if "noqa" in head.lower():
            continue                    # 已显式声明过，不动
        if node.type is None:
            if "bare-except" in kinds and "except:" in head:
                edits[ln] = head.replace("except:", "except Exception:", 1)
            continue
        if "silent-except" not in kinds:
            continue
        body = node.body or []
        if len(body) != 1 or not isinstance(body[0], _ast.Pass):
            continue
        pl = int(getattr(body[0], "lineno", 0) or 0)
        if pl <= 0 or pl > len(lines):
            continue
        if has_logger:
            raw = lines[pl - 1]
            indent = raw[:len(raw) - len(raw.lstrip())]
            edits[pl] = indent + 'logger.debug("silent-except", exc_info=True)\n'
        else:
            edits[ln] = head.rstrip() + "  # noqa\n"
    if not edits:
        return 0
    new = list(lines)
    for ln, txt in edits.items():
        new[ln - 1] = txt
    out = "".join(new)
    try:
        compile(out, p, "exec")
    except SyntaxError:
        logger.warning("自动修静态扣分点后 %s 编不过，已放弃这次修改", r)
        return 0
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(out)
    except OSError:
        logger.warning("自动修静态扣分点写 %s 失败（放弃）", r)
        return 0
    logger.info("静态面优先修：%s 改了 %d 处 except 处理", r, len(edits))
    return len(edits)


def fix_new_static_points(tid, v, added):
    """「新增扣分点优先修」：确定性就地修掉本轮新引入的静态扣分点。

    只动**本轮改过的文件**里、可安全自动修的 kind（见 _AUTOFIX_KINDS）；
    其它的**绝不硬改**（改错比不改更糟），留给修复链（prompt 里已写明要先修这些）。
    返回 {"fixed": [...], "left": [...], "files": [...]}。
    """
    v = v or {}
    pts = [p for p in (added or []) if isinstance(p, dict)]
    files = [_norm_rel(x) for x in (v.get("files") or []) if str(x or "").strip()]
    by_file = {}
    for p in pts:
        by_file.setdefault(_norm_rel(p.get("file")), []).append(p)
    touched = []
    for rel, items in by_file.items():
        kinds = {str(i.get("kind") or "") for i in items} & set(_AUTOFIX_KINDS)
        if not kinds:
            continue
        if not _autofix_allowed(rel):
            continue
        if files and not _in_files(rel, files):
            # 不是这次改出来的文件 → 不碰（那是存量问题，乱改会误伤）
            continue
        if _rewrite_except_lines(rel, kinds) > 0:
            touched.append(rel)
    # 修完再跑一遍自检，**以自检结果为准**判定到底修掉了什么
    if touched:
        after = _static_points(_run_static_audit())
        left_keys = {p.get("key") or _point_key(p) for p in after}
        really_fixed = [p for p in pts
                        if (p.get("key") or _point_key(p)) not in left_keys]
        left = [p for p in pts if (p.get("key") or _point_key(p)) in left_keys]
    else:
        really_fixed, left = [], pts
    return {"fixed": really_fixed, "left": left, "files": touched}


def precheck_static(tid, v):
    """评分前的静态预检：跑自检 → 比对基线 → 新增扣分点先修掉 → 记新基线。

    「新增扣分点优先修」就在这里落地：**先修，再评分**。返回的 dict 直接进
    评分结果（sc["static_first"]）与报告，便于事后归因「这一分的差别是修掉哪条换来的」。
    """
    v = dict(v or {})
    audit = _run_static_audit()
    ran = bool(audit.get("ok"))
    now_pts = _static_points(audit)
    base = load_static_baseline()
    diff = diff_static_points(base, now_pts, v.get("files"))
    added = diff.get("added") or []
    counts = audit.get("counts_scored") or audit.get("counts") or {}
    info = {"ran": ran, "points": len(now_pts),
            "high": int(counts.get("high") or 0),
            "mid": int(counts.get("mid") or 0),
            "info": int(counts.get("info") or 0),
            "baseline": diff.get("base_source") or "",
            "base_known": not bool(diff.get("base_unknown")),
            "added": [_point_brief(p) for p in added][:12],
            "added_n": len(added),
            "fixed": [], "fixed_n": 0, "left": [], "left_n": 0,
            "fix_files": [], "ts": _now()}
    if added:
        _emit(tid, "step",
              text="评分前静态预检：新增 %d 处扣分点（基线：%s），先修掉再评分…"
                   % (len(added), info["baseline"] or "无"))
        try:
            r = fix_new_static_points(tid, v, added)
        except Exception as e:                          # noqa: BLE001
            logger.warning("静态面优先修自己出错（继续评分）", exc_info=True)
            r = {"fixed": [], "left": added, "files": [],
                 "error": "%s: %s" % (type(e).__name__, e)}
        info["fixed"] = [_point_brief(p) for p in (r.get("fixed") or [])][:12]
        info["fixed_n"] = len(r.get("fixed") or [])
        info["left"] = [_point_brief(p) for p in (r.get("left") or [])][:12]
        info["left_n"] = len(r.get("left") or [])
        info["fix_files"] = list(r.get("files") or [])
        if info["fixed_n"]:
            _emit(tid, "step",
                  text="已就地修掉 %d 处新增扣分点（%s），再评分…"
                       % (info["fixed_n"], "、".join(info["fix_files"])[:160]))
        if info["left_n"]:
            _emit(tid, "step",
                  text="还有 %d 处新增扣分点不能安全自动修（%s），"
                       "已写进修复链：**新增扣分点优先修**"
                       % (info["left_n"],
                          "、".join("%s:%s %s" % (p.get("file"), p.get("line"),
                                                  p.get("kind"))
                                   for p in info["left"][:3])))
    # 评分后这一版就是下一轮的基线；顺手现在记一次，
    # 万一这次评分挂了（没走到后面）也不会让下一轮误把存量当新增。
    try:
        if info["fixed_n"]:
            record_static_baseline(points=_static_points(_run_static_audit()))
        else:
            record_static_baseline(points=now_pts)
    except Exception:                                   # noqa: BLE001
        logger.debug("记静态基线失败（不影响评分）", exc_info=True)
    return info


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
        # ★ 2026-11-23（教训：「跑分退步（低于上一版）」×2）：
        #   验证器虽然已经跑过 code_audit 并拦 high/mid，但**评分这一步跟静态结果
        #   脱钩** —— 于是「静态扣分点被引入」只能等分数掉下来才发现，修复链还得
        #   重新猜是哪个改动弄的。
        # ★★ 2026-11-23 本轮需求：不再只是「取一次计数塞进结果」，而是
        #   **评分前先跑静态检查 → 对比基线 → 新增扣分点先走修复链修掉 → 才评分**
        #   （见 precheck_static / fix_new_static_points）。于是「一行 except: pass
        #   白掉 0.5 分还被判退步」这种情况在评分之前就已经被修掉了。
        _pf = {}
        try:
            _pf = precheck_static(tid, v) or {}
        except Exception as e:                          # noqa: BLE001
            logger.warning("评分前的静态预检没跑成（照常评分）", exc_info=True)
            _pf = {"ran": False, "error": "%s: %s" % (type(e).__name__, e)}
        # 兼容老口径（lessonloop 的探针就认 iter_runner 里的 static_first）：
        # ran/high/mid 三个键保留，另外带上本轮新增/已修/未修的明细。
        _static = {"ran": bool(_pf.get("ran")),
                   "high": int(_pf.get("high") or 0),
                   "mid": int(_pf.get("mid") or 0)}
        _static.update(_pf)
        sc = _sg.gate(force=True, block_on_drop=False,
                      note="迭代验证通过后自动跑分：%s" % (v.get("title") or ""))
        if isinstance(sc, dict):
            sc["static_first"] = _static
            if int(_pf.get("added_n") or 0):
                # 新增扣分点在本轮已经先修了（fixed_n）/ 还剩几处要修复链接着修
                sc["static_new"] = {"added": int(_pf.get("added_n") or 0),
                                    "fixed": int(_pf.get("fixed_n") or 0),
                                    "left": int(_pf.get("left_n") or 0),
                                    "fix_files": list(_pf.get("fix_files") or []),
                                    "left_points": list(_pf.get("left") or [])}
            if _static.get("ran") and (_static["high"] or _static["mid"]):
                sc["static_hint"] = ("静态面还有 %d high / %d mid 扣分点，"
                                     "退步时先修这些再重跑"
                                     % (_static["high"], _static["mid"]))
            elif int(_pf.get("left_n") or 0):
                sc["static_hint"] = ("新增 %d 处静态扣分点还没修掉（%s），"
                                     "**新增扣分点优先修**：先修这些再重跑"
                                     % (int(_pf.get("left_n") or 0),
                                        "、".join(str(p.get("file") or "")
                                                  for p in (_pf.get("left") or [])[:3])))
            # ★ 2026-11-23 本轮需求（用户备注「拉起测试 RSI」）：
            #   跑分这一步要**同时**把「测试」和「RSI」拉起来 ——
            #     ① 测试（回归）：回答「有没有改坏」，本轮没跑过就当场拉起一次；
            #     ② RSI：回答「有没有变好」，当场采样（内有 60 秒节流）+ 取
            #        「最该修的一项」，和分数写在同一条结果里。
            #   两件事都失败也不影响评分本身（证据缺了要如实空着，不能造假）。
            try:
                _rgv = _regress_evidence(tid=tid, allow_run=True)
                sc["regress"] = {"ok": bool(_rgv.get("ok")),
                                 "total": int(_rgv.get("total") or 0),
                                 "passed": int(_rgv.get("passed") or 0),
                                 "failed": int(_rgv.get("failed") or 0),
                                 "line": _regress_verdict(_rgv)[1]}
            except Exception:                           # noqa: BLE001
                logger.debug("跑分时拉起回归证据失败（不影响评分）", exc_info=True)
            try:
                from . import lessonloop as _ll
                try:
                    _ll.rsi_sample(force=False)
                except Exception:                       # noqa: BLE001
                    logger.debug("RSI 采样失败（不影响评分）", exc_info=True)
                sc["rsi"] = str(_ll.rsi_line() or "").strip()
            except Exception:                           # noqa: BLE001
                logger.debug("拉起 RSI 失败（不影响评分）", exc_info=True)
            # 单次改动文件数上限（3 个）：写进结果，报告里能看出「这一轮凭什么是
            # 这几个文件的分」，也便于事后解释超上限被顺延的那几个。
            sc["change_cap"] = {"limit": _MAX_CHANGE_FILES,
                                "files": list(v.get("files") or []),
                                "over": list(v.get("files_over_cap") or [])}
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
    # ★ 2026-11-23 需求「评分前先跑静态检查，新增扣分点先修再评」：
    #   报告里必须留下「这次新增了几条、修掉几条、还剩几条」——
    #   否则用户只看到分数，看不出「那一分是修掉哪条换回来的」。
    sn = sc.get("static_new") or {}
    if int(sn.get("added") or 0):
        out.append("- **静态面优先修**：新增扣分点 %d 处 → 评分前已修掉 %d 处%s" % (
            int(sn.get("added") or 0), int(sn.get("fixed") or 0),
            ("，还剩 %d 处已写进修复链（新增扣分点优先修：%s）" % (
                int(sn.get("left") or 0),
                "、".join("`%s`" % x for x in (sn.get("fix_files") or [])[:3])))
            if int(sn.get("left") or 0) else ""))
    rec = sc.get("rec") or {}
    press = rec.get("press") or {}
    if press.get("items"):
        out.append("- **压分点**：%d 处，共压 %.2f 分" % (
            len(press.get("items") or []), float(press.get("lost") or 0)))
    drop = sc.get("drop") or {}
    if drop:
        out.append("- **退步归因**：%s" % str(drop.get("why") or drop.get("note") or "已记录")[:200])
    # ★ 2026-11-23 用户备注「拉起测试 RSI」：报告里「测试」和「RSI」都要出现 ——
    #   前者说没改坏，后者说在变好，两者分开写才不会被分数一句话糊过去。
    rg = sc.get("regress") or {}
    if rg:
        out.append("- **测试（本轮回归）**：%s" % str(
            rg.get("line") or ("%s/%s 通过" % (rg.get("passed"), rg.get("total"))))[:200])
    rsi = str(sc.get("rsi") or "").strip()
    if rsi:
        out.append("- **RSI**：%s" % rsi[:240])
    cap = sc.get("change_cap") or {}
    if cap:
        out.append("- **单次改动上限**：≤%d 个文件；本轮 %d 个%s" % (
            int(cap.get("limit") or _MAX_CHANGE_FILES),
            len(cap.get("files") or []),
            ("，超上限顺延到下一轮 %d 个（%s）" % (
                len(cap.get("over") or []),
                "、".join("`%s`" % x for x in (cap.get("over") or [])[:3])))
            if (cap.get("over") or []) else ""))
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
    # ★ 单次改动文件数上限（3 个）：报告里明写口径，用户才知道「这轮为什么
    #   只动了这几个文件」，以及超出的那几个去哪了（顺延到下一轮）。
    out.append("- **单次改动上限**：≤%d 个文件%s" % (
        _MAX_CHANGE_FILES,
        ("（本轮改了 %d 个，超出的顺延）" % len(files)) if len(files) > _MAX_CHANGE_FILES else ""))
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
    # ★ 2026-11-23 回合收尾强制证据检查：新的一轮验证开始，之前记的回归结果
    #   一律作废 —— 收尾要的是**这一轮真跑出来的**通过率，不是上一轮的。
    _round_begin("verify:%s" % (v.get("title") or ""))

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
            # ★ 2026-11-23 收尾强制证据检查：分数退步本身就是「这一轮没闭环」，
            #   必须显式写进最终回复（回归可能全过，但整体验收没过）。
            md, _ev = _closeout_md(
                tid, md, v, problems,
                extra=["跑分退步 %+.2f（%s → %s）：分数没涨回去就不算闭环" % (
                    float(_sc.get("delta") or 0), _sc.get("prev"),
                    _sc.get("total"))])
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
            # ★ 2026-11-23 需求：把「新增扣分点」显式交给修复链，让修复计划
            #   第一条动作就是「新增扣分点优先修」—— 否则修复链又要重新猜掉分原因。
            _focus = list((_sc.get("static_new") or {}).get("left_points") or [])
            try:
                try:
                    _a2 = _si.run_audit(deep=False, source="manual",
                                        create_plan=True, focus=_focus)
                except TypeError:                       # 老签名兜底（没 focus 参数）
                    _a2 = _si.run_audit(deep=False, source="manual",
                                        create_plan=True)
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

        # ★ 2026-11-23 收尾强制证据检查：这一轮算「通过」之前，先把
        #   回归通过数 + 遗留清单写进最终回复；不满足就带上「未闭环」标记。
        md, _ev = _closeout_md(tid, md, v, problems)
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
    # ★ 2026-11-23 收尾强制证据检查（下面三个分支都要发最终报告，统一在这里拼一次）：
    #   验证没过时「回归通过数」和「遗留清单（=没过的问题）」都必须写在回复里，
    #   末尾带上「⚠️ 本轮未闭环：…」—— 不允许只发一句「验证没过」就结束。
    md, _ev = _closeout_md(tid, md, v, problems)
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


_WATCHDOG = [None]


def _reclaim_dead_run(reason="看门狗"):
    """把「线程已死但 running 还挂着」的任务回收掉，真没跑完的放回队首。

    返回 orphan 字典（None 表示没有僵尸）。调用方负责 _save() 和后续 _next()。
    """
    with _LOCK:
        run = _STATE.get("running")
        if not run or _run_alive(run):
            return None
        _STATE["running"] = None
        done = any((h.get("tid") or "") == (run.get("tid") or "")
                   for h in (_STATE.get("history") or []))
        if done:
            return dict(run, finished=True)
        _STATE["queue"].insert(0, {
            "tid": run.get("tid"), "plan_id": run.get("plan_id"),
            "source": run.get("source") or "manual",
            "title": run.get("title") or "",
            "max_n": run.get("max_n") or 0,
            "at": run.get("started_at") or time.time(),
            "brief": run.get("brief") or {},
            "cur_action": run.get("cur_action") or {},
            "resumed": True})
        logger.warning("%s：任务线程已不在，放回队列接着做：%s", reason,
                       run.get("plan_id"))
        return dict(run)


def _where_text(run):
    """「上次停在 第 3/9 条：xxx」这类人话，没有就返回空串。"""
    ca = (run or {}).get("cur_action") or {}
    if not ca.get("title"):
        return ""
    return "，上次停在「第 %s/%s 条：%s」" % (
        ca.get("index") or "?", ca.get("total") or "?", ca.get("title"))


def _start_watchdog():
    """看门狗：每 30 秒查一次 worker 线程是否还活着。

    ★ 2026-11-23 用户要求「除了手动终止，其他非正常关闭重启都要重新拉起，
      并且记得进度」：
      · 跨进程的重启 —— boot_resume / _load 用 started_at < _BOOT_AT 能识别；
      · **本进程内** worker 被硬杀 / 抛未捕获异常（finally 没跑到）——
        running 会永远挂着，界面显示「一直在跑」，实际早停了，而且 submit
        的 dup 判断会让这个计划再也提交不进去。
      这里补上第三种情况的自动回收：死了就按 history 判断是「已跑完只是
      占位没清」还是「真没跑完」，后者放回队首接着做并说明停在哪。
    """
    th0 = _WATCHDOG[0]
    if th0 is not None and th0.is_alive():
        return False

    def _loop():
        while True:
            time.sleep(30)
            try:
                orphan = _reclaim_dead_run("看门狗")
                if not orphan:
                    continue
                _save()
                tid = orphan.get("tid") or "_sys"
                if orphan.get("finished"):
                    _emit(tid, "status",
                          text="「%s」已经结束，只是占位没清，已回收"
                               % (orphan.get("title") or orphan.get("plan_id")),
                          state="done", _force_save=True)
                else:
                    _emit(tid, "status",
                          text="⚠️ 执行线程意外停止（不是正常结束），已放回队列接着做%s"
                               % _where_text(orphan),
                          state="waiting", _force_save=True)
                    _next()
            except Exception:                            # noqa: BLE001
                logger.debug("看门狗循环出错", exc_info=True)

    th = threading.Thread(target=_loop, name="iter-watchdog", daemon=True)
    _WATCHDOG[0] = th
    th.start()
    logger.info("已启动迭代看门狗（每 30 秒查一次线程存活）")
    return True


def boot_resume():
    """进程启动后调用：有待验证的先验证，有排队的接着跑。

    由 main.py 的 startup 调用（不在模块级自动跑 —— 否则任何 import 本模块的
    工具进程都会意外开始执行任务）。
    """
    time.sleep(2.5)                                     # 等 FastAPI 起来
    try:
        _start_watchdog()
    except Exception:                                   # noqa: BLE001
        logger.debug("启动看门狗失败（不影响恢复队列）", exc_info=True)
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
                                           # ★ 2026-11-23：把「停在哪一条」一起带进队列，
                                           #   重启后能明确告诉用户从哪儿继续。
                                           "cur_action": run.get("cur_action") or {},
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
                  text="上次重启时「%s」还没跑完%s，已放回队列接着做"
                       % (_name, _where_text(orphan)),
                  state="running", _force_save=True)
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
        # ★ 2026-11-23 用户要求「记得当前工作干到哪」：_load 已经把孤儿任务
        #   放回队列了（boot_resume 的 orphan 分支其实走不到），所以「停在哪一条」
        #   要在这里说出来 —— 否则用户只看到「恢复第 N 个任务」，
        #   不知道上次是卡在哪一步。
        _emit_sys("重启后恢复：正在恢复第 %d 个任务「%s」（队列里共 %d 个等待）%s"
                  % (done_n + 1,
                     first.get("title") or first.get("plan_id") or "（未命名）", qn,
                     _where_text(first)),
                  state="running")
        _next()


def _plan_actions(plan_id):
    """把计划里的动作清单抽成前端能直接渲染的列表（带状态 / 文件 / 风险）。"""
    out = []
    if not plan_id:
        return out
    try:
        r = _si.get_plan(plan_id)
        p = (r or {}).get("plan") or {}
        for a in (p.get("actions") or []):
            if not isinstance(a, dict):
                continue
            out.append({
                "id": str(a.get("id") or ""),
                "title": str(a.get("title") or "")[:80],
                "status": str(a.get("status") or "pending"),
                "files": [f for f in (a.get("files") or []) if isinstance(f, str)][:6],
                "risk": str(a.get("risk") or ""),
                "error": str(a.get("error") or "")[:200],
            })
    except Exception:                                    # noqa: BLE001
        logger.debug("抽动作清单失败（详情里就不显示动作表）", exc_info=True)
    return out


def _task_progress(acts):
    """进度：第几步 / 共几步 / 当前在干什么。"""
    total = len(acts)
    okn = len([a for a in acts if a["status"] in ("done", "ok", "skipped")])
    cur = next((a for a in acts if a["status"] in ("running", "doing")), None)
    if total and okn < total:
        step = okn + 1
    else:
        step = okn
    return {"total": total, "done": okn, "step": step,
            "current": (cur or {}).get("title") or ""}


def journal(limit=40):
    """★ 2026-11-23 用户要求：「左侧工具栏增加一个可以查看自迭代日志」。

    一次给出弹窗要的全部东西（前端不用连调好几个接口）：
      · running / queue / history（最近 limit 条）
      · 每条任务的**进度**（第几步 / 共几步 / 当前在干什么）
      · **动作清单**（成功 / 失败 / 跳过 / 待做）
      · **分数变化**（改动前后 + 进退，从事件里摘）
      · **事件日志**（验证 / 跑分 / 反思的原始记录，最多 60 条）
      · 「一键拉起」候选列表（中断 / 失败 / 改完没验证 / 队列卡住）
    """
    limit = max(1, min(int(limit or 40), 200))
    with _LOCK:
        run = dict(_STATE["running"]) if _STATE.get("running") else None
        q = [dict(x) for x in (_STATE["queue"] or [])]
        hist = [dict(x) for x in (_STATE["history"] or [])[:limit]]
        events = list(_STATE.get("events") or [])
        verify = dict(_STATE.get("verify") or {}) or None
        budget = int(_STATE.get("budget") or 0)
        reps = list(_STATE.get("reports") or [])[:5]
        seq = int(_STATE.get("seq") or 0)
        # ★ 2026-11-23 回合收尾证据检查的结果（回归通过数 / 遗留清单 / 未闭环原因）
        closeout = dict(_STATE.get("last_closeout") or {})

    by_tid = {}
    for e in events:
        by_tid.setdefault(str(e.get("tid") or ""), []).append(e)

    def _score_of(tid):
        """从事件里摘最近一次跑分。

        ⚠️ 2026-11-23 实测踩到（用户问「跑分呢，为什么没看到」）：
          原来只读 `e["text"]`，但跑分事件是用 `_ev("score", before=..., after=...,
          delta=..., cum=..., verdict=...)` 发出来的 —— **根本没有 text 字段**。
          于是每条任务摘出来的分数都是空串，界面上永远显示「（无）」，
          用户会以为跑分没跑。这里改成从结构化字段拼出人话，
          并兼容 loop_score（单轮绝对分）与老版本带 text 的记录。
        """
        _vz = {"improved": "进步", "regressed": "退步", "flat": "持平"}
        for e in reversed(by_tid.get(tid, [])):
            _k = str(e.get("kind") or "")
            if _k == "score":
                _txt = str(e.get("text") or "").strip()
                if _txt:
                    return _txt
                _b, _a, _d = e.get("before"), e.get("after"), e.get("delta")
                if _b is None and _a is None:
                    continue
                _s = "跑分 %s → %s" % (
                    "?" if _b is None else ("%g" % float(_b)),
                    "?" if _a is None else ("%g" % float(_a)))
                if _d is not None:
                    _s += "（%+.2f，%s）" % (float(_d),
                                             _vz.get(str(e.get("verdict") or ""), "未知"))
                if e.get("cum") is not None:
                    _s += " 累计 %+.2f" % float(e.get("cum"))
                return _s
            if _k == "loop_score":
                _tot = e.get("total")
                if _tot is None:
                    continue
                return "本轮评分 %g（%+.2f，%s）" % (
                    float(_tot), float(e.get("delta") or 0.0),
                    _vz.get(str(e.get("verdict") or ""), "未知"))
        return ""

    def _pack(t, state_hint=""):
        if not t:
            return None
        tid = str(t.get("tid") or "")
        acts = _plan_actions(t.get("plan_id"))
        evs = by_tid.get(tid, [])
        return {
            "tid": tid,
            "plan_id": str(t.get("plan_id") or ""),
            "source": str(t.get("source") or ""),
            "title": str(t.get("title") or "")[:120],
            "state": state_hint or str(t.get("state") or ""),
            "started_at": t.get("started_at") or 0,
            "ended_at": t.get("ended_at") or 0,
            "elapsed": (round(time.time() - float(t.get("started_at") or time.time()), 1)
                        if not t.get("ended_at") else t.get("seconds") or 0),
            "at": str(t.get("at") or ""),
            "progress": _task_progress(acts),
            "actions": acts,
            "score": _score_of(tid),
            "done": t.get("done"), "failed": t.get("failed"),
            "skipped": t.get("skipped"),
            "files": t.get("files") or [],
            "error": str(t.get("error") or "")[:300],
            "events": evs[-60:],
        }

    tasks = []
    if run:
        tasks.append(_pack(run, "running"))
    for x in q:
        tasks.append(_pack(x, "waiting"))
    for h in hist:
        tasks.append(_pack(h))

    return {
        "ok": True,
        "seq": seq,
        "state": _state_name(run, q),
        "running": _pack(run, "running"),
        "queue": [_pack(x, "waiting") for x in q],
        "history": [_pack(h) for h in hist],
        "tasks": tasks,
        "verify": verify,
        "budget": budget,
        "max_rounds": _MAX_AUTO_ROUNDS,
        "reports": reps,
        # ★ 2026-11-23 回合收尾强制证据检查的结果（回归通过数 / 遗留清单 /
        #   未闭环原因 / RSI）。前端浮层与诊断脚本都直接读这一个字段，
        #   不用再自己拼「到底闭环没有」。
        "closeout": closeout,
        "candidates": revive_candidates(limit=limit),
        "counts": {
            "running": 1 if run else 0,
            "queue": len(q),
            "history": len(hist),
        },
    }


def revive_candidates(limit=40):
    """★ 用户要的「一键拉起未成功执行的完整任务」—— 范围：全部。

    列出四类可拉起的：
      · failed  / error     执行失败或异常中断
      · cancelled           被终止（动作没做完）
      · unverified          改完登记了待验证，但验证没跑完（服务没重启就一直挂着）
      · stuck               队列里有、却没有任务在跑（卡住了）
    """
    limit = max(1, min(int(limit or 40), 200))
    with _LOCK:
        hist = [dict(x) for x in (_STATE["history"] or [])[:limit]]
        q = [dict(x) for x in (_STATE["queue"] or [])]
        run = _STATE.get("running")
        verify = dict(_STATE.get("verify") or {}) or None
        budget = int(_STATE.get("budget") or 0)

    out = []
    seen_plan = set()
    for h in hist:
        st = str(h.get("state") or "")
        if st not in ("failed", "error", "cancelled"):
            continue
        pid = str(h.get("plan_id") or "")
        if pid and pid in seen_plan:
            continue
        if pid:
            seen_plan.add(pid)
        out.append({
            "kind": "failed" if st in ("failed", "error") else "cancelled",
            "kind_text": {"failed": "执行失败", "error": "异常中断",
                          "cancelled": "被终止"}.get(st, st),
            "tid": str(h.get("tid") or ""),
            "plan_id": pid,
            "title": str(h.get("title") or "")[:120],
            "at": str(h.get("at") or ""),
            "done": h.get("done"), "failed": h.get("failed"),
            "error": str(h.get("error") or "")[:200],
            "can_revive": bool(pid),
            "why": "" if pid else "这条记录没有计划 id，无法重新拉起",
        })
    if verify:
        out.append({
            "kind": "unverified",
            "kind_text": "改完还没验证",
            "tid": str(verify.get("tid") or ""),
            "plan_id": str(verify.get("plan_id") or ""),
            "title": str(verify.get("title") or "")[:120],
            "at": (time.strftime("%m-%d %H:%M:%S",
                                 time.localtime(float(verify.get("at") or 0)))
                   if verify.get("at") else ""),
            "tries": int(verify.get("tries") or 0),
            "files": verify.get("files") or [],
            "can_revive": True,
            "why": "",
        })
    if q and not run and budget < _MAX_AUTO_ROUNDS:
        for x in q:
            out.append({
                "kind": "stuck",
                "kind_text": "排队中但没在跑",
                "tid": str(x.get("tid") or ""),
                "plan_id": str(x.get("plan_id") or ""),
                "title": str(x.get("title") or "")[:120],
                "at": "",
                "can_revive": True,
                "why": "",
            })
    return out


def revive(kind="", tid="", plan_id="", limit=0):
    """★ 一键拉起：按候选类型把任务重新拉起来。

    kind:
      · "unverified" → 直接启动验证线程（连续两轮 → 跑分 → 反思），
                       不用等重启，立刻就能看到它在验；
      · "failed" / "cancelled" → 把同一个计划重新提交进队列
                       （source="approved" → 跑完自动重启并验证）；
      · "stuck"      → 踢一下 _next()，让队列动起来；
      · ""           → 自动判断：有 verify 就先验，否则按 plan_id 重新提交。
    """
    k = str(kind or "").strip().lower()
    pid = str(plan_id or "").strip()

    with _LOCK:
        verify = dict(_STATE.get("verify") or {}) or None
        busy = bool(_STATE.get("running"))

    # 待验证 / 自动判断：优先把它验完（这正是用户最想看到的那条链）
    want_verify = (k == "unverified") or (not k and verify and
                                          (not pid or str(verify.get("plan_id") or "") == pid))
    if want_verify:
        if not verify:
            return {"ok": False, "error": "现在没有待验证的记录（可能已经验完了）"}
        if busy:
            return {"ok": False, "error": "有任务正在跑，等它结束再验证，避免两边同时改代码"}
        try:
            _start_verify(wait_busy=True)
        except Exception as e:                           # noqa: BLE001
            logger.warning("一键拉起验证失败", exc_info=True)
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return {"ok": True, "mode": "verify",
                "message": "已启动验证：连续 2 轮全过才算通过，然后跑分比对上一版"}

    if k == "stuck" or (not k and not pid):
        with _LOCK:
            qn = len(_STATE["queue"] or [])
        if not qn:
            return {"ok": False, "error": "队列是空的，没有可拉起的任务"}
        _next()
        return {"ok": True, "mode": "queue", "message": "已让队列继续跑"}

    if not pid:
        return {"ok": False, "error": "缺少计划 id，没法重新拉起"}

    # 重新提交：会检查计划里还有没有可执行的动作，没有就明确报错
    r = submit(pid, source="approved", title="", max_n=0)
    if not isinstance(r, dict):
        r = {"ok": bool(r)}
    if r.get("ok"):
        r.setdefault("mode", "submit")
        r.setdefault("message", "已重新提交进队列（source=approved，跑完自动重启并验证）")
    return r


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

