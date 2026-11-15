# -*- coding: utf-8 -*-
"""活跃迭代引擎 —— 事件驱动的「思考 → 打磨计划 → 自动执行」闭环。

用户 2026-11-16 原话要点（决定了本模块的全部设计）：
  · 「不是时间的问题，我希望是**每次对话或任务结束**自动评估并思考，
     然后**不断完善计划，直到计划可以了**就可以执行实施了」；
  · 「每天最多 50 次」——口径是**计划数**，一个计划打磨几轮只算 1 次；
  · 「全部自动执行，改完提示我重启」；
  · 触发信号：任务失败/报错、同一件事耗时明显变长、反复改同一个文件、
     用户提的需求没做到/做偏了；
  · 「两者都要，新功能优先」；
  · 寒暄（你好/谢谢）跳过，不烧模型；
  · 界面上要能看到活跃度。

为什么要有这个模块（真实数据，不是猜的）：
  改造前 `data/self_iterate/settings.json` 里 `paused = true`，而
  `_reflect_impl` 的 2101 行明确写着「暂停期间：计划照常生成，但绝不自动改代码」
  —— 所以整套自动化其实一直是**停摆**的。旁证：
    · `rounds.json` 根本不存在（从没真正迭代过一轮）；
    · `plans.json` 只有 1 条 `trigger=manual / summary="x" / actions=[]` 的测试垃圾；
    · `selfiter`（每日反思）`last_run` 是空字符串，一次都没跑过。
  再加上原来只靠「每天 04:30 一次反思 + 每 120 分钟一次评估」，
  而且评估要「均分 < 6.5 或摩擦 ≥ 2 次」才肯改进 —— 门槛高、频率低，
  用户当然感觉不到它在思考。

三重保护（防止「活跃」变成「烧钱 + 改坏代码」）：
  ① 配额：每天最多 N 个计划，到点就跳过，绝不超支；
  ② 节流：两次触发之间至少隔 min_gap_sec，且同一时刻只跑一个思考线程；
  ③ 防死循环：任务失败触发的反思额外间隔 task_fail_gap，避免
     「改坏 → 反思 → 又改坏」自己咬自己。
"""

import json
import threading
import time

import logging
logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_BUSY = [False]          # 当前是否有一轮思考在跑
_LAST_AT = [0.0]         # 上次真正开跑的时间戳
_WEB_TICK = [0]          # 触发计数，用来决定这次要不要联网抓技术动态

# 默认配置。写进 selfloop.json 的顶层，用户/我都可以直接改文件调整。
DEFAULTS = {
    "enabled": True,        # 总开关
    "daily_plans": 50,      # 每天最多几个计划（按计划数算，打磨几轮只算 1 个）
    "min_gap_sec": 90,      # 两次触发之间至少隔多久（秒）
    "task_fail_gap": 300,   # 任务失败触发的额外间隔（秒），防自我循环
    "web_every": 3,         # 每 N 次触发才联网抓一次技术动态（其余用已入库论文）
    "slow_ratio": 2.0,      # 耗时超过历史均值这么多倍，算「明显变长」
    "trigger_keep": 50,     # 触发原因日志保留最近多少条（前端展示最近 5 条）
}

# 寒暄词。命中且整句很短 → 判定闲聊，不触发思考。
_CHIT = (
    "你好", "您好", "谢谢", "多谢", "好的", "好", "嗯", "哦", "噢", "在吗", "在不在",
    "收到", "可以", "行", "ok", "OK", "早", "晚安", "哈哈", "辛苦了", "没事", "明白",
    "知道了", "了解", "对", "是", "嗯嗯", "好的好的", "谢谢啦",
)


# ------------------------------------------------------------------ 存取

def _path():
    """状态文件路径。

    ⚠️ 2026-11-16 实测踩到：`config.data()` 返回的是 **str**，
    而本模块的 load/_save 用的是 Path 方法（.exists()/.read_text()）。
    直接返回 str 会抛 AttributeError，又被下面的 except 静默吞掉 ——
    结果是「状态从来没存下来过，配额永远显示 0/50，配额检查形同虚设」。
    所以这里必须显式包成 Path。
    """
    from pathlib import Path
    from . import config
    return Path(config.data("data", "selfloop.json"))


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return time.strftime("%Y-%m-%d")


