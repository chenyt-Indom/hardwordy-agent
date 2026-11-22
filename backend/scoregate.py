# -*- coding: utf-8 -*-
"""改动后自动评分闸门（2026-11-22 用户要求）。

用户原话：
    「以后每次有修改代码的任务完成之前都得自动进行评分然后比对，
      确保每次迭代评分都比上次高」

为什么单独一个模块，而不是塞进 self_iterate：
    评分闸门要在**工具层**生效（写入成功 → 打脏标记；重启前 → 强制评分），
    而 self_iterate.py 有 9000 行、改一次风险太大。这里只做三件事：
    打标记、评分比对、给结论。

流程：
    ① 四个写入工具（write / patch / insert_after / apply）成功后
       调 mark_dirty(rel) —— 只写一个小 json，**不做评分**，零开销；
    ② 任务要「完成」（= 调 selfdev_restart 让新代码生效）时
       调 gate() —— 全量评分 → 与上一次分数比对 → 写进累计分账本；
    ③ 比上次**低** → 闸门拦住，不让你重启，并把「哪一块掉的、可疑代码在哪」
       原样回给你，要求先修好或回滚；持平或更高 → 放行。

⚠️ 逃生口（刻意留的）：
    用户在界面上点「立即重启」走的是 /api/selfdev/apply_restart，
    **不经过**这道闸门 —— 那是人的决定，工具层不该替用户拦死。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCK = threading.RLock()

# 全量评分要几十秒，同一时刻只允许一个在跑
_RUNNING = False


def _data_dir() -> str:
    """与累计分账本同一个目录（config.data("data")/iterloop）。

    ⚠️ 必须走 config，不能硬编码项目根 —— 设了 MM_DATA_DIR（Docker / 便携包
    换目录）时写回源码目录会失败，闸门状态就永远存不下来。
    """
    d = ""
    try:
        from . import config as _cfg
        d = os.path.join(_cfg.data("data"), "iterloop")
    except Exception:                                   # noqa: BLE001
        logger.debug("scoregate 取 config.data 失败，退回项目根", exc_info=True)
    if not d:
        d = os.path.join(_ROOT, "data", "iterloop")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


def _path() -> str:
    return os.path.join(_data_dir(), "scoregate.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load() -> Dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            d.setdefault("dirty", False)
            d.setdefault("files", [])
            d.setdefault("last_score", None)
            d.setdefault("last_gate", None)
            d.setdefault("gates", [])
            return d
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"dirty": False, "files": [], "last_score": None,
            "last_gate": None, "gates": []}


def _save(d: Dict[str, Any]) -> None:
    try:
        p = _path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


# ------------------------------------------------- 后台自动更新评分（防抖）
# ★ 2026-11-22 用户要求：「以后每次结束之后都得在第一时间更新评分」。
#   之前的评分**只在 selfdev_restart 闸门里跑**：如果这轮只改前端、或者任务
#   结束时没重启，分数就一直停在上一版 —— 用户看到的永远是旧分。
#   现在：写入打标记后起一个守护线程，等「安静下来」若干秒就在后台评一次，
#   结果写进 last_score.json（面板「当前版本」立刻能看到）+ 压分报告。
#
#   ⚠️ 三条硬约束（都是踩过坑才加的）：
#     ① **绝不写 scoregate.json 的 last_score 键** —— 闸门的比较基准就是它
#        （_prev_score 读它）。自动评分若改了它，闸门每次都拿「刚算的分数」
#        跟自己比 → delta 恒为 0 → 永远检测不到退步。
#     ② **不提交累计分账本** —— 账本只由重启闸门提交，否则同一批改动会被
#        记两轮进退。
#     ③ 闸门若发现后台已经算过、且之后没再改动，就**复用**那次结果，
#        不重复跑 37 秒。

_AUTO_QUIET_S = 20.0      # 最后一次写入后安静这么久才评（防抖，别边写边评）
_AUTO_MIN_GAP_S = 60.0    # 两次后台评分之间至少间隔这么久（别一直烧 CPU）
_auto_state: Dict[str, Any] = {"worker": None, "last_ts": 0.0, "runs": 0,
                               "full": None, "full_ts": 0.0}


def _auto_score_once(force: bool = False) -> Dict[str, Any]:
    """后台跑一次评分并缓存（不写账本、不动闸门基准）。

    force=True 时**不看 dirty**：用于「启动体检」—— 重启时闸门是在旧代码上
    评的分，新进程里没人改代码就永远等不到 dirty，面板会一直显示旧分。
    """
    global _RUNNING
    with _LOCK:
        d = _load()
        if not force:
            if not d.get("dirty"):
                return {"ran": False, "why": "没有待评分的改动"}
            dts = float(d.get("dirty_ts") or 0)
            if float(d.get("auto_ts") or 0) >= dts:
                return {"ran": False, "why": "这一版已经自动评过"}
        if _RUNNING:
            return {"ran": False, "why": "评分正在跑"}
        _RUNNING = True
    try:
        from . import iterloop as _il
        t0 = time.time()
        now = _il.score(full=True)
        cost = round(time.time() - t0, 1)
        try:
            from . import scorepress as _SP
            _SP.record(now, note="后台自动更新评分")
        except Exception:                               # noqa: BLE001
            logger.debug("后台压分报告失败", exc_info=True)
        try:
            _il.write_last_score(now)     # 面板「当前版本」立刻可见
        except Exception:                               # noqa: BLE001
            logger.debug("后台写 last_score 失败", exc_info=True)
        # ★ 2026-11-22 用户要求：「跑分低于原来还可以不断反思以及改进还可以写日志」。
        #   原来这条路**只评分、不比对** —— 而迭代改完代码自动重启后走的正是这里
        #   （rescore_if_stale → _auto_score_once），于是「分数掉了」既不进低分日志、
        #   也不会触发自动修复，用户看到的只是面板上数字变小了。
        #   现在补上：和上一版比，退步就记日志 + 沉淀教训 + 起自动修复（最多 3 轮）。
        try:
            _prev = d.get("last_score") or {}
            _pv = _prev.get("total")
            _tot = (now or {}).get("total")
            if _pv is not None and _tot is not None:
                _delta = round(float(_tot) - float(_pv), 2)
                if _delta < 0:
                    _rec = {"iso": _now(), "ts": time.time(), "prev": _pv,
                            "total": _tot, "delta": _delta,
                            "verdict": "regressed", "cost_s": cost,
                            "quality": (now or {}).get("quality"),
                            "ability": (now or {}).get("ability"),
                            "rebased": False, "blocked": False,
                            "files": list(d.get("files") or []),
                            "note": "后台自动评分：比上一版低 %.2f" % _delta}
                    try:
                        _r2 = _il.commit_round(
                            before=_prev, after=now,
                            note="后台自动评分（发现退步）") or {}
                        _ex = (_r2 or {}).get("explain") or {}
                        _rec["per_part"] = (_r2 or {}).get("per_part") or {}
                        _rec["suspects"] = (_ex.get("suspects") or [])[:8]
                        _rec["codes"] = (_ex.get("new_codes") or [])[:20]
                        _rec["summary"] = str(_ex.get("summary") or "")[:400]
                    except Exception:                   # noqa: BLE001
                        logger.debug("自动评分退步记账失败", exc_info=True)
                    # blocked=True：自动评分这条路上也要真的去修，不能只记不修
                    _rec["drop"] = _record_drop(_rec, True)
                    logger.info("后台自动评分发现退步：%s → %s（%+.2f），"
                                "已记低分日志并启动自动修复", _pv, _tot, _delta)
        except Exception:                               # noqa: BLE001
            logger.debug("自动评分退步比对失败（不影响评分本身）", exc_info=True)

        with _LOCK:
            d2 = _load()
            d2["auto_score"] = {"iso": _now(), "ts": time.time(),
                                "total": (now or {}).get("total"),
                                "quality": (now or {}).get("quality"),
                                "ability": (now or {}).get("ability"),
                                "cost_s": cost,
                                "files": list(d2.get("files") or [])}
            d2["auto_ts"] = float(d2.get("dirty_ts") or time.time())
            _save(d2)
        _auto_state["runs"] = int(_auto_state.get("runs") or 0) + 1
        _auto_state["last_ts"] = time.time()
        # 完整分数对象留一份在内存里：闸门（同进程）可以直接拿来比对，
        # 不用再跑一次 37 秒的全量评分。
        _auto_state["full"] = now
        _auto_state["full_ts"] = time.time()
        logger.info("后台自动评分完成：%s（%.1fs）", (now or {}).get("total"), cost)
        return {"ran": True, "total": (now or {}).get("total"), "cost_s": cost}
    except Exception as e:                              # noqa: BLE001
        logger.debug("后台自动评分失败", exc_info=True)
        return {"ran": False, "why": "%s: %s" % (type(e).__name__, e)}
    finally:
        _RUNNING = False


def _auto_worker() -> None:
    """守护线程：发现「有改动 + 已经安静下来」就在后台评一次。"""
    while True:
        try:
            time.sleep(5)
            d = _load()
            if not d.get("dirty"):
                continue
            dts = float(d.get("dirty_ts") or 0)
            if time.time() - dts < _AUTO_QUIET_S:
                continue                      # 还在写，等它写完
            if float(d.get("auto_ts") or 0) >= dts:
                continue                      # 这一版评过了
            if time.time() - float(_auto_state.get("last_ts") or 0) < _AUTO_MIN_GAP_S:
                continue
            _auto_score_once()
        except Exception:                               # noqa: BLE001
            logger.debug("后台自动评分线程异常", exc_info=True)


def ensure_auto_worker() -> None:
    """确保后台自动评分线程在跑（幂等，重复调无副作用）。"""
    with _LOCK:
        w = _auto_state.get("worker")
        if w is not None and getattr(w, "is_alive", lambda: False)():
            return
        try:
            t = threading.Thread(target=_auto_worker, name="scoregate-auto",
                                 daemon=True)
            t.start()
            _auto_state["worker"] = t
        except Exception:                               # noqa: BLE001
            logger.debug("起后台自动评分线程失败", exc_info=True)


def _score_stale() -> bool:
    """面板上的分数是不是比代码旧（改过代码但没重评）。

    ★ 为什么需要：改完代码点重启时，闸门是在**旧代码**上评的分；新进程起来后
      如果没人再动代码，就永远等不到 dirty，面板会一直显示旧分 —— 用户看到的
      是「明明修好了，分数却没变」。这里用 mtime 对比来判断，简单且不依赖额外状态。
    """
    try:
        from . import iterloop as _il
        p = os.path.join(_il._data_dir(), "last_score.json")
        if not os.path.isfile(p):
            return True
        st = os.path.getmtime(p)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for sub in ("backend", "frontend"):
            dd = os.path.join(root, sub)
            if not os.path.isdir(dd):
                continue
            for name in os.listdir(dd):
                fp = os.path.join(dd, name)
                if os.path.isfile(fp) and os.path.getmtime(fp) > st + 2:
                    return True
    except Exception:                                   # noqa: BLE001
        logger.debug("检查评分是否过期失败", exc_info=True)
    return False


def rescore_if_stale() -> Dict[str, Any]:
    """启动体检：分数比代码旧 → 后台补评一次（不阻塞启动）。"""
    if not _score_stale():
        return {"ran": False, "why": "面板分数已是最新"}

    def _go():
        try:
            _auto_score_once(force=True)
        except Exception:                               # noqa: BLE001
            logger.debug("启动补评失败", exc_info=True)

    try:
        threading.Thread(target=_go, name="scoregate-boot", daemon=True).start()
        return {"ran": True, "why": "分数比代码旧，已在后台补评"}
    except Exception as e:                              # noqa: BLE001
        return {"ran": False, "why": "起线程失败：%s" % e}


def auto_state() -> Dict[str, Any]:
    """给面板/接口看的只读快照。"""
    d = _load()
    a = d.get("auto_score") or {}
    w = _auto_state.get("worker")
    return {"alive": bool(w is not None and getattr(w, "is_alive", lambda: False)()),
            "runs": int(_auto_state.get("runs") or 0),
            "total": a.get("total"), "iso": a.get("iso"),
            "cost_s": a.get("cost_s"), "files": _live(a.get("files")),
            "fresh": (float(d.get("auto_ts") or 0) >= float(d.get("dirty_ts") or 0)
                      and a.get("total") is not None)}


# ---------------------------------------------------------------- 打脏标记

def mark_dirty(rel: str = "", note: str = "") -> None:
    """任何代码写入成功后调它。只打标记，**不评分**（零开销）。

    ⚠️ 2026-11-22 修自激死循环：评分要跑回归，而回归里有个断言会真调
    selfdev_write 写探针文件 → 又 mark_dirty → dirty_ts 刷新 → 自动评分线程
    又跑回归……实测 backend/_guard_probe_target.py 被写 172 次、每分钟 1~3 次。
    所以评分期间由 iterloop.regress_part 设 MM_NO_SCORE_MARK=1，这里直接跳过。
    """
    if os.environ.get("MM_NO_SCORE_MARK"):
        return
    try:
        with _LOCK:
            d = _load()
            d["dirty"] = True
            d["dirty_iso"] = _now()
            d["dirty_ts"] = time.time()
            # ★ 2026-11-22 修：历史测试调 mark_dirty 写进来的文件可能早被删了，
            #   留着会让「本轮改过哪些文件」和扣分归因指向不存在的文件
            #   （实测脏列表里出现过 backend/_guard_probe_target.py 这种幽灵项）。
            _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            # ⚠️ 2026-11-22 修两个真 bug：
            #   ① 原来只过滤**已有**列表，刚 append 进来的 rel 不再校验 ——
            #      于是测试调 mark_dirty("backend/_guard_probe_target.py")
            #      （文件根本不存在）时，幽灵项又回到了脏列表里（实测复现）。
            #   ② `str(...).lstrip("./")` 是**按字符集**剥离，不是按前缀：
            #      `"../foo"` → `"foo"`、`"./a"` → `"a"`，连正常路径都会被改坏。
            #      改成显式去 `./` 前缀。
            fs = list(d.get("files") or [])
            r = str(rel or "").replace("\\", "/")
            while r.startswith("./"):
                r = r[2:]
            if r and r not in fs:
                fs.append(r)
            # 统一过滤一次（含刚 append 的），不存在的文件一律剔除
            fs = [x for x in fs
                  if x and os.path.exists(os.path.join(_base, str(x).replace("/", os.sep)))]
            d["files"] = fs[-40:]
            if note:
                d["note"] = str(note)[:200]
            _save(d)
        # 每打一次标记就确保后台线程在跑（幂等）——这样「任务结束」后
        # 不用等重启，几十秒内面板上的当前版本分数就会自己更新。
        ensure_auto_worker()
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _live(fs) -> list:
    """读时过滤：剔除已经不存在的文件。

    ⚠️ 2026-11-22 修：只在 mark_dirty 写入时过滤**不够**。
    回归断言 test_selfdev_write_guard 会真的建一个探针文件再删掉，
    打标记那一刻文件**确实存在**（过滤通过），删掉之后就成了幽灵项，
    永远留在脏列表里，让「本轮改过哪些文件」和扣分归因指向不存在的文件。
    所以读的时候必须再过滤一次。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for x in (fs or []):
        s = str(x or "").replace("\\", "/")
        if s and os.path.exists(os.path.join(base, s.replace("/", os.sep))):
            out.append(s)
    return out


