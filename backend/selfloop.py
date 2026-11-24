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
# ★ 2026-11-16 用户报「每次启动都要自动迭代一轮，134 秒」：进程启动时刻。
#   重启后节流虽然从 last_at 恢复了，但只要距上次超过 90 秒，前端一重推历史
#   就会立刻开跑。所以再给一道「启动冷却」——刚启动的头几分钟只接受用户
#   主动点击（manual），自动触发一律让路。
_BOOT_TS = [time.time()]

# 默认配置。写进 selfloop.json 的顶层，用户/我都可以直接改文件调整。
DEFAULTS = {
    "enabled": True,        # 总开关
    # ★ 2026-11-17 用户要求「省 API 开支且质量不变」。
    #   实测：daily_plans=50 一天产出 54 条待批准，用户一条都没批 ——
    #   生成速度是处理速度的几十倍，多出来的 token 纯浪费。
    #   降到 12：单条计划的质量（打磨轮数/评分门槛）完全不变，只是少想一些。
    #   想多要就改这个数或点「立即反思」（手动永远放行）。
    "daily_plans": 12,      # 每天最多几个计划（按计划数算，打磨几轮只算 1 个）
    # ★ 2026-11-23 用户要求「反思节流 90 → 300 秒」：
    #   90 秒的间隔意味着同一次对话里改两句就又能触发一轮反思，实测一天能烧出
    #   几十轮（触发日志里 12:19:51 一行三条就是证据）。拉长到 300 秒（5 分钟）
    #   后，5 分钟内的自动触发一律让路；用户想立刻跑就点「立即反思」
    #   （think_now / source="manual" 不经过这道节流，永远放行）。
    "min_gap_sec": 300,     # 两次触发之间至少隔多久（秒）
    "task_fail_gap": 300,   # 任务失败触发的额外间隔（秒），防自我循环
    "web_every": 3,         # 每 N 次触发才联网抓一次技术动态（其余用已入库论文）
    "slow_ratio": 2.0,      # 耗时超过历史均值这么多倍，算「明显变长」
    "trigger_keep": 50,     # 触发原因日志保留最近多少条（前端展示最近 5 条）
    "boot_gap_sec": 180,    # 启动冷却：进程刚起来这段时间内不自动触发（手动不受限）
    # ★ 2026-11-17：待批准积压闸门。真实数据：用户有 54 条待批准没处理，而每轮
    #   反思还在往里加 5~10 条 —— 生成速度远超处理速度，这些 token 是纯浪费。
    #   积压 ≥ 这个数时**自动触发**直接跳过（手动点「立即反思」永远放行）。
    #   设 0 = 关闭闸门。
    "backlog_gate": 30,
    # ★ 2026-11-24 死锁保险：积压超过阈值时，若距上次真正开跑已经 ≥ 这么多小时，
    #   就**放行一次**（一天最多一次）。没有这道保险，闸门会把对话触发永久堵死 ——
    #   实测 11-23/11-24 定时任务仍在生成 168 条待批准，而 chat 触发一条都进不来。
    "backlog_free_hours": 24,
}

# ★ 2026-11-23：老默认值的一次性迁移。
#   为什么必须迁移（不是只改 DEFAULTS 就够）：`cfg()` 的补默认逻辑是
#   「键**不存在**才补」，而真实的 `data/selfloop.json` 里早就写死了
#   `"min_gap_sec": 90`（历史上第一次 cfg()/set_cfg 就把默认值落盘了）。
#   所以光把 DEFAULTS 改成 300，跑起来读到的仍然是 90 —— 用户要求的
#   节流压根没生效。这里给旧值 90 做一次性升级（写进 schema，只做一次），
#   之后用户自己在界面上调成任何值（包括 90）都不会再被覆盖。
_LEGACY_DEFAULTS = {"min_gap_sec": 90}   # 旧版默认值 → 需要升级掉的存量值
_SCHEMA = 2                              # 配置结构版本（1 = 未标记的老数据）