# ---- 跨重启去重（2026-11-16 用户报「点完重启弹窗又出现」的根因修复）----
# 真实时间线（数据来自 selfloop.json / boot_gate.json）：
#   03:55:48 自动迭代改了 selfdev.py / reply_gate.py
#   03:55:53 用户点重启    03:56:04 重启完成
#   03:56:10 又触发一轮反思 → 又改代码 → pending.mark() → 弹窗又冒出来
# 原因：前端 pushedKeys 和后端 _LAST_AT 都在**内存**里，一重启全清空，
# 前端重抓最近 10 条历史对话当成新任务重推，后端节流也归零直接放行。
# 于是「改代码 → 弹窗 → 重启 → 又改代码」自己咬自己，用户永远关不掉弹窗。
# 修法：把「哪些对话已经思考过」和「上次开跑时间」都落盘。
_SEEN_KEEP = 300         # 最多记多少个对话指纹
_SEEN_TTL = 86400        # 超过一天的指纹作废（用户真重发同一句话还该再想一次）


def _sig_hash(u, a):
    """一段问答对的指纹。截断后取 md5，避免 seen 表无限膨胀。"""
    import hashlib
    s = (str(u or "")[:400] + "\u0000" + str(a or "")[:400]).encode("utf-8", "ignore")
    return hashlib.md5(s).hexdigest()[:16]


def _seen_hit(d, sig):
    """这个指纹是不是已经思考过了（在 TTL 内）。"""
    if not sig:
        return False
    seen = d.get("seen") or {}
    if not isinstance(seen, dict):
        return False
    ts = float(seen.get(sig) or 0)
    return bool(ts) and (time.time() - ts) < _SEEN_TTL


def _seen_add(d, sig):
    """记下这个指纹，顺手清掉过期的和超量的。"""
    if not sig:
        return
    now = time.time()
    seen = d.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    seen[sig] = now
    if len(seen) > _SEEN_KEEP:
        items = sorted(seen.items(), key=lambda kv: float(kv[1] or 0))[-_SEEN_KEEP:]
        seen = dict(items)
    d["seen"] = {k: v for k, v in seen.items() if now - float(v or 0) < _SEEN_TTL}


def load():
    """读状态文件。坏文件按空处理 —— 统计坏了不能连累主流程。"""
    try:
        p = _path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {}


def _save(d):
    """原子写盘。失败不影响执行（少一条统计比丢一次迭代强）。"""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _roll(d):
    """跨天把当日计数清零。返回同一个 dict（就地改）。"""
    if d.get("day") != _today():
        d["day"] = _today()
        d["plans"] = 0
        d["triggers"] = 0
        d["runs"] = 0
        d["slow_hits"] = 0
    return d


def record_trigger(reason, source):
    """记录一次触发原因（用户信号 / 定时 / 事件），供前端展示。

    痛点：selfloop.json 计数为 0 但 plans.json 有 event 计划，数据不一致，
    用户怀疑「智能体没在自动迭代」。把每次触发的原因落盘，用户能自己判断
    机制是否在跑。保留最近 N 条（N 从 config 读，默认 50）。
    """
    try:
        d = cfg()
        n = int(d.get("trigger_keep") or 50)
        if n < 1:
            n = 1
        rec = {"reason": str(reason or "")[:200],
               "source": str(source or "")[:80],
               "ts": _now()}
        arr = list(d.get("trigger_log") or [])
        arr.append(rec)
        if len(arr) > n:
            arr = arr[-n:]
        d["trigger_log"] = arr
        _save(d)
        return rec
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return None


def cfg():
    """配置 + 状态（扁平一份）。缺的键用 DEFAULTS 补齐。"""
    d = load()
    for k, v in DEFAULTS.items():
        if k not in d:
            d[k] = v
    return _roll(d)


def set_cfg(**kw):
    """改配置。只接受 DEFAULTS 里有的键，避免写进垃圾字段。"""
    d = cfg()
    changed = {}
    for k, v in kw.items():
        if k in DEFAULTS:
            d[k] = v
            changed[k] = v
    _save(d)
    return {"ok": True, "changed": changed, "cfg": {k: d[k] for k in DEFAULTS}}