def pending() -> Dict[str, Any]:
    """本轮有没有还没评分的改动。"""
    d = _load()
    return {"dirty": bool(d.get("dirty")),
            "files": _live(d.get("files")),
            "since": d.get("dirty_iso") or ""}


# ---------------------------------------------------------------- 比对逻辑

# ★ 2026-11-22 用户要求「评分没有上限，超过 100 也继续往上加」——口径从
#   「0~100 归一化」改成「质量基线(≤100) + 能力跑分(无上限)」。
#   口径一变，**上次的分数和新分数不可直接相减**（会凭空多出能力段那 49 分，
#   累计分一次虚涨 49，看着像巨大进步，其实是换尺子）。
#   所以记一个口径版本：对不上就把本轮当「重建基准」，delta 记 0、不判进退。
_SCHEMA = 3      # 3: 能力分文件数排除 _tmp*/_probe*（口径修正，需重建基准）


def _prev_score(d: Dict[str, Any]):
    """上一次的**完整**评分（分项都在，才能算清是哪一块掉的）。"""
    s = d.get("last_score")
    if isinstance(s, dict) and s.get("total") is not None:
        return s
    try:
        from . import iterloop as _il
        led = _il.load_ledger()
        base = (led.get("base") or {}).get("total")
        if base is not None:
            return {"total": base, "static": None, "regress": None, "func": None,
                    "note": "上一次分数来自账本基准分（没有分项快照）"}
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return None