def _migrate(d):
    """就地升级老落盘配置。返回 True 表示改动过（需要写回）。

    只做「存量旧默认值 → 新默认值」，绝不碰用户显式调过的其它值：
      · schema 已经是当前版本 → 直接返回，绝不重复迁移；
      · 存量值等于旧默认值 → 升级成新默认值；
      · 其它值（用户自己调过）→ 原样保留。
    """
    changed = False
    try:
        ver = int(d.get("schema") or 1)
    except Exception:                                   # noqa: BLE001
        ver = 1
    if ver >= _SCHEMA:
        return False
    for k, old in _LEGACY_DEFAULTS.items():
        try:
            if float(d.get(k) or 0) == float(old):
                d[k] = DEFAULTS.get(k, old)
                changed = True
        except Exception:                               # noqa: BLE001
            logger.debug("migrate %s 失败", k, exc_info=True)
    d["schema"] = _SCHEMA
    return True


# 积压条数的缓存：每次对话结束都读一遍 plans.json 没必要（几百 KB）。
_BACKLOG_CACHE = {"at": 0.0, "n": 0}
_BACKLOG_TTL = 60.0


def _backlog():
    """待批准动作总数（改进 + 新功能）。失败按 0 处理 —— 统计坏了不能挡住反思。"""
    now = time.time()
    if now - float(_BACKLOG_CACHE.get("at") or 0) < _BACKLOG_TTL:
        return int(_BACKLOG_CACHE.get("n") or 0)
    n = 0
    try:
        from . import self_iterate as _si
        n += int((_si.list_pending_actions() or {}).get("total") or 0)
        n += int((_si.list_pending_features() or {}).get("total") or 0)
    except Exception:                                   # noqa: BLE001
        logger.debug("backlog 统计失败", exc_info=True)
        n = 0
    _BACKLOG_CACHE["at"] = now
    _BACKLOG_CACHE["n"] = n
    return n

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
    """配置 + 状态（扁平一份）。缺的键用 DEFAULTS 补齐。

    ★ 2026-11-23：补默认之后再过一遍 `_migrate()` —— 老落盘文件里
      `min_gap_sec` 是显式写着的 90，光补默认改不动它（详见 _migrate 注释）。
      迁移只发生一次（写 schema），之后用户怎么调都不再被覆盖。
    """
    d = load()
    for k, v in DEFAULTS.items():
        if k not in d:
            d[k] = v
    _roll(d)
    if _migrate(d):
        _save(d)
    return d


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


def request_next(reason="上一个功能验证通过，接着推荐下一个功能"):
    """连续迭代的「接力棒」：一个功能做完并验证通过后，让引擎接着想下一个。

    用户 2026-11-19 要求：「推荐新功能实现一件之后，就停下来，我需要可以连续
    实现多种功能连续迭代」。原来 on_task_end 只在**失败**时反思，成功了就停，
    所以永远只能做一件。

    source="chain" 只豁免「启动冷却 / 最小间隔」（链式推进本来就该紧接着跑），
    配额、积压、忙碌这几道闸门照样管 —— 不会失控烧 token。
    """
    try:
        d = cfg()
        if not d.get("enabled"):
            return {"ok": True, "skipped": "总开关关着"}
        if not d.get("chain_mode", True):
            return {"ok": True, "skipped": "连续模式关着"}
        return _maybe_think(reason, source="chain", gap=0)
    except Exception as e:                              # noqa: BLE001
        logger.warning("selfloop.request_next 失败：%s: %s", type(e).__name__, e)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _skip_note(why, source=""):
    """记一笔「这轮为什么没跑」，落盘供面板/排查用，然后返回标准 skipped 结构。

    ★ 2026-11-24 实测踩到的坑：跳过原因原来只是**内存里的返回值，从不落盘**。
      所以「11-16 之后对话触发再没开跑」这件事，事后完全查不出是谁挡的 ——
      上一轮我甚至把原因读错成「backlog=0」，真因是 backlog 168 条堵死闸门。
      现在每次跳过都写一条（保留最近 40 条），谁挡的、挡了多少一眼可见。
    """
    try:
        # ★ 读-改-写必须进锁：前端每 20 秒可能并发上报多条信号，
        #   两个线程同时「读旧数组 → 各自 append → 各自写盘」会丢记录。
        with _LOCK:
            d = cfg()
            arr = list(d.get("skip_log") or [])
            arr.append({"at": _now(), "why": str(why or "")[:160],
                        "source": str(source or "")[:40]})
            d["skip_log"] = arr[-40:]
            _save(d)
    except Exception:                                   # noqa: BLE001
        logger.debug("skip_log 写入失败（不影响主流程）", exc_info=True)
    return {"ok": True, "skipped": why}