# ------------------------------------------------------------------ 判定

def is_real_task(user_text, meta=None):
    """这轮对话值不值得让模型思考一次？

    返回 (bool, 原因)。寒暄/极短消息返回 False —— 用户明确要求跳过，
    否则「你好」也会烧一次模型。
    """
    u = str(user_text or "").strip()
    meta = meta or {}
    if not u:
        return False, "空消息"
    # 前端/工具上报了报错 → 一定要反思，哪怕字很短
    if meta.get("error") or meta.get("failed"):
        return True, "任务报错"
    core = "".join(ch for ch in u if ch not in "，。！？、~～!?.,;；:： \t\n\"'“”‘’")
    if not core:
        return False, "只有标点"
    if len(core) <= 6:
        for w in _CHIT:
            if core == w or core.startswith(w) or w in core:
                return False, "寒暄（%s）" % core[:8]
    if len(core) <= 2:
        return False, "太短"
    return True, "真实任务"


def _quota_left(d):
    return max(0, int(d.get("daily_plans") or 0) - int(d.get("plans") or 0))


# ------------------------------------------------------------------ 入口

def on_turn_end(user_text, assistant_text="", meta=None, source="chat"):
    """对话结束回调（前端上报摘要时调）。不阻塞：判断完就返回。"""
    try:
        d = cfg()
        if not d.get("enabled"):
            return {"ok": True, "skipped": "总开关关着"}
        ok, why = is_real_task(user_text, meta)
        if not ok:
            return {"ok": True, "skipped": why}
        # ★ 跨重启去重（2026-11-16 用户报「点完重启弹窗又出现」）：
        #   重启后前端会把最近 10 条历史对话整段重推一遍，那些**已经思考过**的
        #   绝不能再想一次，否则就是「重启 → 又改代码 → 又弹窗」的死循环。
        sig = _sig_hash(user_text, assistant_text)
        if _seen_hit(d, sig):
            return {"ok": True, "skipped": "这段对话已经思考过了"}
        return _maybe_think("对话结束（%s）" % why,
                            focus_user=user_text, source=source, sig=sig)
    except Exception as e:                              # noqa: BLE001
        logger.warning("selfloop.on_turn_end 失败：%s: %s", type(e).__name__, e)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def on_task_end(info):
    """迭代任务结束回调（iter_runner 调）。

    只在**失败**时反思：成功还反思的话，就成了
    「改代码 → 成功 → 再反思 → 再改代码」的无限循环，会把自己的配额烧光。
    """
    try:
        info = info or {}
        d = cfg()
        if not d.get("enabled"):
            return
        state = str(info.get("state") or "")
        if state not in ("failed", "error"):
            return
        reason = "迭代任务失败：%s（失败 %s 条%s）" % (
            info.get("title") or info.get("plan_id") or "",
            info.get("failed"),
            ("，" + str(info.get("error"))[:120]) if info.get("error") else "")
        _maybe_think(reason, source="task",
                     gap=float(d.get("task_fail_gap") or 300))
    except Exception as e:                              # noqa: BLE001
        logger.warning("selfloop.on_task_end 失败：%s: %s", type(e).__name__, e)