def _msg(rec: Dict[str, Any]) -> str:
    """给模型看的结论。拦不拦都返回一段可读文本。"""
    pv, tv, d = rec.get("prev"), rec.get("total"), rec.get("delta")
    if d is None:
        head = ("评分闸门：这次 %s 分，上一次拿不到分数，无法比对（不拦）。"
                % tv)
    else:
        head = ("评分闸门：上一次 %s → 这次 %s（%+.2f，%s）"
                % (pv, tv, d, {"improved": "进步", "regressed": "退步",
                               "flat": "持平",
                               "rebased": "评分口径升级，本轮重建基准、不计进退"}
                   .get(rec.get("verdict"), "未知")))
    lines = [head]
    # ★ 2026-11-22 总分改无上限后，必须把两段拆开说，否则模型看到「143.8 分」
    #   会误以为质量段超过 100（其实质量段永远 ≤100，超出部分来自能力跑分）。
    q = rec.get("quality")
    ab = rec.get("ability") if isinstance(rec.get("ability"), dict) else {}
    if q is not None:
        rows = [r for r in (ab.get("rows") or []) if isinstance(r, dict)
                and int(r.get("count") or 0)]
        det = "、".join("%s×%s" % (r.get("label"), r.get("count")) for r in rows)
        lines.append("拆开看：质量基线 %s/100（有上限，扣分点都在这段）"
                     " + 能力跑分 %s（无上限%s）= 总分 %s（**总分无上限**）"
                     % (q, ab.get("score"),
                        ("，" + det) if det else "", rec.get("total")))
    per = rec.get("per_part") or {}
    bits = []
    for k, zh in (("static", "静态"), ("regress", "回归"), ("func", "功能")):
        s = per.get(k)
        if s:
            bits.append("%s %s" % (zh, s))
    if bits:
        lines.append("分项：" + "；".join(bits))
    # ★ 2026-11-22 用户要求「评分机制要能汇报压低评分的部分是哪些部分」：
    #   上面「分项」只说哪一块掉了多少，这里给出**存量压分明细**——
    #   哪怕这次是持平，也明确说清「当前这版还有多少分被谁压着、先修哪条」。
    pr = rec.get("press") or {}
    if pr:
        lines.append("当前压分：共失 %s 分（%s 处），按可挽回分排序前几条："
                     % (pr.get("lost"), pr.get("items_total")))
        for it in (pr.get("top") or [])[:3]:
            if isinstance(it, dict):
                ln = it.get("line")
                loc = str(it.get("file") or "?")
                if ln:
                    loc += ":%s" % ln
                lines.append("  · %s [%s] 可挽回 %s 分"
                             % (loc, it.get("kind") or "?", it.get("cost")))
    # ★ 2026-11-22 修：suspects 是**按文件归并**的（只有 lines[]/kinds[] 数组，
    #   没有单条 line/kind），直接读 s.get("line") 会全变成「?」，模型看到
    #   「backend/foo.py ?（?）」根本定位不到行。这里做兼容读法，
    #   并优先用 codes（逐条明细，含 file/line/kind/msg）展示精确位置。
    codes = rec.get("codes") or []
    if codes:
        lines.append("新增扣分点（精确到行）：")
        for c in codes[:6]:
            if isinstance(c, dict):
                lines.append("  · %s:%s [%s] %s%s"
                             % (c.get("file") or "?", c.get("line") or "?",
                                c.get("kind") or c.get("code") or "?",
                                str(c.get("msg") or "")[:120],
                                "  ★本轮改过" if c.get("in_changed") else ""))
    sus = rec.get("suspects") or []
    if sus:
        lines.append("可疑代码：")
        for s in sus[:5]:
            if isinstance(s, dict):
                ln = s.get("line")
                if ln in (None, "", 0):
                    ls = s.get("lines")
                    if isinstance(ls, list) and ls:
                        ln = ls[0]
                kd = s.get("code") or s.get("kind")
                if not kd:
                    ks = s.get("kinds")
                    if isinstance(ks, list) and ks:
                        kd = "、".join(str(k) for k in ks[:3])
                lines.append("  · %s %s（%s）%s"
                             % (s.get("file") or "?", ln or "?",
                                kd or "?",
                                "★本轮改过" if s.get("in_changed") else ""))
            else:
                lines.append("  · %s" % s)
    dp = rec.get("drop") or {}
    if dp.get("recorded"):
        lines.append("📝 已自动记录这次退步的原因（独立低分日志 + 失败归因表），"
                     "并沉淀成教训%s。"
                     % ("：" + dp["lesson"] if dp.get("lesson") else ""))
    rp = dp.get("repair") or {}
    if rp.get("started"):
        lines.append("🔧 已自动启动修复（最多 %s 轮：反思→改代码→重评），"
                     "后台在跑，不用你手动改；进度可看 /api/iter/scoredrop。"
                     % rp.get("max_rounds"))
    elif rp.get("why"):
        lines.append("🔧 自动修复没启动（%s），请手动修。" % rp.get("why"))
    if rec.get("blocked"):
        lines.append("")
        lines.append("⛔ 这次比上次低，**先别重启**。要么把掉分的地方修好，"
                     "要么回滚这次改动，然后重新调 selfdev_restart 再评一次。")
        lines.append("（如果用户明确要求「就这样重启」，请告诉用户自己点界面上的"
                     "「立即重启」—— 那条路不经过这道闸门，是人的决定。）")
    else:
        lines.append("✅ 评分不低于上次，可以收尾。")
    return "\n".join(lines)