# 已排队的补跑：key → True。防止同一条需求反复排定时器。
_CATCHUP = {}


def _schedule_catchup(delay, reason, focus_user="", source="chat", sig=""):
    """被「启动冷却」挡掉的那次触发，冷却结束后**自动补跑一次**（用户 2026-11-24 选 A）。

    为什么必须补跑：冷却挡掉之后，这条真实需求只有等「用户再发一句话」才会被
    前端重推（前端确实会撤回指纹）。用户不发消息，它就被永久丢弃了 ——
    这正是「挡掉即丢弃」的问题，也是用户要求修的那一条。
    """
    key = sig or ("%s|%s" % (source, str(reason)[:60]))
    with _LOCK:
        if key in _CATCHUP:
            return False
        _CATCHUP[key] = True
    try:
        delay = max(1.0, float(delay))
    except Exception:                                   # noqa: BLE001
        delay = 5.0

    def _fire():
        with _LOCK:
            _CATCHUP.pop(key, None)
        try:
            _maybe_think("补跑（冷却结束）：" + str(reason)[:140],
                         focus_user=focus_user, source=source, sig=sig)
        except Exception:                               # noqa: BLE001
            logger.warning("冷却后补跑失败", exc_info=True)

    try:
        t = threading.Timer(delay, _fire)
        t.daemon = True
        t.start()
    except Exception:                                   # noqa: BLE001
        with _LOCK:
            _CATCHUP.pop(key, None)
        logger.warning("补跑定时器没排上", exc_info=True)
        return False
    return True