def _maybe_think(reason, focus_user="", source="chat", gap=None, sig=""):
    """三重检查都过了才真的开线程。"""
    d = cfg()
    if _quota_left(d) <= 0:
        return {"ok": True, "skipped": "今日计划数已满（%d）" % d.get("daily_plans")}
    now = time.time()
    # ★ 节流跨重启有效：_LAST_AT 是内存变量，重启后归零 → waited 变成天文数字
    #   → 节流形同虚设（实测 03:55:16 跑完，03:56:10 重启后又跑了一轮）。
    #   所以第一次用到时从落盘的 last_at 恢复。
    if not _LAST_AT[0]:
        try:
            _LAST_AT[0] = float(d.get("last_at") or 0)
        except Exception:                               # noqa: BLE001
            _LAST_AT[0] = 0.0
    need = float(gap if gap is not None else (d.get("min_gap_sec") or 90))
    with _LOCK:
        if _BUSY[0]:
            return {"ok": True, "skipped": "上一轮思考还没结束"}
        waited = now - _LAST_AT[0]
        if waited < need:
            return {"ok": True, "skipped": "距上次只过了 %.0f 秒（需 %.0f）"
                    % (waited, need)}
        _BUSY[0] = True
        _LAST_AT[0] = now

    try:
        d = cfg()
        d["triggers"] = int(d.get("triggers") or 0) + 1
        d["last_trigger"] = {"at": _now(), "reason": reason[:200], "source": source}
        d["last_at"] = now          # ★ 落盘，重启后节流还能接上
        # ★ 只在**真正开跑**时记指纹：被节流挡掉的那次不算思考过，
        #   下一轮重推还应该有机会。这样既不重复烧钱，也不会漏掉真实需求。
        _seen_add(d, sig)
        _save(d)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    # 记录触发原因（用户信号 / 定时 / 事件），供前端活跃度面板展示
    record_trigger(reason, source)

    th = threading.Thread(target=_cycle, args=(reason, focus_user, source),
                          daemon=True, name="selfloop")
    th.start()
    return {"ok": True, "started": True, "reason": reason}


def _cycle(reason, focus_user, source):
    """后台跑一轮：生成计划（自带打磨）→ 自动执行。"""
    from . import self_iterate as _si
    out = {"ok": False}
    try:
        d = cfg()
        if _quota_left(d) <= 0:
            return
        d["plans"] = int(d.get("plans") or 0) + 1
        _save(d)

        _WEB_TICK[0] += 1
        every = max(1, int(d.get("web_every") or 3))
        use_web = (_WEB_TICK[0] % every == 1)   # 每 N 次抓一次新的，其余用已入库的

        focus = (
            "触发原因：%s\n"
            "这轮用户原话：%s\n"
            "要求：① 优先想「能给用户加什么真正有用的新功能」；"
            "② 其次修掉刚才暴露的问题；"
            "③ 只做真正值得做的，不要为了凑数硬加东西。"
            % (reason, str(focus_user or "")[:400])
        )
        try:
            _si.set_stage("loop", "自动思考：%s" % reason[:60])
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

        r = _si.reflect(trigger="event", use_web=use_web, focus=focus,
                        auto_execute=True, polish=True)
        out = r or {"ok": False}

        p = (r or {}).get("plan") or {}
        pol = p.get("polish") or {}
        res = {
            "ok": bool((r or {}).get("ok")),
            "at": _now(),
            "reason": reason[:200],
            "source": source,
            "plan_id": p.get("id") or "",
            "summary": (p.get("summary") or "")[:160],
            "actions": len(p.get("actions") or []),
            "features": len(p.get("features") or []),
            "polish_score": pol.get("score"),
            "polish_rounds": pol.get("rounds"),
            # 2026-11-16 用户要求「构思不出来就只出报告」——这里记下来，
            # 活跃度面板/历史里就能区分「真改了」和「只想出了报告」。
            "report_only": bool((r or {}).get("report_only")),
            "executed": bool(((r or {}).get("auto") or {}).get("submitted")),
            "error": str((r or {}).get("error") or "")[:200],
        }
    except Exception as e:                              # noqa: BLE001
        logger.warning("selfloop 思考失败：%s: %s", type(e).__name__, e)
        res = {"ok": False, "at": _now(), "reason": reason[:200], "source": source,
               "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        _BUSY[0] = False
        try:
            d = cfg()
            if res.get("executed"):
                d["runs"] = int(d.get("runs") or 0) + 1
            d["last_result"] = res
            rec = list(d.get("recent") or [])
            rec.insert(0, res)
            d["recent"] = rec[:12]
            _save(d)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)


# ------------------------------------------------------------------ 统计