def _record_drop(rec: Dict[str, Any], blocked: bool) -> Dict[str, Any]:
    """退步时：记低分日志 + 沉淀教训 + 镜像归因表 + （拦住了就）起自动修复。

    用户 2026-11-22 的六条口径全在这里落地：
      · 只要比上次低就记（-0.01 也记）
      · 两边都记（独立文件 + 归因表）
      · 沉淀成长期教训，可用向量检索找类似的
      · 自动直接改代码修好再重评，反复到涨回去
    """
    out: Dict[str, Any] = {}
    try:
        from . import scoredrop as _sd
        r = _sd.record(rec) or {}
        out["recorded"] = bool(r.get("saved"))
        if r.get("lesson"):
            out["lesson"] = str(r["lesson"].get("key") or "")
        if blocked and r.get("row"):
            out["repair"] = _sd.trigger_repair(r["row"])
    except Exception:                                   # noqa: BLE001
        logger.debug("scoregate 记低分失败", exc_info=True)
    return out


def gate(force: bool = False, note: str = "",
         block_on_drop: bool = True) -> Dict[str, Any]:
    """跑一次评分并和上次比对。

    force=True      本轮没改动也评（用于基线）
    block_on_drop   比上次低时是否拦住（重启闸门用 True；纯记录用 False）
    """
    global _RUNNING
    d = _load()
    if not d.get("dirty") and not force:
        return {"ran": False, "ok": True, "blocked": False, "verdict": "skip",
                "msg": "本轮没有代码改动，跳过评分（没有可比的东西）"}
    if _RUNNING:
        return {"ran": False, "ok": True, "blocked": False, "verdict": "busy",
                "msg": "上一次评分还在跑，本次跳过（不重复评）"}
    _RUNNING = True
    try:
        from . import iterloop as _il
        prev = _prev_score(d)
        # 上次是旧口径（没有 schema 标记 / 版本不同）→ 这轮重建基准
        rebased = bool(prev) and prev.get("schema") != _SCHEMA
        # ★ 后台自动评分刚算过、之后没再改动 → 直接复用，省一次 37 秒。
        #   两个条件都要满足：① 文件里记的 auto_ts 覆盖了本批改动；
        #   ② 同进程内存里还留着那份完整分数（重启过就没了，那就重算）。
        _reuse = (float(d.get("auto_ts") or 0) >= float(d.get("dirty_ts") or 0)
                  and isinstance(_auto_state.get("full"), dict)
                  and _auto_state["full"].get("total") is not None)
        t0 = time.time()
        if _reuse:
            now = _auto_state["full"]
            cost = 0.0
            logger.info("闸门复用后台自动评分结果（%.2f），不重复跑", now.get("total"))
        else:
            now = _il.score(full=True)
            cost = round(time.time() - t0, 1)
        total = now.get("total")
        pv = (prev or {}).get("total")
        if rebased:
            # 换尺子：不判进退、delta 记 0，只把新口径的分数立成新基准
            delta = 0.0
            verdict = "rebased"
            pv = None
        elif pv is None or total is None:
            delta = None
            verdict = "unknown"
        else:
            delta = round(float(total) - float(pv), 2)
            verdict = ("improved" if delta > 0
                       else ("regressed" if delta < 0 else "flat"))
        # 写进累计分账本（累加 + 归因 + history 一条）
        res = {}
        try:
            res = _il.commit_round(before=(None if rebased else prev), after=now,
                                   note=((note or "自动评分闸门")
                                         + ("；评分口径升级，本轮重建基准、不计进退"
                                            if rebased else ""))[:200],
                                   rebased=rebased) or {}
        except Exception:                               # noqa: BLE001
            logger.debug("scoregate commit_round 失败", exc_info=True)
        jd = res if isinstance(res, dict) else {}
        ex = jd.get("explain") or {}
        rec = {"iso": _now(), "ts": time.time(), "prev": pv, "total": total,
               "delta": delta, "verdict": verdict, "cost_s": cost,
               "quality": (now or {}).get("quality"),
               "ability": (now or {}).get("ability"),
               "rebased": bool(rebased),
               "blocked": False,
               "files": list(d.get("files") or []),
               "note": str(note or "")[:200],
               "per_part": (jd.get("per_part") or {}),
               "suspects": (ex.get("suspects") or [])[:8],
               # ★ 2026-11-22 修：suspects 是**按文件归并**的（只有 lines 数组、
               #   没有单条 kind/msg），低分日志据此只能记到「某文件」而定位不到行。
               #   commit_round 的 explain.new_codes 才有逐条明细，一并带出。
               "codes": (ex.get("new_codes") or [])[:20],
               "summary": str(ex.get("summary") or "")[:400]}
        # 分项进退：优先用账本算的，没有就自己算
        if not rec.get("per_part") and prev:
            pp = {}
            for k in ("static", "regress", "func"):
                a = ((prev or {}).get(k) or {}).get("score")
                b = (now or {}).get(k) or {}
                if a is not None and b.get("score") is not None:
                    pp[k] = round(float(b["score"]) - float(a), 2)
            rec["per_part"] = pp
        # ★ 2026-11-22 用户要求：「评分机制能够汇报压低评分的部分是哪些部分
        #   然后详细记录然后上报，反思机制需要重点参考然后构建改进计划然后
        #   正常推送」。scoredrop 记的是**变化**（这轮改坏了什么），这里补的是
        #   **存量**（当前这一版还有哪些地方在压分、各能挽回多少分）——
        #   每次评分都出报告，不管进步、持平还是退步。
        try:
            from . import scorepress as _SP
            rec["press"] = _SP.record(now, note=(note or "自动评分闸门"))
        except Exception:                               # noqa: BLE001
            logger.debug("压分报告生成失败", exc_info=True)
        blocked = bool(block_on_drop and verdict == "regressed")
        rec["blocked"] = blocked
        # ★ 2026-11-22 用户要求：退步要自动记录原因、沉淀教训、并自动修复。
        #   ⚠️ 只有「真的拦住了」才起自动修复（纯记录场景别偷偷改代码）。
        if verdict == "regressed":
            rec["drop"] = _record_drop(rec, blocked)
        d["dirty"] = False
        d["files"] = []
        if isinstance(now, dict):
            now["schema"] = _SCHEMA
        d["last_score"] = now
        d["last_gate"] = rec
        gs = list(d.get("gates") or [])
        gs.append({"iso": rec["iso"], "total": total, "delta": delta,
                   "verdict": verdict, "blocked": blocked})
        d["gates"] = gs[-50:]
        _save(d)
        return {"ran": True, "ok": not blocked, "blocked": blocked,
                "prev": pv, "total": total, "delta": delta,
                "verdict": verdict, "rec": rec, "msg": _msg(rec),
                "drop": rec.get("drop"),
                "cum": jd.get("cum")}
    except Exception as e:                              # noqa: BLE001
        logger.debug("scoregate 评分失败", exc_info=True)
        return {"ran": False, "ok": True, "blocked": False, "verdict": "error",
                "msg": "评分闸门自己出错，不拦（%s: %s）" % (type(e).__name__, e)}
    finally:
        _RUNNING = False


# ---------------------------------------------------------------- 只读状态

def status() -> Dict[str, Any]:
    """给体检 / 面板看的只读快照。"""
    d = _load()
    lg = d.get("last_gate") or {}
    return {"dirty": bool(d.get("dirty")),
            "files": list(d.get("files") or []),
            "since": d.get("dirty_iso") or "",
            "last": {"iso": lg.get("iso"), "prev": lg.get("prev"),
                     "total": lg.get("total"), "delta": lg.get("delta"),
                     "verdict": lg.get("verdict"),
                     "blocked": lg.get("blocked")},
            "gates": len(d.get("gates") or []),
            # ★ 2026-11-22：后台自动评分的状态（线程活着没 / 最近一次算到多少分）
            "auto": auto_state()}