def _maybe_think(reason, focus_user="", source="chat", gap=None, sig=""):
    """三重检查都过了才真的开线程。"""
    d = cfg()
    # ★ 2026-11-17：原来这里不看 source —— 配额一满，用户点「立即反思」
    #   也会被「今日计划数已满」挡掉，等于手动按钮失效。
    #   下面 backlog_gate 那道闸门本来就写了「手动永远放行」，这里保持一致。
    if _quota_left(d) <= 0 and source != "manual":
        return _skip_note("今日计划数已满（%d）" % d.get("daily_plans"), source)
    # ★ 2026-11-24 专家意见（L3 三缺之三：没有预算）：
    #   daily_plans 只管「计划数」，且数散在自己文件里。这里再过一道统一预算账本，
    #   让「计划 / 改动文件 / 自动重启」三类消耗进同一本账，面板和日志能一眼看到。
    #   手动触发（source="manual"）永远放行，和上面几道闸门口径一致。
    try:
        from . import evolve_budget as _eb
        _gb = _eb.gate("plans", 1, manual=(source == "manual"))
        if not _gb.get("ok"):
            return _skip_note(_gb.get("why"), source)
    except Exception:                                   # noqa: BLE001
        logger.debug("进化预算闸门失败（不拦，按原逻辑继续）", exc_info=True)
    # ★ 2026-11-17 用户问「为什么这么多条待批准」：根因就是**生成远快于处理**。
    #   一次反思产 5~10 条动作 + 2~3 个新功能，而用户一天批不了几条。
    #   队列已经很深时继续想新的，token 是纯浪费（这些计划只会越堆越多）。
    #   所以生成前先看队列深度，积压超阈值就跳过 —— 手动触发永远放行。
    try:
        gate = int(d.get("backlog_gate") or 0)
    except Exception:                                   # noqa: BLE001
        gate = 0
    if gate > 0 and source != "manual":
        backlog = _backlog()
        if backlog >= gate:
            # ★ 2026-11-24 死锁修复（实测数据）：闸门本意是「省 token」，但它把
            #   **对话触发永久堵死** —— 11-23/11-24 定时任务照样生成了 168 条
            #   待批准，而 chat 触发一条都进不来。积压只增不减 → 永远解不了封。
            #   现在加一道保险：距上次真正开跑 ≥ backlog_free_hours（默认 24h）
            #   就放行一次（一天最多一次，token 成本可忽略），并记进 skip_log。
            try:
                free_h = float(d.get("backlog_free_hours", 24) or 0)
            except Exception:                           # noqa: BLE001
                free_h = 24.0
            _t = time.time()
            try:
                _last = float(d.get("last_at") or 0)
            except Exception:                           # noqa: BLE001
                _last = 0.0
            stale_h = ((_t - _last) / 3600.0) if _last else 1e9
            # ★ 2026-11-24 预演实测踩到的 bug：原来这里先把「今天已放行」写盘，
            #   结果名额被**冷却期内**的那次触发白白吃掉（它后面才被 boot_gap 挡住），
            #   等补跑真正到来时 freed_today 已经是 True → 补跑又被积压挡住。
            #   修法：名额不提前占，直接用 last_at 限流 —— 放行并**真开跑**时
            #   last_at 会更新，之后 free_h 小时内自然不再放行；没开跑就不消耗名额。
            if free_h > 0 and stale_h >= free_h:
                _skip_note("积压 %d 条、但已停摆 %.0f 小时：死锁保险放行一次"
                           % (backlog, stale_h), source)
            else:
                return _skip_note(
                    "待批准已积压 %d 条（阈值 %d）：先把已有的批掉，"
                    "再想新的（想立刻跑请点「立即反思」）" % (backlog, gate), source)
    now = time.time()
    # ★ 2026-11-16 用户报「每次启动都要自动迭代一轮，134.9 秒」：
    #   进程刚起来时，前端可能还在重推历史、队列也可能在补跑，这时候自动开跑
    #   会让用户觉得「一开机它就自己忙上了」。所以给一段冷却期，只接受手动点击。
    # ★ 2026-11-19：chain（连续迭代接力）要豁免启动冷却 ——
    #   每做完一个功能都会重启，如果链式推进也被「刚启动 180 秒」挡住，
    #   那就永远只能做完第一个，正是用户抱怨的「实现一件就停」。
    if source not in ("manual", "chain"):
        try:
            boot_gap = float(d.get("boot_gap_sec", 180) or 0)
        except Exception:                               # noqa: BLE001
            boot_gap = 180.0
        since_boot = now - _BOOT_TS[0]
        if boot_gap > 0 and since_boot < boot_gap:
            # ★ 用户 2026-11-24 选 A：冷却挡掉的**不丢弃**，冷却一结束就自动补跑。
            left = boot_gap - since_boot
            ok_q = _schedule_catchup(left + 2.0, reason, focus_user, source, sig)
            return _skip_note("刚启动 %.0f 秒（冷却 %d 秒，%s）"
                              % (since_boot, int(boot_gap),
                                 "已安排 %.0f 秒后补跑" % (left + 2.0) if ok_q
                                 else "补跑已在队列里"), source)
    # ★ 节流跨重启有效：_LAST_AT 是内存变量，重启后归零 → waited 变成天文数字
    #   → 节流形同虚设（实测 03:55:16 跑完，03:56:10 重启后又跑了一轮）。
    #   所以第一次用到时从落盘的 last_at 恢复。
    if not _LAST_AT[0]:
        try:
            _LAST_AT[0] = float(d.get("last_at") or 0)
        except Exception:                               # noqa: BLE001
            _LAST_AT[0] = 0.0
    # ★ 2026-11-23 节流 90 → 300 秒。兜底值也要跟着改 —— 原来这里写死 `or 90`，
    #   配置里一旦是空/0/坏值就退回 90，等于把用户要求的 5 分钟节流绕过去了。
    #   现在兜底取 DEFAULTS 里的同一份值，只留一个真源。
    # ★ 手动触发（source="manual"）永远放行：用户点「立即反思」就是「我现在就要想」，
    #   被节流挡掉等于按钮失效（配额/积压闸门同样对 manual 放行，这里保持一致）。
    if source == "manual":
        need = 0.0
    else:
        need = float(gap if gap is not None
                     else (d.get("min_gap_sec") or DEFAULTS["min_gap_sec"]))
    # ★ 2026-11-24：检查与占位必须在**同一把锁**里完成。
    #   拆成两段 with 时，两个线程可能同时通过检查再各自置位 → 两轮反思并行，
    #   各自改代码、互相覆盖（这正是「改完又改回去」的典型来源）。
    _why = ""
    with _LOCK:
        if _BUSY[0]:
            _why = "上一轮思考还没结束"
        else:
            waited = now - _LAST_AT[0]
            if waited < need:
                _why = "距上次只过了 %.0f 秒（需 %.0f）" % (waited, need)
            else:
                _BUSY[0] = True
                _LAST_AT[0] = now
    if _why:
        return _skip_note(_why, source)

    try:
        d = cfg()
        d["triggers"] = int(d.get("triggers") or 0) + 1
        d["last_trigger"] = {"at": _now(), "reason": reason[:200], "source": source}
        d["last_at"] = now          # ★ 落盘，重启后节流还能接上
        # ★ 2026-11-24：记一笔「计划」消耗 —— 只在**真的开跑**时记，
        #   被节流 / 积压 / 冷却挡掉的都不算（和 last_at / 指纹同口径）。
        try:
            from . import evolve_budget as _eb2
            _eb2.spend("plans", 1, why=reason, manual=(source == "manual"))
        except Exception:                                # noqa: BLE001
            logger.debug("记计划预算失败（不影响本轮）", exc_info=True)
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

        # ★ 2026-11-16 用户要求：「信任模式关闭时，自动自我迭代（包括任务进行中
        #   发现的问题和可优化的内容）也不要自动执行，全部放到待批准，并说明
        #   原因和解决方案」。
        #   这里原来写死 auto_execute=True —— 等于绕过了信任模式，任务一结束就
        #   自动改代码，用户在「待批准」里根本看不到这些计划。
        #   改成 None：由 reflect 内部按 settings.trust_mode 决定。
        #   信任模式关 → 只生成计划 + 待批准（带原因/改法）；开 → 照旧自动执行。
        r = _si.reflect(trigger="event", use_web=use_web, focus=focus,
                        auto_execute=None, polish=True)
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
        # ★ 2026-11-16 用户要求：「在每次任务中都要时刻思考」新功能。
        #   放在 finally 里 —— 哪怕这轮反思失败/超时，新功能的构思照样跑。
        #   真实成本由 featradar 自己控：距上次 20 分钟内不重复想、待批准堆到
        #   12 条就先停，所以不会每轮都烧一次调用。
        try:
            from . import featradar as _fr
            fr = _fr.think(reason=reason)
            if fr.get("added"):
                res["feat_added"] = int(fr.get("added") or 0)
                res["feat_titles"] = [x.get("title") for x in (fr.get("items") or [])]
        except Exception:                               # noqa: BLE001
            logger.debug("featradar tick failed", exc_info=True)
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