def _creative_brief():
    """读「独立创意通道」的最新结果，给前端显示「今天想到了什么新点子」。

    为什么要显示（2026-11-16 用户反馈「活跃度太低、看不到它在想什么」）：
    创新通道的产出原来只藏在 prompt 里，用户完全看不到 —— 他只能从
    「这轮改了什么」间接猜。把点子标题直接摆到界面上，活跃度才是**可感知**的。
    """
    try:
        from pathlib import Path
        from . import config
        p = Path(config.data("data", "self_iterate", "creative.json"))
        if not p.is_file():
            return {}
        d = json.loads(p.read_text(encoding="utf-8"))
        seeds = d.get("seeds") or []
        if not seeds:
            return {}
        ts = float(d.get("ts") or 0)
        age_h = (time.time() - ts) / 3600.0 if ts else 999
        return {
            "n": len(seeds),
            "titles": [str(s.get("title") or "")[:24] for s in seeds[:5]],
            "age_h": round(age_h, 1),
            "fresh": age_h < 24,
        }
    except Exception:                                   # noqa: BLE001
        logger.debug("creative brief failed", exc_info=True)
        return {}


def _health():
    """真实自动改动的健康度 —— 给用户看「放开创新后有没有搞坏东西」。

    ★ 2026-11-16 用户要求「把成功率可能下降这点彻底解决」：光降低概率不够，
      还得让用户**看得见**。这里读 actlog 的硬数据（已剔除验证用例），
      并把「该不该担心」直接判出来，而不是让用户自己看百分比猜。
    """
    try:
        from . import self_iterate as _si
        m = _si.runtime_metrics()
        rate = m.get("rate")
        total = int(m.get("total") or 0)
        if total < 12 or rate is None:
            level, tip = "unknown", "样本还少（%d 次），先跑一阵再看" % total
        elif rate >= 70:
            level, tip = "good", "自动改动稳定，放心让它大胆试"
        elif rate >= 50:
            level, tip = "warn", "有失败但都自动回滚了，已自动把每次改动改小"
        else:
            level, tip = "bad", "连续失败偏多，已把单次改动压到 4 条以内"
        return {"rate": rate, "total": total, "ok": int(m.get("ok") or 0),
                "fail": int(m.get("fail") or 0), "rollback": int(m.get("rollback") or 0),
                "fuse": int(m.get("fuse") or 0), "probe_n": int(m.get("probe_n") or 0),
                "level": level, "tip": tip}
    except Exception as e:                              # noqa: BLE001
        return {"rate": None, "level": "unknown", "tip": "读不到：%s" % e}


def stats():
    """给前端「🌱 迭代想法」顶部用的活跃度。"""
    try:
        d = cfg()
        return {
            "ok": True,
            "creative": _creative_brief(),
            "health": _health(),
            "enabled": bool(d.get("enabled")),
            "day": d.get("day"),
            "plans_today": int(d.get("plans") or 0),
            "daily_plans": int(d.get("daily_plans") or 0),
            "left": _quota_left(d),
            "triggers_today": int(d.get("triggers") or 0),
            "runs_today": int(d.get("runs") or 0),
            "busy": bool(_BUSY[0]),
            "last_trigger": d.get("last_trigger") or {},
            "last_result": d.get("last_result") or {},
            "recent": (d.get("recent") or [])[:8],
            "triggers": (d.get("trigger_log") or [])[-5:],
            "cfg": {k: d.get(k) for k in DEFAULTS},
        }
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def think_now(focus=""):
    """手动触发一次思考（前端按钮用）。不受节流限制，但仍受每日配额约束。"""
    try:
        d = cfg()
        if not d.get("enabled"):
            return {"ok": False, "error": "总开关关着"}
        if _quota_left(d) <= 0:
            return {"ok": False, "error": "今日计划数已满（%d）" % d.get("daily_plans")}
        with _LOCK:
            if _BUSY[0]:
                return {"ok": False, "error": "上一轮思考还没结束，稍等"}
            _BUSY[0] = True
            _LAST_AT[0] = time.time()
        d["triggers"] = int(d.get("triggers") or 0) + 1
        d["last_trigger"] = {"at": _now(), "reason": "手动触发", "source": "manual"}
        d["last_at"] = _LAST_AT[0]
        _save(d)
        record_trigger("手动触发", "manual")
        threading.Thread(target=_cycle, args=("手动触发", focus, "manual"),
                         daemon=True, name="selfloop-manual").start()
        return {"ok": True, "started": True}
    except Exception as e:                              # noqa: BLE001
        _BUSY[0] = False
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
