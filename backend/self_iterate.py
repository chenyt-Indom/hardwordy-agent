"""自我迭代（self-iterate）—— AI 主动反思、看新技术、形成计划、等用户批准后动手。

2026-11-15 用户要求（原话要点）：
  · 前端一个「AI 迭代想法」按钮，点开能看到：AI 对自己的反思、改进方法、
    对用户工作方式的想法、可以增加的功能；
  · 能实时从网上各大平台获取最新技术论文与新闻，主动思考怎么用到本项目；
  · AI 可以自主决定要改什么，但**动手前必须给提醒**，用户在抽屉里点批准/拒绝；
  · 批准后弹备注框，用户可以补充建议或微调；
  · 逐条批准、整批批准都要支持；
  · 归档历史 + 显示「建议采纳率」（提了多少条、实际做了多少条）；
  · 每次改动前自动备份，可一键回滚。

设计上的三条硬约束（自己定的，避免这个功能变成"乱改代码"的入口）：
  1. 只允许改 backend/ 与 frontend/ 下的 .py/.js/.css/.html；
     data/ models/ sessions/ image_library/ secrets 一律拒绝。
  2. 每个动作在应用前把原文件备份到 data/self_iterate/backups/<tag>/，
     应用后做语法校验，失败自动回滚。
  3. **没有用户批准的动作绝不执行**（approve 之外没有第二条执行入口）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import logging
logger = logging.getLogger(__name__)

from . import jsonfix as _jsonfix

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "self_iterate"
_BACKUPS = _DATA / "backups"
_PLANS = _DATA / "plans.json"
_SIGNALS = _DATA / "signals.json"
_JOURNAL = _ROOT / "data" / "selfdev_journal.jsonl"

# 允许 AI 自主修改的范围（白名单），其余一律拒绝
_ALLOWED_PREFIX = ("backend/", "frontend/")
_ALLOWED_EXT = (".py", ".js", ".css", ".html")
_FORBIDDEN_PARTS = ("data/", "models/", "sessions/", "image_library/",
                    "secrets", ".env", ".git/", "__pycache__")

_MAX_FILES_PER_ACTION = 3
# 功能类动作（origin=feature）的文件数上限。新功能 = 新模块 + 路由 + 前端 + 断言，
# 4 个起步；限 3 会让「自主加新功能」这件事永远做不成。
_MAX_FILES_FEATURE = 6
# ---- 运行状态（2026-11-15 用户要求）----
# 用户原话：「我能不能看到他的状态，比如说显示『正在自动自迭代』」。
# 反思是同步阻塞的（20~40 秒：抓网 + 论文评估 + LLM），以前中间只有一条静态
# 提示，用户分不清是卡住还是在跑，后台定时触发时更是完全看不到。
# 这里维护一份进程内的运行状态，前端每 1.5 秒拉一次，显示阶段 + 已用秒数。
_RUN = {
    "running": False,
    "stage": "idle",
    "detail": "",
    "trigger": "",
    "started": 0.0,
    "ended": 0.0,
    "plan_id": "",
    "error": "",
}
_RUN_LOCK = threading.Lock()

# ★ 2026-11-15 新增：**同一时刻只允许一个自动执行**。
#   实测踩到：信任模式下多个计划会并发执行（actlog 里 ZZVERIFY_EXEC 与
#   ZZCHAIN_212234 的失败时间只差 14 秒），各自备份、各自回滚、各自写盘，
#   互相踩踏 —— 表现就是「明明改对了却被回滚」「断言文件凭空消失」。
#   用 RLock 而不是 Lock：execute_approved 会为「残留问题」递归调自己
#   （_depth>0），同一线程重入不能自己锁死。
_EXEC_LOCK = threading.RLock()


def _exec_guard(fn):
    """给 execute_approved 加进程级互斥：抢不到锁就明确拒绝，不排队硬等。"""
    def _wrapped(*args, **kw):
        # ★ 2026-11-16：原来是写死的四个位置参数，导致 execute_approved 新增的
        #   force 参数一传就报「unexpected keyword argument」。改成透传。
        _depth = kw.get("_depth", 0)
        if len(args) >= 4:
            _depth = args[3]
        if _depth > 0:
            # 递归接续下一轮：锁已在本线程手里，直接进
            return fn(*args, **kw)
        if not _EXEC_LOCK.acquire(blocking=False):
            return {"ok": False, "done": 0, "total": 0, "results": [],
                    "error": "已有一个自动执行在进行中，本次跳过（避免并发改坏代码）"}
        try:
            return fn(*args, **kw)
        finally:
            _EXEC_LOCK.release()
    _wrapped.__name__ = getattr(fn, "__name__", "execute_approved")
    _wrapped.__doc__ = getattr(fn, "__doc__", "")
    return _wrapped

# 反思 prompt 的硬约束（2026-11-15 用户要求）：
# 对应两个反复出现的问题 ——「只诊断不修复」和「同一诉求反复催」。
# 直接拼进反思 prompt，让模型每轮都必须产出可落地的代码 patch。
_REFLECT_HARD_RULES = (
    "【本轮硬约束】\n"
    "1. 本轮 actions 至少有一条必须是代码 patch（改 backend/ 或 frontend/ 下的文件），"
    "只做诊断、只读代码、只写分析都不算完成本轮；诊断/读代码只能作为 patch 的附属步骤。\n"
    "2. 多点任务强制串行：一次只推进一个点，做完一个点再动下一个；"
    "每个点必须附上验证命令（如 python -m py_compile <file> 或对应测试命令），"
    "没有验证命令的动作视为未完成。\n"
    "3. 同一个诉求不要反复催：如果上一轮已经提过但没落地，本轮要么直接给出 patch，"
    "要么明确说明卡在哪一步，不允许再重复一遍同样的建议。\n"
    "4. 每条 patch 动作必须带 verify 字段（可执行的验证命令字符串），"
    "缺少 verify 的 patch 视为无效动作，不得计入本轮产出。\n"
    "5. 若任务包含多个点（如脖子/嘴巴/口型三点），本轮只允许生成其中一个点的动作，"
    "其余点留到后续轮次，禁止在同一轮里并列推进多个点。\n"
)

_STAGES = {
    "fetch": "抓取技术动态（arXiv / GitHub / HN / 中文媒体）",
    "paper": "论文入库 · 向量召回 · 四维评估",
    "think": "模型思考中（读反思素材 → 出方案）",
    "decompose": "拆解并列需求（已有/待建/本轮做）",
    "plan": "整理改进方案",
    "exec": "信任模式自动改代码",
    "audit": "改完自动自检",
    "done": "完成",
    "error": "出错",
    "idle": "空闲",
}


# 免确认授权语句（2026-11-15 用户要求）：
# 用户说「权限给到最高」「想怎么搞就怎么搞」「你看着办」这类话时，
# 视为已经批准，跳过 ask_user 直接进入执行并输出计划。
_AUTO_APPROVE_KEYWORDS = (
    "权限给到最高",
    "想怎么搞就怎么搞",
    "你看着办",
)


def is_auto_approve(text):
    """判断用户这句话是否属于「免确认授权」，命中则跳过 ask_user。"""
    s = str(text or "")
    if not s:
        return False
    return any(k in s for k in _AUTO_APPROVE_KEYWORDS)


def run_status():
    """给前端轮询：现在在做什么、已经跑了多久、上一轮什么结果。"""
    with _RUN_LOCK:
        d = dict(_RUN)
    st, en = d.get("started") or 0.0, d.get("ended") or 0.0
    if d.get("running") and st:
        d["elapsed"] = round(time.time() - st, 1)
    elif st and en:
        d["elapsed"] = round(en - st, 1)
    else:
        d["elapsed"] = 0.0
    d["stage_label"] = _STAGES.get(d.get("stage") or "", d.get("stage") or "")
    # 开关类功能生效自证：最近一次自动执行时间 + 累计次数 + 最近结果。
    # 用户反复追问「有没有在跑」，这里直接贴真实数据，不再靠猜。
    try:
        logs = list_actlog(limit=200)
        auto_logs = [x for x in logs if x.get("auto")]
        d["auto_total"] = len(auto_logs)
        if auto_logs:
            last = auto_logs[0]
            d["auto_last_ts"] = last.get("ts") or ""
            d["auto_last_result"] = last.get("result") or ""
            d["auto_last_title"] = last.get("title") or ""
        else:
            d["auto_last_ts"] = ""
            d["auto_last_result"] = ""
            d["auto_last_title"] = ""
    except Exception:                                   # noqa: BLE001
        d["auto_total"] = 0
        d["auto_last_ts"] = ""
        d["auto_last_result"] = ""
        d["auto_last_title"] = ""
    return d


def set_stage(stage, detail=""):
    """推进当前阶段。detail 是给人看的一句话（如「已抓 30 条」「评了 6 篇」）。"""
    with _RUN_LOCK:
        if not _RUN.get("running"):
            return
        _RUN["stage"] = stage
        if detail:
            _RUN["detail"] = str(detail)[:140]


def _run_begin(trigger):
    with _RUN_LOCK:
        _RUN.update({"running": True, "stage": "fetch", "detail": "",
                     "trigger": str(trigger or "manual"),
                     "started": time.time(), "ended": 0.0,
                     "plan_id": "", "error": ""})


def _run_end(err="", plan_id=""):
    with _RUN_LOCK:
        ok = not err
        _RUN.update({"running": False,
                     "stage": ("done" if ok else "error"),
                     "ended": time.time(),
                     "error": str(err or "")[:300],
                     "plan_id": str(plan_id or "")})
_SIGNAL_KEEP = 60

# ---- 设置（2026-11-15 用户要求）----
# 用户原话：「再设置一个信任模式的开关，开启就可以让模型不用通过用户批准，
# 自动进行迭代，然后改完之后就提示重启」。
# 另外要求智能体能主动检查自己的代码、任务没达到预期时能自己怀疑是代码问题。
_SETTINGS = _DATA / "settings.json"
_AUDITS = _DATA / "audits.json"
# 用户最后一次**真实聊天**的时间（抽空自检靠它判断"现在是不是空档"）。
# ⚠️ 不能拿 sessions 文件的 mtime 代替：定时任务自己也往 session 里写消息，
#    那会把"最后活动"一直刷新成刚刚，抽空自检就永远等不到空档。
_ACT = _DATA / "activity.json"

_DEFAULT_SETTINGS = {
    # 全局暂停闸（2026-11-15 用户要求）：「自动化可以增加一个暂停按钮，
    # 再次按下则继续，正常开启绿灯，暂停暗灯」。
    # True = 所有**自动**触发全部停手（定时反思 / 定时自检 / 抽空自检 /
    # 自动评估 / 信任模式下的自动执行）；用户手动点的操作仍然照常跑，
    # 否则「暂停」会变成「整个功能不可用」。
    "paused": False,
    # 信任模式：反思产出的动作**不再等批准**，直接执行。
    # 用户明确选了「全部自动，包括高风险（改 main.py、一次改多个文件），
    # 但高风险会单独在前端提示一声」。
    "trust_mode": False,
    # ---- 计划打磨（2026-11-16 用户要求）----
    # 「不断完善计划，直到计划可以了就可以执行实施了」，
    # 判定标准：「模型自评 8 分以上，或打磨满 3 轮就执行」。
    "polish_plan": True,
    "polish_target": 8,
    "polish_rounds": 3,
    # 改完代码后：用户选的是「弹提示 + 给一个一键重启按钮」，不自动重启。
    "auto_restart": False,
    # 每次改完代码自动跑一次静态自检（用户要求「每次代码被改完就自动跑」）。
    "audit_after_change": True,
    # 每天定时自检一次（用户要求「+ 每天一次」）。
    "audit_daily": True,
    # 自检时是否让 LLM 做深度逻辑审查（会花 token，默认关，手动点才用）。
    "deep_audit": False,
    # 任务结果没达到预期时，自动触发一次「是不是代码问题」的诊断。
    "diagnose_on_fail": True,
    # 信任模式下一轮最多自动执行几条动作 —— 防止一次改太多没法收拾。
    # 2026-11-15 用户要求新功能也要自动落地。一轮典型是「2~4 条 bug 修复 +
    # 2~4 条新功能」，上限 6 会把排在后面的功能动作整批截掉，所以提到 10。
    "max_auto_actions": 10,
    # 同文件改动冷却（2026-11-15）：同一文件被自动改了 N 次**且上次失败**时，
    # 先停手换思路。真实数据：self_iterate.py 被改 16 次、main.py 14 次，
    # 整体成功率只有 76~79% —— 反复改同一处就是在同一个坑里越挖越深。
    "change_cool_limit": 5,
    # ---- 体验评估（2026-11-15 追加）----
    # 用户要求「主动评估任务完成效果，闲聊里也要能读出体验」。
    # 启发式每轮都算（零成本）；LLM 批量评估按下面两个开关控制频率。
    "evaluate_auto": True,          # 检测到明显不满/报错时，自动跑一次 LLM 评估
    "evaluate_periodic": True,      # 定时（默认 2 小时）跑一次批量评估
    "evaluate_every_min": 120,      # 定时评估间隔（分钟）
    "evaluate_min_turns": 4,        # 至少攒够几轮才评估，避免重复评同一批
    # ---- 抽空自检（2026-11-15 追加）----
    # 用户原话：「自检这东西能不能也是和自动迭代一样，自从抽空检测」。
    # 每天 05:10 那次是"保底"；这里做的是"趁用户不聊的时候顺手查一遍"。
    "idle_audit": True,             # 抽空自检总开关
    "idle_gap_min": 10,             # 用户安静满这么多分钟才算空档（避免打断对话）
    "idle_every_min": 180,          # 两次抽空自检至少间隔（分钟），默认 3 小时
    # ---- 行为级回归（2026-11-15 追加）----
    # 用户要求「彻底解决逻辑 bug 查不出来」的问题。
    # 静态检查只能看名字/结构，逻辑错要靠真跑断言 —— 每次改完代码自动跑一遍，
    # 失败就回滚（regress.py）。
    "regress_after_change": True,
    # ---- 自检防重复触发（2026-11-15 追加）----
    # 实测事故：一个卡死的测试脚本每分钟调一次自检，配合信任模式连续建了
    # 3 个计划并自动改代码 —— 用户看到的就是「程序被反复修改」。
    # 同一来源在这么多秒内重复触发 → 直接跳过，不建计划。
    "audit_cooldown_sec": 60,
    # ---- 单轮内自动收敛（2026-11-15 追加）----
    # 一次执行内最多接续几轮，把上一轮没修完的同类问题接着修掉。
    # 实测：探针 9 处问题，第一轮只修掉 6 处，靠它自动接续到 0，不用人再点一次。
    # 设 0 = 关掉自动接续（每轮都要人手动触发）。
    "converge_rounds": 2,
    # ---- 新功能自修轮数（2026-11-15 追加）----
    # 用户要求「加入之后要自动验证是否完全正常，并反复改到完全正常为止」。
    # 验收断言不过时，带着失败输出重改，最多这么多轮；仍不过才回滚。
    # 设 0/1 = 只验一次不改（回到旧行为）。
    "feature_repair_rounds": 3,
}


# ---------------------------------------------------------------- 存储层

def _ensure():
    try:
        _BACKUPS.mkdir(parents=True, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _read_json(path, default):
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return default


def _write_json(path, obj):
    _ensure()
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings():
    """读设置。缺字段一律回落到默认值，避免旧文件少字段导致 KeyError。"""
    raw = _read_json(_SETTINGS, {})
    out = dict(_DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for k in _DEFAULT_SETTINGS:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
    return out


def get_settings():
    s = load_settings()
    return {"ok": True, "settings": s,
            "paused": bool(s.get("paused")),
            "trust_mode": bool(s.get("trust_mode"))}


def is_paused():
    """自动化是否处于暂停状态。

    只该在**自动触发**的入口调用（定时任务、抽空自检、自动评估、
    信任模式自动执行）。用户手动点的操作不该被这个闸挡住。
    任何异常都按「没暂停」处理 —— 暂停是便利功能，不能因为它坏了
    就把整个自动化锁死。
    """
    try:
        return bool(load_settings().get("paused"))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return False


def paused_gate(what="自动化"):
    """自动入口的统一闸门。返回 None 表示放行，否则返回「已跳过」结果。

    用法：
        g = paused_gate("定时反思")
        if g: return g
    """
    if not is_paused():
        return None
    return {"ok": True, "skipped": True, "paused": True,
            "reason": "自动化已暂停，%s 未执行（点顶部暂停按钮可恢复）" % what}


def toggle_pause(paused=None):
    """切换 / 设置暂停状态。

    paused=None → 翻转当前状态（按钮点一下切换）；
    paused=True/False → 直接设为该值（给 API 显式指定用）。
    """
    cur = load_settings()
    now = (not bool(cur.get("paused"))) if paused is None else bool(paused)
    out = set_settings(paused=now)
    return {"ok": True, "paused": now,
            "settings": out.get("settings") or {},
            "label": "已暂停（灯灭）" if now else "运行中（绿灯）"}


def set_settings(**kw):
    """改设置。只接受白名单里的字段（防止乱写进无关的键）。"""
    cur = load_settings()
    changed = {}
    for k, v in kw.items():
        if k not in _DEFAULT_SETTINGS or v is None:
            continue
        if isinstance(_DEFAULT_SETTINGS[k], bool):
            v = bool(v)
        elif isinstance(_DEFAULT_SETTINGS[k], int) and not isinstance(v, bool):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        cur[k] = v
        changed[k] = v
    save_settings(cur)
    return {"ok": True, "settings": cur, "changed": changed}


def save_settings(d):
    _write_json(_SETTINGS, d)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------- 活动打点 / 抽空判定
# 用户原话：「自检这东西能不能也是和自动迭代一样，自从抽空检测」。
# 每天 05:10 那次是保底；这里做的是「趁用户不聊的时候顺手查一遍」。

def touch_activity():
    """记一笔「用户刚说了话」。主聊天通道每收到一条消息都调它。"""
    try:
        _write_json(_ACT, {"ts": time.time(), "iso": _now()})
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return True


def last_activity():
    """用户最后一次说话的时间戳（秒）。没有记录时返回 0。"""
    d = _read_json(_ACT, {})
    try:
        return float(d.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def idle_gate(settings=None):
    """判断现在适不适合「抽空跑一次自检」。

    返回 {"ok": bool, "reason": str, ...}，ok=False 时 reason 是给人看的中文原因。
    四条全过才放行：
      1. 开关开着
      2. 用户已安静满 idle_gap_min 分钟（不打断对话）
      3. 距上次自检满 idle_every_min 分钟（别反复扫同一份代码）
      4. 没有反思 / 迭代队列在跑（不抢资源、不并发改代码）
    """
    s = settings or load_settings()
    if s.get("paused"):
        return {"ok": False, "paused": True, "reason": "自动化已暂停"}
    if not s.get("idle_audit"):
        return {"ok": False, "reason": "抽空自检开关关着"}

    gap = max(1, int(s.get("idle_gap_min") or 10)) * 60.0
    last = last_activity()
    idle_sec = (time.time() - last) if last > 0 else gap
    if idle_sec < gap:
        return {"ok": False,
                "reason": "用户 %d 秒前还在说话（需安静满 %d 分钟）"
                          % (int(idle_sec), int(gap // 60)),
                "idle_sec": round(idle_sec, 1)}

    every = max(1, int(s.get("idle_every_min") or 180)) * 60.0
    hist = _audit_history().get("items") or []
    last_audit = 0.0
    for it in hist:
        try:
            last_audit = max(last_audit, float(it.get("_ts") or 0))
        except (TypeError, ValueError):
            continue
    if last_audit <= 0 and hist:
        try:
            last_audit = time.mktime(time.strptime(str(hist[0].get("ts")),
                                                   "%Y-%m-%d %H:%M:%S"))
        except Exception:                               # noqa: BLE001
            last_audit = 0.0
    if last_audit and (time.time() - last_audit) < every:
        return {"ok": False,
                "reason": "距上次自检只过了 %d 分钟（需满 %d 分钟）"
                          % (int((time.time() - last_audit) / 60), int(every // 60)),
                "last_audit": last_audit}

    try:
        if (run_status() or {}).get("running"):
            return {"ok": False, "reason": "自我反思正在跑，等它结束"}
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    try:
        from . import iter_runner as _IR
        if (_IR.snapshot(0) or {}).get("running"):
            return {"ok": False, "reason": "迭代队列正在执行，等它结束"}
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    return {"ok": True, "reason": "空档，可以自检",
            "idle_sec": round(idle_sec, 1), "last_audit": last_audit}


def _pid():
    return time.strftime("%Y%m%d_%H%M%S")


# ------------------------------------------------- 检查点落盘复用
# 用户要求：run_audit 开头读检查点，跳过已确认的文件，避免每轮从零读代码。
_CHECKPOINT = _DATA / "checkpoint.json"


_ROUNDS = _DATA / "rounds.jsonl"


def _change_gate(rel, count, last_failed=False, threshold=None, consec_fail=0,
                 root_cause=""):
    """同文件改动冷却闸门：改太多次还没成，就先停手换思路。

    返回 {"allow": bool, "reason": str}。

    为什么需要（真实数据，不是猜的）
    --------------------------------
    `self_iterate.py` 被自动改过 16 次、`main.py` 14 次，整体成功率 76~79%。
    反复改同一个文件 = 在同一个坑里越挖越深：前一次没成，下一次往往还在
    同一个错误假设上打转。与其继续硬改，不如先放下、换个切入点。

    ★ 2026-11-15 实测再修：原来判据是「次数超阈值 **且 上次失败**」，
      结果是「失败一次就锁死」——f6 因为一次补段找不到而失败，紧接着
      重试就被拦，而那次失败的原因（补段匹配太死）当时已经修好了。
      单次失败很可能只是模型偶发抽风，**连续失败**才说明真在同一个坑里。
      所以改成：超阈值 **且 连续失败 >= 2** 才拦；单次失败放行让它重试。
    """
    try:
        limit = int(threshold if threshold is not None
                    else (_DEFAULT_SETTINGS.get("change_cool_limit") or 5))
    except Exception:                                     # noqa: BLE001
        limit = 5
    try:
        cnt = int(count or 0)
    except Exception:                                     # noqa: BLE001
        cnt = 0
    try:
        consec = int(consec_fail or 0)
    except Exception:                                     # noqa: BLE001
        consec = 0
    rel = str(rel or "")
    # 拦两种：
    #   ① 超阈值 **且连续失败 >= 2** —— 在同一个坑里反复挖，先停；
    #   ② 次数已经**远超**阈值（2 倍以上）—— 不管成没成，这么改本身就不正常
    #      （真实数据里 self_iterate.py 被改 16 次，就是这种）。
    # ★ 2026-11-16：写了根因说明就放行。原逻辑只看次数，模型即使已经讲清
    #   「上次为什么没改对、这次换什么思路」也照样被拦 —— 那等于只惩罚、
    #   不给出口。用户要的是「只要不搞坏就行」，有解释就该让它试。
    _has_rc = bool(str(root_cause or "").strip())
    # ★ 2026-11-16：次数判据也必须看**成败**。原来是 `cnt >= limit*2` 不管成败
    #   一律拦 —— 等于「改得多就停」，而改得多恰恰说明迭代在推进。用户要的是
    #   「只要不搞坏就行」，所以只有「连续失败」或「上次失败」才拦。
    # 三个拦下条件（任一）：
    #   ① 连续失败 >= 2  —— 真在同一个坑里反复挖；
    #   ② 上次没改对     —— 先想清楚再动手；
    #   ③ 次数极端多（>= 6 倍阈值）—— 60 分钟内改 99 次这种，不管成没成都异常。
    # 其余一律放行：改得多但一直成功，说明迭代在推进，拦它等于扼杀创新。
    if ((not _has_rc) and cnt >= limit
            and (consec >= 2 or last_failed or cnt >= limit * 6)):
        return {"allow": False,
                "reason": "%s 已自动改 %d 次%s（阈值 %d），先停手换思路"
                          % (rel, cnt,
                             "、连续失败 %d 次" % consec if consec >= 2
                             else ("、上次没改对" if last_failed else "、远超阈值"),
                             limit)}
    # ★ 2026-11-15 追加：24h 同文件改动冷却。
    #   真实数据：main.py 被改 21 次、self_iterate.py 被改 17 次 —— 反复叠加。
    #   24h 内同文件已改 >= 3 次时，本轮必须先写「上次为什么没改对 + 这次根因」
    #   才允许再改（由调用方把 root_cause 传进来；没传就拦下）。
    try:
        _hits = _count_recent_file_changes(rel, hours=24)
    except NameError:                                     # noqa: BLE001
        _hits = 0
    except Exception:                                     # noqa: BLE001
        _hits = 0
    # ⚠️ 2026-11-16 实测踩到的坑：`_count_recent_file_changes` 原来**根本没定义**
    #   （靠 except NameError 兜底 → _hits 恒为 0，这条护栏等于不存在）。
    #   补上实现后如果按原条件「_hits >= 3 就拦」，会立刻把 main.py（24h 改 25 次）、
    #   self_iterate.py（21 次）、app.js（15 次）**全部锁死** —— 那等于把用户要的
    #   「自主自由创新」直接掐掉。所以条件必须同时满足：
    #     ① 24h 内改过很多次（>= 8，正常迭代一天改几十次是常态，不该拦）；
    #     ② 近期**连续失败 >= 2**（这才说明真在同一个坑里反复挖）。
    #   单纯「改得多」不是问题，反复失败才是。
    if (_hits >= 8 and consec >= 2
            and not str(root_cause or "").strip()):
        return {"allow": False,
                "need_root_cause": True,
                "reason": "%s 在 24h 内已被自动改 %d 次、且近期连续失败 %d 次，"
                          "本轮必须先写「上次为什么没改对 + 这次根因」才允许再改"
                          % (rel, _hits, consec)}
    return {"allow": True, "reason": ""}


def record_round(plan):
    """把本轮反思的结构化结果记一条，供「自主创新流水线可观测卡片」展示。

    只记**结构化摘要**（标题/数量/论文统计/能力缺口），不记全文 ——
    这个文件每轮追加一行，记全文会很快涨到几十 MB。
    """
    try:
        p = plan or {}
        rec = {
            "ts": time.time(),
            "iso": _now(),
            "id": str(p.get("id") or ""),
            "summary": str(p.get("summary") or "")[:300],
            "features": [str((f or {}).get("title") or "")[:80]
                         for f in (p.get("features") or [])][:8],
            # ★ 2026-11-15：原来只记标题，论文依据完全没进可观测记录 ——
            #   回看历史时看不出「这轮创新是有依据的还是拍脑袋的」。
            "theories": [str((f or {}).get("theory") or "")[:200]
                         for f in (p.get("features") or [])][:8],
            "n_actions": len(p.get("actions") or []),
            "n_feature_actions": len([a for a in (p.get("actions") or [])
                                      if str((a or {}).get("origin")) == "feature"]),
            "n_with_paper": len([f for f in (p.get("features") or [])
                                 if ("论文支撑" in str((f or {}).get("theory") or ""))
                                 or ("《" in str((f or {}).get("theory") or ""))]),
            "paper": p.get("paper") or {},
            "gaps": str(p.get("gaps") or "")[:200],
        }
        _ROUNDS.parent.mkdir(parents=True, exist_ok=True)
        with open(_ROUNDS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:                                # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return {"error": "%s: %s" % (type(e).__name__, e)}


def recent_rounds(n=10):
    """读回最近 n 轮的结构化记录（新的在前）。"""
    try:
        if not _ROUNDS.is_file():
            return []
        rows = []
        with open(_ROUNDS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:                         # noqa: BLE001
                    continue
        return rows[-int(n):][::-1] if n else rows[::-1]
    except Exception:                                     # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return []


def save_checkpoint(data):
    """把本轮自检的进度落盘（已确认文件、已发现问题等）。"""
    try:
        payload = dict(data or {})
        payload["ts"] = time.time()
        payload["iso"] = _now()
        _write_json(_CHECKPOINT, payload)
        return {"ok": True, "path": str(_CHECKPOINT)}
    except Exception as e:                              # noqa: BLE001
        logger.warning("save_checkpoint 失败: %s", e)
        return {"ok": False, "error": str(e)}


def load_checkpoint():
    """读回上次自检的检查点；没有或损坏时返回空 dict。"""
    d = _read_json(_CHECKPOINT, {})
    return d if isinstance(d, dict) else {}


def _fix_review_states(items):
    """读时迁移：把历史上因熔断被判成 failed 的动作改成 need_review。

    ★ 2026-11-16 实测踩到：迁移脚本把磁盘改完（42 条），**正在运行的后端进程**
      几分钟后又用内存里的旧数据把它覆盖回 failed。所以迁移必须放进读取路径 ——
      无论谁写回，读出来的时候都是对的；下一次 save 就会把正确状态写回磁盘。
    """
    n = 0
    for pl in (items or []):
        if not isinstance(pl, dict):
            continue
        for a in (pl.get("actions") or []):
            if not isinstance(a, dict) or a.get("status") != "failed":
                continue
            err = str(a.get("error") or "")
            if not any(m in err for m in _REVIEW_MARKS):
                continue
            a["status"] = "need_review"
            a["review_reason"] = err
            n += 1
    return n


def load_plans():
    d = _read_json(_PLANS, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
    try:
        _fix_review_states(d.get("items"))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


_PLANS_LOCK = threading.Lock()


def _plans_flock(fh):
    """跨进程文件锁（Windows 用 msvcrt，其它用 fcntl）。失败不阻断，只记日志。"""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _plans_funlock(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _merge_plans(disk, mem):
    """按 id 合并两份 plans：内存版优先（它是最新状态），磁盘独有的保留。

    ⚠️ 为什么需要：2026-11-15 实测，孤儿脚本 + 后端 + 测试脚本三个进程
    并发「读-改-写」同一个 plans.json，后写的把先写的整个覆盖掉 ——
    刚建的计划直接消失、动作出现 id=null，程序被反复改。
    """
    if not isinstance(disk, dict) or not isinstance(mem, dict):
        return mem
    d_items, m_items = disk.get("items"), mem.get("items")
    if not isinstance(d_items, list) or not isinstance(m_items, list):
        return mem
    m_by = {p.get("id"): p for p in m_items if isinstance(p, dict)}
    out = []
    for p in d_items:
        if not isinstance(p, dict):
            continue
        out.append(m_by.pop(p.get("id"), p))
    out.extend(m_by.values())
    try:
        out.sort(key=lambda x: str(x.get("id") or ""), reverse=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    merged = dict(mem)
    merged["items"] = out
    return merged


def save_plans(d):
    """写计划。必须跨进程互斥 + 按 id 合并，否则并发写会丢计划。"""
    _ensure()
    with _PLANS_LOCK:
        lf = None
        try:
            lf = open(str(_PLANS) + ".lock", "a+")
            _plans_flock(lf)
            disk = _read_json(_PLANS, {})
            _write_json(_PLANS, _merge_plans(disk, d))
        except Exception:                               # noqa: BLE001
            logger.warning("写 plans 失败，退化为直接写", exc_info=True)
            _write_json(_PLANS, d)
        finally:
            if lf is not None:
                _plans_funlock(lf)
                lf.close()


def _actlog(entry):
    """信任模式自动执行时写一条 actlog，供用户查「有没有在改代码」。"""
    try:
        p = _DATA / "actlog.jsonl"
        _ensure()
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


# 2026-11-16 实测发现的问题：`runtime_metrics()` 的成功率被**验证用例污染**了。
# 上一轮为了验证回滚能力，故意造了 4 条必然失败的改动（「语法错误补丁→应被拦下」
# 「写 data/ 目录→应失败」…），它们全被算进「真实自动改动成功率」，把 65.5% 这个
# 数字拉低了。而这个数字会原样注入反思 prompt —— 等于系统拿**测试造成的失败**
# 判断自己「不靠谱」，进而更保守。所以验证用例必须和真实改动分开统计。
_PROBE_HINTS = ("探针", "应被", "→ 失败", "→ 成功", "e2e", "E2E",
                "验证用例", "语法错误补丁", "写 data/ 目录", "find 匹配不到")


def _is_probe(plan, act):
    """这条动作是不是「验证/测试用例」而不是真实自动改动。"""
    try:
        if (plan or {}).get("probe") or (act or {}).get("probe"):
            return True
        blob = "%s %s" % (str((act or {}).get("title") or ""),
                          str((plan or {}).get("id") or ""))
        return any(h in blob for h in _PROBE_HINTS)
    except Exception:                                   # noqa: BLE001
        return False


def _verify_failure_real(fail_kind, days=7):
    """校验 fail_kind 是否真实存在（反幽灵修复 / Phantom Guardrails）。

    背景（arXiv:2611.17336v1 Phantom Guardrails）：模型会「修复」一个
    根本不存在的失败类型 —— 幽灵修复。实测成功率只有 66%，其中相当一部分
    是「修了一个从未发生过的 fail_kind」。

    做法（2026-11-17 升级为双源校验）：
      ① 主源：actlog（本模块自己的动作账本，list_actlog）；
      ② 备源：actlog 模块的 task.json 事件账本（count_fail_kind_ex），
         它按**时间窗**过滤，能识别「3 个月前出现过、近 7 天没发生」的幽灵。
    两源任一命中即视为真实（宁可放行，也不要把真实失败误判成幽灵）。

      · 计数 > 0 → 真实，放行；
      · 计数 = 0 → 幽灵，拒绝立项，并往 actlog 记一条 phantom 事件留痕。

    返回 {"ok": bool, "count": int, "reason": str, "source": str}。
    任何异常都按「放行」处理 —— 校验坏了不能把整个迭代锁死。
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return {"ok": True, "count": 0, "source": "skip",
                "reason": "fail_kind 为空，跳过校验"}
    try:
        n_days = float(days)
    except (TypeError, ValueError):
        n_days = 7.0
    if n_days <= 0:
        n_days = 7.0
    cutoff = time.time() - n_days * 86400.0

    # ---- 源 ①：本模块 actlog（动作账本）----
    count = 0
    try:
        rows = list_actlog(500)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        rows = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # 只认失败记录，且 fail_kind 要精确匹配
        if str(r.get("result") or "") != "fail":
            continue
        if str(r.get("fail_kind") or "").strip() != fk:
            continue
        try:
            t = time.mktime(time.strptime(str(r.get("ts") or "")[:19],
                                          "%Y-%m-%d %H:%M:%S"))
        except Exception:                               # noqa: BLE001
            continue
        if t >= cutoff:
            count += 1
    if count > 0:
        return {"ok": True, "count": count, "source": "actlog",
                "reason": "%s 近 %d 天真实失败 %d 次" % (fk, int(n_days), count)}

    # ---- 源 ②：actlog 模块的 task.json 事件账本（按时间窗）----
    # 只有「账本读得出来且确实是 0」才算幽灵；读不出来（ok=False）按放行处理。
    try:
        from . import actlog as _AL
        n2, ok2 = _AL.count_fail_kind_ex(fk, days=int(n_days))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        n2, ok2 = 0, False
    if n2 > 0:
        return {"ok": True, "count": int(n2), "source": "task_events",
                "reason": "%s 近 %d 天真实失败 %d 次（事件账本）"
                          % (fk, int(n_days), int(n2))}
    if not ok2:
        # 账本读不出来 —— 不能据此判幽灵，放行
        return {"ok": True, "count": 0, "source": "unavailable",
                "reason": "%s 的失败账本读不出来，跳过幽灵校验" % fk}

    # ---- 两源都是 0：确认幽灵，记一条 phantom 留痕 ----
    try:
        _actlog({"ts": _now(),
                 "plan_id": "",
                 "action": "phantom_guard",
                 "title": "拒绝幽灵修复：%s" % fk[:60],
                 "files": [],
                 "result": "fail",
                 "fail_kind": fk,
                 "phantom": True,
                 "detail": "%s 近 %d 天真实计数为 0（actlog + 事件账本双源），拒绝立项"
                           % (fk, int(n_days)),
                 "probe": False,
                 "auto": False})
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"ok": False, "count": 0, "source": "phantom",
            "reason": "%s 近 %d 天真实计数为 0（幽灵修复），拒绝立项"
                      % (fk, int(n_days))}


def _log_act(plan, act, ok, detail=""):
    """记录一条动作执行日志（用户可查「到底改没改」）。"""
    _actlog({"ts": _now(),
             "plan_id": (plan or {}).get("id"),
             "action": (act or {}).get("id"),
             "title": str((act or {}).get("title") or "")[:90],
             "files": (act or {}).get("files") or [],
             "result": "ok" if ok else "fail",
             "detail": str(detail or "")[:500],
             # ★ 验证用例标记：runtime_metrics 统计成功率时会跳过它们，
             #   避免「测试故意制造的失败」被当成系统不靠谱的证据。
             "probe": _is_probe(plan, act),
             # 2026-11-15：用户要「详细说明实现了什么功能 / 改了什么」——
             # feature = 这个动作打算实现什么（动作自带的描述），
             # changelog = 实际改成了什么样（从补丁归纳）。两者配对才讲得清。
             "feature": str((act or {}).get("detail") or "")[:300],
             "changelog": (act or {}).get("changelog") or {},
             "backup": (act or {}).get("backup") or "",
             "auto": bool((act or {}).get("auto"))})


def list_actlog(limit=40):
    """读执行日志（倒序），供前端/工具查询。

    返回信任模式自动执行记录（时间、文件、成功/失败），
    供 /api/selfiter/actlog 与前端「最近自动执行」区块消费。
    """
    p = _DATA / "actlog.jsonl"
    if not p.is_file():
        return []
    out = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:                           # noqa: BLE001
                continue
    except Exception:                                   # noqa: BLE001
        return []
    return out[-int(limit):][::-1]


# 用户要求（2026-11-15）：「确保原来没问题的程序不因为模型判断失误被反复修改，
# 有风险的才需要改」。静态检查跑一次是确定性的，但**反复改同一处**说明
# 上一次没改对 —— 越改越乱是真实踩过的坑（avatar_mouth.py 一天改 9 次）。
# 所以在动手前加一道熔断：同一文件在窗口内被自动改成功太多次，直接停手。
_LOOP_WINDOW_MIN = 60       # 观察窗口（分钟）
# ★ 2026-11-16 实测：原来是 3，而一轮计划典型就改 3~5 个文件（刚才那轮改了
#   main.py / app.js / index.html / style.css / self_iterate.py 各一次），
#   跑两轮就会撞上「60 分钟内同文件成功改 3 次 → 熔断」，把**正常迭代**当成
#   「越改越乱」。用户明确要「自主自由创新，只要不搞坏就行」，所以放到 10：
#   真在同一个坑里打转时，`_change_gate` 的「连续失败」判据会先拦住它。
_LOOP_MAX_HITS = 10         # 窗口内同一文件被自动改成功的上限


def _count_fail_kind(kind, days=7):
    """统计最近 days 天内某个 fail_kind 的**真实**失败次数（读 actlog.jsonl）。

    ★ 2026-11-16 静态自检报的 high：`_count_fail_kind` 在 `_change_summary` 里
      被调用，但**本文件从来没定义过它**，外面套着 except 于是静默失效 ——
      「幽灵修复检测」（针对 fail_kind=X 共 N 次，N 为 0 就说明这次改动没有
      真实失败依据）实际上永远拿不到计数，等于不存在。这里补上实现。
    """
    kind = str(kind or "")
    if not kind:
        return {}
    try:
        rows = list_actlog(500)
    except Exception:                                   # noqa: BLE001
        return {}
    now = time.time()
    n = 0
    for r in rows:
        if r.get("result") != "fail":
            continue
        # actlog 里没有 fail_kind 字段，按 detail 归类（与前端显示同一套判据）
        try:
            if classify_fail(r.get("detail")) != kind:
                continue
        except Exception:                               # noqa: BLE001
            continue
        if r.get("probe") or _is_probe({"id": r.get("plan_id")},
                                       {"title": r.get("title")}):
            continue                                    # 验证用例不算
        try:
            t = time.mktime(time.strptime(str(r.get("ts") or "")[:19],
                                          "%Y-%m-%d %H:%M:%S"))
        except Exception:                               # noqa: BLE001
            continue
        if now - t <= float(days) * 86400:
            n += 1
    return {kind: n}


def _count_recent_file_changes(rel, hours=24):
    """统计 rel 在最近 hours 小时内被**真实**自动改动过几次。

    ★ 2026-11-16：`_loop_guard` 里一直在调这个函数，但它**从来没被定义过** ——
      调用处包着 `except NameError: _hits = 0`，所以既不报错也不生效，
      「24h 同文件改动冷却」这条护栏等于不存在（静态自检报的 high 就是这个）。
      现在补上实现，并沿用 _is_probe 的规则排除验证用例 ——
      否则我跑一次测试就会把真实文件「锁」进冷却，把模型挡在门外。
    """
    rel = str(rel or "").strip()
    if not rel:
        return 0
    n = 0
    now = time.time()
    try:
        rows = list_actlog(300)
    except Exception:                                   # noqa: BLE001
        return 0
    for r in rows:
        files = r.get("files") or []
        if rel not in files:
            continue
        if r.get("probe") or _is_probe({"id": r.get("plan_id")},
                                       {"title": r.get("title")}):
            continue                                    # 验证用例不算
        try:
            t = time.mktime(time.strptime(str(r.get("ts") or "")[:19],
                                          "%Y-%m-%d %H:%M:%S"))
        except Exception:                               # noqa: BLE001
            continue
        if now - t <= float(hours) * 3600:
            n += 1
    return n


_LOOP_RESET = _DATA / "loop_reset.json"


def _loop_reset_map():
    """人工复核通过的时间点：{文件: ts}。

    ★ 2026-11-16 用户报「这么多个失败，彻底解决」——截图里 7 条动作全被
      「反复修改熔断：frontend/app.js 最近 60 分钟内已改成功 10 次」拦死。
      熔断本身是对的（真被改乱过），但**没有出口**：人工看完觉得没问题，
      也没有任何办法让它继续，那个文件就永久锁死了。
      所以加这个文件：用户点「复核通过」后记一个时间点，
      之后统计只数这个时间点**之后**的记录 —— 等于重新计数。
    """
    d = _read_json(_LOOP_RESET, {})
    return d if isinstance(d, dict) else {}


def reset_loop(files):
    """人工复核通过 → 从此刻起重新计数，不让旧记录继续把文件锁死。"""
    files = [str(f or "") for f in (files or []) if f]
    if not files:
        return
    try:
        d = _loop_reset_map()
        now = time.time()
        for f in files:
            d[f] = now
        _write_json(_LOOP_RESET, d)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


_REVIEW_MARKS = ("反复修改熔断", "先停手")

# 待人工复核队列（2026-11-16 用户要求）：熔断命中不再直接判失败，
# 而是把动作挂到这里，前端「审核」按钮读它、点「复核通过」后放行。
_REVIEW_QUEUE = _DATA / "review_queue.json"


def is_review_reason(why):
    """这条拦截是「改太多次、需要人看一眼」还是「模型质量差」？

    前者可以转人工复核（用户点头就放行）；后者复核也没用，仍然判失败。
    """
    s = str(why or "")
    return any(m in s for m in _REVIEW_MARKS)


def _review_queue_read():
    """读待人工复核队列。返回 {"items": [...]}，损坏时给空队列。"""
    d = _read_json(_REVIEW_QUEUE, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        d = {"items": []}
    return d


def _review_queue_write(d):
    try:
        _write_json(_REVIEW_QUEUE, d)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def push_review(pid, action, reason=""):
    """把一条被熔断拦下的动作挂进待人工复核队列（幂等：同 id 覆盖）。

    用户要求（2026-11-16）：「如果要人工复核就在前端一个审核的按钮」。
    这里只负责**记录**，不执行任何改动 —— 真正放行仍然要人点。
    """
    try:
        a = action or {}
        aid = str(a.get("id") or "")
        if not aid:
            return {"ok": False, "error": "动作没有 id，无法挂复核"}
        d = _review_queue_read()
        items = [x for x in d.get("items") or []
                 if not (isinstance(x, dict)
                         and str(x.get("action_id") or "") == aid
                         and str(x.get("plan_id") or "") == str(pid or ""))]
        items.insert(0, {
            "plan_id": str(pid or ""),
            "action_id": aid,
            "title": str(a.get("title") or "")[:200],
            "risk": str(a.get("risk") or "mid"),
            "files": [str(f) for f in (a.get("files") or []) if f],
            "reason": str(reason or "")[:400],
            "ts": _now(),
            "_ts": time.time(),
        })
        d["items"] = items[:200]
        _review_queue_write(d)
        return {"ok": True, "queued": aid}
    except Exception as e:                              # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def list_review_queue(limit=100):
    """给前端读：当前有哪些动作在等人工复核。"""
    d = _review_queue_read()
    items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
    try:
        items.sort(key=lambda x: float(x.get("_ts") or 0), reverse=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"ok": True, "items": items[:int(limit or 100)],
            "total": len(items)}


def clear_review(plan_id="", action_id=""):
    """复核处理完（通过或拒绝）后从队列里摘掉。"""
    try:
        d = _review_queue_read()
        pid, aid = str(plan_id or ""), str(action_id or "")
        keep = []
        for x in (d.get("items") or []):
            if not isinstance(x, dict):
                continue
            if pid and str(x.get("plan_id") or "") != pid:
                keep.append(x)
                continue
            if aid and str(x.get("action_id") or "") != aid:
                keep.append(x)
                continue
            # 命中要摘掉的那条
        d["items"] = keep
        _review_queue_write(d)
        return {"ok": True, "left": len(keep)}
    except Exception as e:                              # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _loop_guard(files, root_cause=""):
    """反复修改熔断。返回 (allow, why)。读不到日志时放行（不误伤）。

    ★ 2026-11-16：加 `root_cause` —— 24h 冷却闸门在「这文件改过很多次**且**近期
      连续失败」时要求给出「上次为什么没改对 + 这次根因」。动作自带的 detail
      通常就是在讲这个（「改成宽松锚点，因为上次 find 匹配太死」），
      所以调用方把它当 root_cause 传进来；否则模型永远过不了那道闸。
    """
    files = [f for f in (files or []) if f]
    if not files:
        return True, ""
    try:
        rows = list_actlog(limit=200)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return True, ""
    now = time.time()
    _rst = _loop_reset_map()
    hit = {}
    for r in rows:
        if r.get("result") != "ok":
            continue
        try:
            t = time.mktime(time.strptime(str(r.get("ts") or ""),
                                          "%Y-%m-%d %H:%M:%S"))
        except Exception:                               # noqa: BLE001
            continue
        if now - t > _LOOP_WINDOW_MIN * 60:
            continue
        for f in (r.get("files") or []):
            if f in files:
                _since = _rst.get(f)
                if _since and t <= float(_since):
                    continue        # 人工复核通过之前的不算，别把文件永久锁死
                hit[f] = hit.get(f, 0) + 1

    # ★ 2026-11-15 接线 _change_gate：上面只数「最近 N 分钟内**成功**改了几次」，
    #   完全不看失败 —— 于是「同一个文件改了 5 次全失败」这种最该停手的情况
    #   反而放行（因为它一次都没成功，hit 是 0）。这里补一层：
    #   累计改动次数 + 上次是否失败。
    try:
        _cnt, _last_fail, _consec, _closed = {}, {}, {}, set()
        for r in rows:
            # ★ 2026-11-15 实测修 bug：这里原来统计**全部历史**，没有时间窗口。
            #   _change_gate 里 cnt >= limit*2 就不管成败一律拦，于是
            #   main.py 全历史被改 17 次 → cnt=17 >= 6 → **永久锁死**：
            #   任何涉及 main.py 的新功能都改不动，自动迭代等于废了。
            #   真实数据：main.py 最近 30 分钟内只改了 1 次。
            #   正确语义是「最近这段时间在同一个坑里反复挖」，必须限窗口。
            try:
                _t = time.mktime(time.strptime(str(r.get("ts") or ""),
                                              "%Y-%m-%d %H:%M:%S"))
            except Exception:                             # noqa: BLE001
                continue
            if now - _t > _LOOP_WINDOW_MIN * 60:
                continue
            # ★ 2026-11-15 实测修死锁：被护栏拦下的记录**根本没改文件**，
            #   不能计入改动次数。否则「被拦 → 记一条 fail → 计数+1 →
            #   下次更容易被拦」形成正反馈，实测 21:14 那条 fail 就是被拦
            #   本身，导致 main.py 永远出不来（自动迭代实际已停摆）。
            _d = str(r.get("detail") or "")
            if any(m in _d for m in ("先停手换思路", "反复修改熔断",
                                     "改动冷却")):
                continue
            _ok = (r.get("result") == "ok")
            for f in (r.get("files") or []):
                if f in files:
                    _since2 = _rst.get(f)
                    if _since2 and _t <= float(_since2):
                        continue    # 同上：人工复核通过后重新计数
                    _cnt[f] = _cnt.get(f, 0) + 1
                    # rows 是最新在前，第一次遇到就是「上次」，别被更旧的覆盖
                    if f not in _last_fail:
                        _last_fail[f] = (not _ok)
                    # ★ 连续失败链：从最新往回数，一旦遇到成功就断链（_closed）。
                    #   单次失败不该锁死（可能只是模型偶发抽风），
                    #   连续 2 次失败才说明真在同一个坑里。
                    if f not in _closed:
                        if _ok:
                            _closed.add(f)
                        else:
                            _consec[f] = _consec.get(f, 0) + 1
        for f in files:
            _g = _change_gate(f, _cnt.get(f, 0), _last_fail.get(f, False),
                              consec_fail=_consec.get(f, 0),
                              root_cause=root_cause)
            if not _g.get("allow"):
                return False, str(_g.get("reason") or "改动冷却")
    except Exception:                                     # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    hot = [(f, n) for f, n in hit.items() if n >= _LOOP_MAX_HITS]
    if hot:
        f, n = sorted(hot, key=lambda x: -x[1])[0]
        return False, ("反复修改熔断：%s 在最近 %d 分钟内已被自动改成功 %d 次，"
                       "先停手等人工确认（避免越改越乱）"
                       % (f, _LOOP_WINDOW_MIN, n))
    return True, ""


def load_signals():
    d = _read_json(_SIGNALS, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
    return d


def save_signals(d):
    _write_json(_SIGNALS, d)


# ---------------------------------------------------------------- 信号（对话摘要）

def add_signal(user_text, assistant_text, meta=None):
    """前端每轮对话结束后上报一段摘要，供反思时当作"最近发生了什么"。

    只留最近 _SIGNAL_KEEP 条，避免文件无限增长。
    """
    d = load_signals()
    u = str(user_text or "")[:800]
    a = str(assistant_text or "")[:800]
    # 幂等去重：前端每次都会把最近窗口整段重推，不去重就会把「同一条问答」
    # 记成好几条，体验评估便会误判成「用户重复发送同一需求」（2026-11-15 实测踩到）。
    # 用 用户+回复 一起做指纹：真的重复提问，回复内容是不同的。
    key = hashlib.sha1((u[:400] + "\x00" + a[:400]).encode("utf-8", "replace")).hexdigest()[:16]
    for it in d["items"][-60:]:
        if it.get("key") == key:
            return {"ok": True, "count": len(d["items"]), "dup": True}
    # 同一句话之前出现过 → 间隔超过 3 分钟才算「用户真的又提了一遍」；
    # 短间隔的通常是我这边重试/重新生成，不是用户重复。
    repeat = False
    for it in reversed(d["items"][-30:]):
        if str(it.get("user") or "")[:300] == u[:300]:
            try:
                t0 = time.mktime(time.strptime(str(it.get("ts")),
                                               "%Y-%m-%d %H:%M:%S"))
                gap = time.time() - t0
            except (TypeError, ValueError):
                gap = 99999
            repeat = gap > 180
            break
    h = _heur_eval(u, a, meta)
    d["items"].append({
        "ts": _now(),
        "user": u,
        "assistant": a,
        "meta": meta or {},
        "heur": h,
        "key": key,
        "repeat": repeat,
    })
    if len(d["items"]) > _SIGNAL_KEEP:
        d["items"] = d["items"][-_SIGNAL_KEEP:]
    save_signals(d)
    if h.get("need_llm"):
        _maybe_auto_evaluate()
    return {"ok": True, "count": len(d["items"])}


_AUTO_LOCK = threading.Lock()
_LAST_AUTO = [0.0]


def _maybe_auto_evaluate():
    """用户明确不满 / 报错时，后台自动评一次。

    这是「主动评估」的实时那一半（另一半是定时任务）。
    节流 10 分钟，避免用户连续几句抱怨就触发好几次 LLM。
    """
    try:
        if is_paused():
            return
        if not load_settings().get("evaluate_auto", True):
            return
        now = time.time()
        with _AUTO_LOCK:
            if now - _LAST_AUTO[0] < 600:
                return
            _LAST_AUTO[0] = now
        threading.Thread(target=_auto_eval_worker, daemon=True).start()
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _auto_eval_worker():
    try:
        evaluate(limit=6, use_llm=True, trigger="auto")
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


# ---------------------------------------------------------------- 体验评估
# 2026-11-15 用户追加要求（原话要点）：
#   · 主动在交流中评估任务的完成效果；评分不好或过程繁琐，就自己想怎么优化；
#   · 哪怕平时闲聊，也要能从用户的反应里读出体验好坏，并思考以后怎么改进。
#
# 设计：分两层，避免每轮都调 LLM 烧 token
#   ① 启发式（零成本，每轮都算）—— 只用来判断「要不要认真评估」，不当最终结论；
#   ② LLM 批量评估（攒够几轮再跑）—— 打分 + 找摩擦点 + 给可执行改进。
# 评估结果会喂回 reflect()，变成下一轮迭代的输入。

_EXP = _DATA / "experience.json"
_EXP_KEEP = 400

# 强信号：出现基本就说明这一轮有问题（都是这套系统里用户真实说过的话）
_BAD_STRONG = (
    "一点都不", "不满意", "没解决", "没有解决", "没搞定", "全乱", "乱套",
    "退步", "垃圾", "崩了", "报错", "忘了", "偏离", "不对", "不行", "没用",
    "失败", "太严重", "糊", "丑", "越来越差", "搞错", "理解错", "答非所问",
    "恶心", "难看", "不好看", "不自然", "别扭", "没效果", "没反应", "没变",
    "没生效", "没起作用", "不如以前", "差远了",
)
# 弱信号：单独出现不算问题，累积才算
_BAD_WEAK = (
    "还是", "又", "为什么", "重新", "再来", "麻烦", "繁琐", "太慢", "错",
    "偏了", "不够", "不行", "没有", "卡", "重复",
)
# 正面信号（用词组，避免「好」命中「不好」）
_GOOD_WORDS = ("不错", "完美", "搞定", "满意", "谢谢", "厉害", "可以了", "很好", "对味", "到位")


def _count_hits(text, words):
    t = str(text or "")
    return sum(1 for w in words if w in t)


def _count_good(text):
    """数正面词，但排除疑问/否定用法：「搞定了吗」「没搞定」都不算夸。"""
    t = str(text or "")
    n = 0
    for w in _GOOD_WORDS:
        for m in re.finditer(re.escape(w), t):
            tail = t[m.end():m.end() + 2]
            if tail[:1] in ("吗", "么", "没") or tail[:2] in ("了吗", "了没"):
                continue
            n += 1
            break
    return n


def _heur_eval(user_text, assistant_text="", meta=None):
    """零成本启发式打分：关键词 + 长度 + 复发模式 + 报错。"""
    u = str(user_text or "")
    meta = meta or {}
    bad_s = _count_hits(u, _BAD_STRONG)
    bad_w = _count_hits(u, _BAD_WEAK)
    good = _count_good(u)
    score = 7.0 - min(5.0, bad_s * 1.7 + bad_w * 0.45) + min(1.5, good * 0.6)
    flags = []
    if bad_s >= 1:
        flags.append("明确不满")
    if bad_w >= 3:
        flags.append("多处抱怨")
    if good and not bad_s:
        flags.append("正面反馈")
    if len(u) > 160 and (bad_s or bad_w >= 2):
        flags.append("长段吐槽")           # 愿意打这么多字 = 问题不小
    if re.search(r"(还是|又|再次|仍然|依然).{0,10}(不行|没|问题|错|偏|乱)", u):
        flags.append("问题复发")
    if meta.get("error") or meta.get("failed"):
        flags.append("执行报错")
    try:
        if int(meta.get("turns") or 0) >= 6:
            flags.append("过程繁琐")
    except (TypeError, ValueError):
        logger.debug("silent-except", exc_info=True)
    if not u.strip():
        flags.append("空输入")
    score = max(0.0, min(10.0, score))
    return {"score": round(score, 1), "bad_strong": bad_s, "bad_weak": bad_w,
            "good": good, "flags": flags,
            "need_llm": bool(bad_s or bad_w >= 3 or meta.get("error"))}


def load_experience():
    d = _read_json(_EXP, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
    if not isinstance(d.get("seen"), list):
        d["seen"] = []
    return d


def _turn_key(it):
    """一轮对话的稳定指纹（用于「这一轮已经评过了」判断）。"""
    raw = (str(it.get("user") or "") + "\x00" + str(it.get("assistant") or "")[:200])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


# 摩擦点同义归并：两次评估对同一个问题往往叫法不同，
# 不归并的话「回复截断 / 输出截断」会各占一条，看起来像两个问题。
_FRICTION_CANON = (
    ("回复被截断", ("截断", "断掉", "半句", "不完整", "没收尾", "断在")),
    ("同一需求被重复发送", ("重复", "复述", "重发", "再说一遍", "复问", "同一需求",
                            "说三遍", "粘贴三", "三次", "又说", "再提")),
    ("只诊断不修复", ("只诊断", "不修", "未闭环", "没落地", "未落地", "不闭环",
                      "只表态", "不动手", "表态", "不落地", "不交付")),
    ("过度确认", ("确认", "询问", "ask", "反问", "请示")),
    ("空回复", ("空回复", "无响应", "没有回复", "空答")),
    ("绕路/用错工具", ("绕路", "用错工具", "调错", "多绕", "工具用错")),
    ("改完不验证", ("不验证", "没验证", "未验证", "没测", "没自测")),
    ("过程过长/碎步", ("过长", "太长", "啰嗦", "碎步", "思考过")),
)


def _canon_tag(tag):
    t = str(tag or "")
    for canon, keys in _FRICTION_CANON:
        for k in keys:
            if k in t:
                return canon
    return t


def _dedupe_turns(items):
    """按**用户原话**归并同一句话的多条记录。

    为什么不能只按「用户+回复」去重：一条回复被截断后重试，会产生
    「同一句用户话 + 两条不同回复」，看起来像用户说了两遍，其实是系统侧重试。
    这里合并成一条，保留最长的那条回复，并记下出现了几次（_dup），
    真正的「用户又说了一遍」由 add_signal 里的 repeat 标记负责。
    """
    order, groups = [], {}
    for it in items or []:
        k = str(it.get("user") or "")[:300]
        if k not in groups:
            groups[k] = {"item": it, "n": 1, "repeat": bool(it.get("repeat"))}
            order.append(k)
            continue
        g = groups[k]
        g["n"] += 1
        g["repeat"] = g["repeat"] or bool(it.get("repeat"))
        if len(str(it.get("assistant") or "")) > len(str(g["item"].get("assistant") or "")):
            g["item"] = it
    out = []
    for k in order:
        g = groups[k]
        it = dict(g["item"])
        it["_dup"] = g["n"]
        it["repeat"] = g["repeat"]
        out.append(it)
    return out


def save_experience(d):
    _write_json(_EXP, d)


def _build_eval_prompt(items):
    lines = []
    for i, it in enumerate(items, 1):
        u = (it.get("user") or "").replace("\n", " ")[:420]
        a = (it.get("assistant") or "").replace("\n", " ")[:420]
        fl = ",".join((it.get("heur") or {}).get("flags") or [])
        note = ""
        if int(it.get("_dup") or 1) > 1:
            note += ("（⚠️ 这条用户消息系统侧记录了 %d 次，含截断重试/重推，"
                     "**不要**当成用户重复提问）" % int(it.get("_dup")))
        if it.get("repeat"):
            note += "（⚠️ 用户确实又把这句话提了一次，这是真实的重复诉求）"
        lines.append("%d. 用户：%s\n   我：%s\n   机器初判：%s%s"
                     % (i, u, a, fl or "无", note))
    return """你是这个本地多模态助手项目的「用户体验评估器」。请**严格、不客套**地评估下面这几轮交流。

%s

## 评估维度
1. **任务完成度**（0-10）：用户要的东西真的做成了吗？有没有答非所问、只做一半、
   或者**声称做了其实没做**（这是最严重的）。
2. **过程效率**（0-10）：绕了多少弯路？该用的工具用了吗？有没有反复确认、
   反复返工、让用户把同一件事说三遍？
3. **用户情绪**：满意 / 中性 / 不耐烦 / 不满。**哪怕只是闲聊，也要从语气、
   追问、纠错、催促里判断**——用户没明说「不满意」也要能读出来。
4. **摩擦点**：具体哪一步让用户不爽（例如「用错工具」「没查证就编」
   「改完不验证」「配置要重复填」「问太多废话」）。

## 输出格式（严格 JSON，不要额外文字）
{
  "turns": [{"i":1,"task":"一句话说这轮在干什么","completion":8,"efficiency":5,
             "sentiment":"满意|中性|不耐烦|不满","evidence":"用户原话里的证据",
             "problem":"最大的问题（没有就写空字符串）","fix":"下次具体怎么改（可执行）"}],
  "frictions": [{"tag":"摩擦点短标签","count":2,"detail":"具体表现","suggestion":"怎么消除"}],
  "suggestions": [{"title":"改进项","why":"为什么值得做","how":"怎么做",
                   "kind":"prompt|ui|feature|code","files":["backend/xxx.py"]}],
  "overall": "这几轮的整体判断（40字内）"
}
⚠️ 证据必须来自上面的原话，不要编。⚠️ 没有问题就给高分，不要为了显得认真而挑刺。
只输出 JSON。""" % "\n".join(lines)


def evaluate(limit=12, use_llm=True, trigger="manual"):
    """评估最近几轮交流。启发式先跑（LLM 挂了也有数据），LLM 再补深度判断。"""
    if trigger != "manual":
        g = paused_gate("自动评估")
        if g:
            return g
    sig = load_signals().get("items", [])
    if not sig:
        return {"ok": False, "error": "还没有对话素材（先在对话里聊几轮）"}
    seen = set(load_experience().get("seen") or [])
    window = sig[-max(1, min(int(limit or 12), 30)):]
    items = _dedupe_turns([it for it in window if _turn_key(it) not in seen])
    if not items:
        return {"ok": False, "skipped": True,
                "error": "最近 %d 轮都已经评估过了，多聊几轮再来（不重复评，避免刷分）"
                         % len(window)}
    rec = {"id": "e" + _pid(), "ts": _now(), "trigger": trigger,
           "n": len(items), "turns": [], "frictions": [], "suggestions": [],
           "overall": "", "avg_completion": None, "avg_efficiency": None,
           "llm": False}
    for i, it in enumerate(items, 1):
        h = it.get("heur") or _heur_eval(it.get("user"), it.get("assistant"), it.get("meta"))
        rec["turns"].append({"i": i, "task": (it.get("user") or "").replace("\n", " ")[:70],
                             "completion": h["score"], "efficiency": h["score"],
                             "sentiment": ("不满" if h["score"] < 5 else
                                           ("满意" if h["score"] >= 8 else "中性")),
                             "evidence": "启发式：" + ("、".join(h["flags"]) or "无明显信号"),
                             "problem": "", "fix": ""})
    if use_llm:
        txt, err = _llm_text(_build_eval_prompt(items), max_tokens=3000, temperature=0.4)
        data = _parse_json(txt) if not err else None
        if isinstance(data, dict):
            rec["llm"] = True
            turns = [t for t in (data.get("turns") or []) if isinstance(t, dict)]
            if turns:
                rec["turns"] = turns
            rec["frictions"] = [f for f in (data.get("frictions") or []) if isinstance(f, dict)]
            rec["suggestions"] = [s for s in (data.get("suggestions") or []) if isinstance(s, dict)]
            rec["overall"] = str(data.get("overall") or "")[:200]
        elif err:
            rec["llm_error"] = str(err)[:200]
    # 汇总
    def _avg(key):
        vals = []
        for t in rec["turns"]:
            try:
                vals.append(float(t.get(key)))
            except (TypeError, ValueError):
                logger.debug("silent-except", exc_info=True)
        return round(sum(vals) / len(vals), 1) if vals else None
    rec["avg_completion"] = _avg("completion")
    rec["avg_efficiency"] = _avg("efficiency")
    d = load_experience()
    d["items"].insert(0, rec)
    d["items"] = d["items"][:_EXP_KEEP]
    d["seen"] = (list(seen) + [_turn_key(it) for it in items])[-800:]
    save_experience(d)
    return {"ok": True, "record": rec, "summary": experience_summary()}


def experience_summary():
    """给前端和 reflect 用的汇总：均分、趋势、反复出现的摩擦点、待做改进。"""
    items = load_experience().get("items", [])
    if not items:
        return {"ok": True, "count": 0, "avg_completion": None,
                "avg_efficiency": None, "trend": "", "frictions": [],
                "suggestions": [], "recent": [], "low": []}
    turns = []
    for r in items:
        for t in (r.get("turns") or []):
            turns.append(t)
    def _avg(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else None
    comp = _avg([t.get("completion") for t in turns])
    eff = _avg([t.get("efficiency") for t in turns])
    # 趋势：最近 10 轮 vs 之前 10 轮（turns 是按时间正序塞进 records 的）
    ordered = []
    for r in reversed(items):
        ordered.extend(r.get("turns") or [])
    trend = ""
    if len(ordered) >= 8:
        half = len(ordered) // 2
        a = _avg([t.get("completion") for t in ordered[:half]])
        b = _avg([t.get("completion") for t in ordered[half:]])
        if a is not None and b is not None:
            d = b - a
            trend = ("上升 %.1f" % d) if d >= 0.5 else (("下降 %.1f" % abs(d)) if d <= -0.5 else "持平")
    # 摩擦点按 tag 累计
    fmap = {}
    for r in items:
        rid = str(r.get("id") or r.get("ts") or "")
        for f in (r.get("frictions") or []):
            tag = str(f.get("tag") or "").strip()
            if not tag:
                continue
            tag = _canon_tag(tag)
            it = fmap.setdefault(tag, {"tag": tag, "by": {}, "detail": "",
                                       "suggestion": ""})
            try:
                c = int(f.get("count") or 1)
            except (TypeError, ValueError):
                c = 1
            # 同一批评估里两种叫法往往指同一批轮次 → 取最大值，不重复累加；
            # 不同批次覆盖的是不同轮次，才相加。
            it["by"][rid] = max(it["by"].get(rid, 0), c)
            it["detail"] = it["detail"] or str(f.get("detail") or "")[:200]
            it["suggestion"] = it["suggestion"] or str(f.get("suggestion") or "")[:200]
    for it in fmap.values():
        it["count"] = sum(it["by"].values())
    frictions = sorted(fmap.values(), key=lambda x: -x["count"])[:8]
    # 待做改进：取最近两轮的 suggestions
    sugg = []
    for r in items[:2]:
        sugg.extend([s for s in (r.get("suggestions") or []) if isinstance(s, dict)])
    low = [t for t in ordered if isinstance(t.get("completion"), (int, float))
           and t.get("completion") < 6][-8:]
    return {"ok": True, "count": len(items), "avg_completion": comp,
            "avg_efficiency": eff, "trend": trend, "frictions": frictions,
            "suggestions": sugg[:10],
            "recent": items[:3], "low": low}


def runtime_metrics():
    """真实运行指标 —— 不是模型自评，是 actlog 里记下来的硬数据。

    用户要求（2026-11-15）：「根据工作效率以及效果……主动提出增加全新的功能」。
    原来的 experience_brief() 喂给模型的只有 LLM 自己打的分（完成度 x/10、
    效率 y/10），**一条真实运行数据都没有** —— 它判断「效率低」全靠自我感觉，
    看不出「自动改动成功率只有 60%」「同一文件被回滚了 5 次」这种硬事实。
    这里把这些量化出来，作为「该不该做新功能、该先修哪里」的依据。
    """
    out = {"total": 0, "ok": 0, "fail": 0, "rate": None, "rollback": 0,
           "fuse": 0, "files_24h": 0, "avg_sec": None, "last_ts": "",
           "probe_n": 0, "by_file": []}
    try:
        logs = list_actlog(300)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return out
    if not logs:
        return out
    # ★ 验证用例不进成功率（见 _is_probe 的说明）。历史日志没有 probe 字段，
    #   所以按同样的关键词兜底判定一次，把上一轮测试制造的 4 条失败剔出去。
    probe_n = sum(1 for x in logs if x.get("probe") or _is_probe({"id": x.get("plan_id")},
                                                                 {"title": x.get("title")}))
    logs = [x for x in logs
            if not (x.get("probe") or _is_probe({"id": x.get("plan_id")},
                                                {"title": x.get("title")}))]
    out["probe_n"] = probe_n
    if not logs:
        return out
    ok = fail = rb = fz = 0
    secs = []
    files24 = set()
    per_file = {}
    now = time.time()
    for x in logs:
        res = str(x.get("result") or "")
        detail = str(x.get("detail") or "")
        if res == "ok":
            ok += 1
        elif res == "fail":
            fail += 1
        if "回滚" in detail:
            rb += 1
        if "熔断" in detail:
            fz += 1
        m = re.search(r"(\d+(?:\.\d+)?)\s*秒", detail)
        if m:
            try:
                secs.append(float(m.group(1)))
            except Exception:                           # noqa: BLE001
                logger.debug("silent-except", exc_info=True)
        for f in (x.get("files") or []):
            if not f:
                continue
            per_file[f] = per_file.get(f, 0) + 1
        ts = str(x.get("ts") or "")
        if ts:
            try:
                if now - time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")) <= 86400:
                    for f in (x.get("files") or []):
                        if f:
                            files24.add(f)
            except Exception:                           # noqa: BLE001
                logger.debug("silent-except", exc_info=True)
    total = len(logs)
    out.update({
        "total": total, "ok": ok, "fail": fail,
        "rate": round(ok * 100.0 / total, 1) if total else None,
        "rollback": rb, "fuse": fz, "files_24h": len(files24),
        "avg_sec": round(sum(secs) / len(secs), 1) if secs else None,
        "last_ts": str((logs[0] or {}).get("ts") or ""),
        "by_file": [{"file": k, "n": v} for k, v in
                    sorted(per_file.items(), key=lambda kv: -kv[1])[:5]],
    })
    return out


def experience_brief():
    """压成一段纯文本，塞进 reflect 的 prompt 里。"""
    s = experience_summary()
    rt = runtime_metrics()
    real = []
    if rt.get("total"):
        seg = ("【真实运行指标（不是自评，来自自动改动日志）】最近 %d 次自动改动："
               "成功 %d / 失败 %d，成功率 %s%%；回滚 %d 次，熔断 %d 次；"
               "近 24 小时改过 %d 个文件"
               % (rt["total"], rt["ok"], rt["fail"], rt.get("rate"), rt["rollback"],
                  rt["fuse"], rt["files_24h"]))
        if rt.get("avg_sec"):
            seg += "；平均单次改动耗时 %s 秒" % rt["avg_sec"]
        if rt.get("by_file"):
            seg += "；被改最多次的文件：" + "、".join(
                "%s(%d 次)" % (x["file"], x["n"]) for x in rt["by_file"][:3])
        real.append(seg)
        if rt.get("rate") is not None and rt["rate"] < 70:
            # ★ 2026-11-16 修：这句原来是「**应该优先提高改动的可靠性，而不是加新功能**」，
            #   而成功率长期在 65% 左右 → 这句话**每一轮都被注入 prompt**，等于每轮
            #   都在给模型下达「别创新」的指令。实测产出：连续多轮 summary 都是
            #   「先修可靠性再谈创新」，动作全是兜底/过滤/计数这类修补。
            #   用户明确要求「自主自由创新，只要不搞坏就行」，所以改成
            #   「用护栏提可靠性」而不是「用放弃创新提可靠性」。
            real.append("⚠️ 成功率偏低（<70%），说明单次改动太大或缺少可验证断言 —— "
                        "对策是把改动拆小 + 每条自带断言 + 靠自动回滚兜底，"
                        "**这跟要不要做新功能无关，不要因此减少创新**。")
    if not s.get("count"):
        return "\n".join(real) or "（还没有体验评估数据）"
    lines = list(real)
    lines.append("平均完成度 %s / 10，平均效率 %s / 10，趋势：%s" %
                 (s.get("avg_completion"), s.get("avg_efficiency"),
                  s.get("trend") or "样本不足"))
    for f in (s.get("frictions") or [])[:5]:
        lines.append("- 摩擦点「%s」出现 %s 次：%s → 建议：%s" %
                     (f.get("tag"), f.get("count"), f.get("detail"), f.get("suggestion")))
    for t in (s.get("low") or [])[-4:]:
        if t.get("problem") or t.get("fix"):
            lines.append("- 低分轮次「%s」：问题 %s；改法 %s" %
                         (str(t.get("task"))[:40], t.get("problem"), t.get("fix")))
    return "\n".join(lines)


def evaluate_and_improve(limit=10, use_llm=True, trigger="periodic",
                         auto_improve=None):
    """评估最近几轮 → 不达标就**主动**想怎么改。

    用户原话：「如果评分不好或过程繁琐就要思考怎么解决这个问题或怎么优化，
    以提高效率与减少出错率」。所以这里不只看分数，也看「反复出现的摩擦点」。
    """
    ev = evaluate(limit=limit, use_llm=use_llm, trigger=trigger)
    if not ev.get("ok"):
        if ev.get("skipped"):
            return {"ok": True, "skipped": True, "need_improve": False,
                    "reason": str(ev.get("error") or "最近没有新对话可评"),
                    "summary": experience_summary()}
        return ev
    rec = ev.get("record") or {}
    s = ev.get("summary") or {}
    comp = rec.get("avg_completion")
    eff = rec.get("avg_efficiency")
    why = []
    if isinstance(comp, (int, float)) and comp < 6.5:
        why.append("完成度均分 %.1f" % comp)
    if isinstance(eff, (int, float)) and eff < 6.5:
        why.append("效率均分 %.1f" % eff)
    repeat = [f for f in (s.get("frictions") or [])
              if int(f.get("count") or 0) >= 2]
    if repeat:
        why.append("反复摩擦：" + "、".join(str(f.get("tag")) for f in repeat[:3]))
    need = bool(why)
    out = {"ok": True, "record": rec, "summary": s,
           "need_improve": need, "reason": "；".join(why)}
    if not need:
        return out
    if auto_improve is None:
        auto_improve = bool(load_settings().get("trust_mode"))
    focus = ("用户体验评估发现这些问题：%s。整体判断：%s。"
             "请针对每一条摩擦点给出**具体到环节**的改法（优先砍步骤、减少返工）；"
             "同时按「创新授权」的要求，另给至少一条用户能明显感知的全新能力。" %
             (out["reason"], rec.get("overall") or "（无）"))
    r = reflect(trigger="experience", use_web=False, focus=focus,
                auto_execute=auto_improve)
    out["improve"] = r
    out["need_restart"] = bool((r or {}).get("need_restart"))
    if (r or {}).get("plan"):
        out["plan_id"] = r["plan"].get("id")
    return out


# ---------------------------------------------------------------- 技术情报

_UA = {"User-Agent": "Mozilla/5.0 (compatible; self-iterate/1.0)"}


def _http(url, timeout=9):
    try:
        import requests
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return ""


def _http_json(url, timeout=10):
    try:
        import requests
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return None


def fetch_arxiv(max_n=8):
    """arXiv 最新 AI/多模态论文（官方 Atom API）。"""
    url = ("http://export.arxiv.org/api/query?"
           "search_query=cat:cs.AI+OR+cat:cs.CV+OR+cat:cs.CL"
           "&sortBy=submittedDate&sortOrder=descending&max_results=%d" % max_n)
    txt = _http(url)
    if not txt:
        return []
    out = []
    for blk in re.findall(r"<entry>(.*?)</entry>", txt, re.S)[:max_n]:
        t = re.search(r"<title>(.*?)</title>", blk, re.S)
        s = re.search(r"<summary>(.*?)</summary>", blk, re.S)
        u = re.search(r"<id>(.*?)</id>", blk, re.S)
        title = re.sub(r"\s+", " ", (t.group(1) if t else "")).strip()
        summ = re.sub(r"\s+", " ", (s.group(1) if s else "")).strip()[:280]
        if title:
            out.append({"source": "arXiv", "title": title,
                        "url": (u.group(1).strip() if u else ""),
                        "summary": summ})
    return out


def fetch_github_trending(max_n=8):
    """GitHub 热门项目。

    ⚠️ 2026-11-15 实测：`https://github.com/trending` 网页在本机网络下
    **直连超时**（ReadTimeout 10s），但 `api.github.com` 正常返回 200。
    所以改用官方 Search API 取「近 30 天新建、星标较高」的项目 ——
    这比 Trending 页面更稳，而且能拿到 star 数和描述。
    """
    import datetime
    d30 = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    url = ("https://api.github.com/search/repositories"
           "?q=created:>%s+stars:>30&sort=stars&order=desc&per_page=%d"
           % (d30, max(1, min(max_n, 20))))
    j = _http_json(url)
    if not isinstance(j, dict):
        return []
    out = []
    for it in (j.get("items") or [])[:max_n]:
        name = it.get("full_name") or ""
        if not name:
            continue
        out.append({
            "source": "GitHub",
            "title": "%s ★%s" % (name, it.get("stargazers_count", "")),
            "url": it.get("html_url") or ("https://github.com/" + name),
            "summary": (it.get("description") or "")[:200],
        })
    return out


def fetch_hn(max_n=8):
    """Hacker News 热门。"""
    ids_txt = _http("https://hacker-news.firebaseio.com/v0/beststories.json")
    if not ids_txt:
        return []
    try:
        ids = json.loads(ids_txt)[:max_n]
    except Exception:                                   # noqa: BLE001
        return []
    # 2026-11-15：原来逐个请求，8 个 item 最坏要 48 秒。改成并发。
    def _one(i):
        t = _http("https://hacker-news.firebaseio.com/v0/item/%s.json" % i, timeout=6)
        if not t:
            return None
        try:
            j = json.loads(t)
        except Exception:                               # noqa: BLE001
            return None
        if not j.get("title"):
            return None
        return {"source": "HackerNews", "title": j["title"][:160],
                "url": j.get("url") or ("https://news.ycombinator.com/item?id=%s" % i),
                "summary": ""}

    from concurrent.futures import ThreadPoolExecutor
    out = []
    with ThreadPoolExecutor(max_workers=max(2, len(ids))) as ex:
        for it in ex.map(_one, ids):
            if it:
                out.append(it)
    return out


def fetch_cn_media(max_n=6):
    """中文技术媒体 RSS（机器之心 / 量子位）。"""
    feeds = ["https://www.jiqizhixin.com/rss", "https://www.qbitai.com/feed"]
    out = []
    for fu in feeds:
        txt = _http(fu, timeout=8)
        if not txt:
            continue
        for blk in re.findall(r"<item>(.*?)</item>", txt, re.S)[:max_n]:
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blk, re.S)
            l = re.search(r"<link>(.*?)</link>", blk, re.S)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if title:
                out.append({"source": "中文媒体", "title": title[:160],
                            "url": (l.group(1).strip() if l else ""), "summary": ""})
    return out[:max_n * 2]


def fetch_tech_all(progress=None):
    """四个来源**并发**抓，任何一个失败都不影响其它。

    2026-11-15 改动：原来是串行（arxiv→github→hn→cn），实测 111 秒，
    用户在这段时间里只看到一句「抓取中」，分不清是卡死还是在跑。
    改并发后总时长 ≈ 最慢那个源；每个源回来就回调一次 progress(total, done)，
    前端状态条能实时显示「已抓 12 条（arXiv ✓8 · GitHub ✓4）」。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    srcs = (("arxiv", "arXiv", fetch_arxiv),
            ("github", "GitHub", fetch_github_trending),
            ("hn", "HackerNews", fetch_hn),
            ("cn", "中文媒体", fetch_cn_media))
    got, done, total = {}, [], 0
    with ThreadPoolExecutor(max_workers=len(srcs)) as ex:
        futs = {ex.submit(fn): (k, label) for k, label, fn in srcs}
        for fu in as_completed(futs):
            k, label = futs[fu]
            try:
                got[k] = fu.result() or []
            except Exception:                           # noqa: BLE001
                got[k] = []
            total += len(got[k])
            done.append("%s ✓%d" % (label, len(got[k])))
            if progress:
                try:
                    progress(total, list(done))
                except Exception:                       # noqa: BLE001
                    logger.debug("silent-except", exc_info=True)
    # 保持固定顺序，方便前端/日志稳定阅读
    ordered = {k: got.get(k, []) for k, _, _ in srcs}
    return ordered, total


# ---------------------------------------------------------------- LLM

# JSON 修复的实现在 backend/jsonfix.py（self_iterate / papervec / reverify 共用一份，
# 免得三个模块各有一份弱实现、各踩一遍同样的坑）。这里只留薄委托。
def _strip_llm_notice(txt):
    return _jsonfix.strip_notice(txt)


def _json_balanced(txt):
    return _jsonfix.balanced(txt)


def _llm_text(prompt, max_tokens=8192, temperature=0.55, want_json=False):
    """调一次 LLM 取纯文本。

    ⚠️ 2026-11-16 修「上一轮反思失败：模型返回的不是合法 JSON」：
      ① 原来 max_tokens=4200，而反思的计划 JSON 实测约 11000 字节（中文），
         装不下 → 被截断 → 解析失败。提到 8192（API 上限）。
      ② `chat_stream` **没有**续写逻辑（续写只写在 chat_stream_with_tools 里），
         所以截断了也没人管。这里给 want_json=True 的调用补上：
         检测括号不配平 → 追加一轮「从中断处继续输出剩余 JSON」。
    """
    try:
        from . import keyvault
        from .deepseek_client import chat_stream, DeepSeekError
    except Exception as e:                              # noqa: BLE001
        return None, "导入 LLM 客户端失败：%s" % e
    key = ""
    try:
        key = keyvault.get_key()
    except Exception:                                   # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key（先在顶栏 🔑 填一个）"

    def _once(msgs, mt):
        buf = []
        for kind, txt in chat_stream(
                msgs, "deepseek-flash", key, temperature=temperature,
                max_tokens=mt, thinking=False,
                # 取 JSON 时不能把「（输出达到 max_tokens 上限被截断）」混进正文，
                # 那行中文会插在 JSON 中间，结构直接坏掉（2026-11-16 实测）
                truncation_notice=False):
            if kind == "content" and txt:
                buf.append(txt)
        return _strip_llm_notice("".join(buf).strip())

    try:
        txt = _once([{"role": "user", "content": prompt}], max_tokens)
        # 截断检测 + 自动续写（最多续 2 次，防止死循环）
        if want_json:
            for _ in range(2):
                if _json_balanced(txt):
                    break
                set_stage("think", "输出被长度上限截断，正在续写剩余 JSON…")
                more = _once([
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": txt},
                    {"role": "user", "content": (
                        "（系统提示）你上一条输出**被长度上限截断了**，JSON 没写完。"
                        "现在**从断开的地方继续输出剩余的 JSON**："
                        "不要重复已经写过的内容，不要重新开头，"
                        "不要加解释、不要加代码围栏，直接接着写。")},
                ], max_tokens)
                if not more:
                    break
                # 去掉可能被重复输出的开头
                more = re.sub(r"^\s*```(?:json)?", "", more).strip()
                txt = txt.rstrip() + more
        return txt, None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    """从 LLM 输出里抠出 JSON（含截断修复/脏格式清洗）。

    实现见 backend/jsonfix.py —— self_iterate / papervec / reverify 共用一份，
    免得三个模块各有一份弱实现、各踩一遍「截断 → 不是合法 JSON」的坑。
    """
    return _jsonfix.parse(txt)


def _dump_bad_raw(txt, where="reflect"):
    """解析失败时把原始输出落盘 —— 没有证据就只能靠猜。"""
    return _jsonfix.dump_raw(txt, _ROOT / "data" / "self_iterate", where)


# ---------------------------------------------------------------- 反思

def _journal_tail(n=24):
    """读自开发日志最后 n 条，作为"我最近改过什么"的素材。"""
    try:
        if not _JOURNAL.is_file():
            return []
        with open(_JOURNAL, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in lines:
            try:
                j = json.loads(ln)
            except Exception:                           # noqa: BLE001
                continue
            out.append({
                "ts": j.get("ts") or j.get("time") or "",
                "rel": j.get("rel") or j.get("file") or "",
                "note": (j.get("note") or "")[:160],
            })
        return out
    except Exception:                                   # noqa: BLE001
        return []


def _file_inventory(limit: int = 90) -> str:
    """现有可改文件的清单（只给文件名，给模型"照着挑"）。

    ⚠️ 为什么必须给（2026-11-15 实测）：反思产出的动作里出现了
    `backend/avatar_warp.py` —— 这个文件**根本不存在**（真正的是
    `avatar_lipsync.py` / `wav2lip_engine.py`）。模型凭印象编文件名，
    执行时要么失败、要么凭空造出一个垃圾文件。给清单是最省的办法。
    """
    groups = {}
    for sub, exts in (("backend", (".py",)), ("frontend", (".js", ".css", ".html"))):
        d = _ROOT / sub
        if not d.is_dir():
            continue
        try:
            names = sorted(p.name for p in d.iterdir()
                           if p.is_file() and p.suffix.lower() in exts)
        except Exception:
            continue
        names = [n for n in names
                 if not n.endswith(".bak") and ".bak-" not in n
                 and ".bak_" not in n and not n.endswith("_tmp")
                 and n != "__init__.py"]
        groups[sub] = names
    if not groups:
        return ""
    # ⚠️ 2026-11-16 实测：backend 有 70 个 .py，limit=70 时名额被占满，
    #   frontend 的 app.js / index.html / style.css **一个都进不了清单** ——
    #   打磨阶段模型当场指出「frontend/app.js 不在给定清单中，无法直接执行」，
    #   于是所有前端相关的改进动作都被判为不可执行。
    #   改成按目录留名额：前端固定留 12 个，剩下的全给后端。
    front = groups.get("frontend") or []
    back = groups.get("backend") or []
    keep_front = front[:12]
    keep_back = back[:max(0, int(limit) - len(keep_front))]
    shown = ["backend/%s" % n for n in keep_back] + \
            ["frontend/%s" % n for n in keep_front]
    total = len(back) + len(front)
    tail = "" if total <= len(shown) else "（另有 %d 个未列出）" % (total - len(shown))
    return ("\n\n## 现有文件清单（**只能从这里挑**，不要编造不存在的文件名）\n"
            + "、".join(shown) + tail)


_PROJECT_BRIEF = """本项目是一个"本地多模态助手"（Python FastAPI 后端 + 原生 JS 前端）：
· 对话/工具调用（联网搜索、读网页、地图、天气、图片生成/微改、文档 PPT Excel 生成、视频剪辑、语音合成、数字人）
· 知识库检索、长期/短期记忆、技能（skill）系统、定时任务、自开发（能改自己的源码）
· 数字人模块：warp 形变口型（纯 CPU、6ms/帧）、闭嘴基准合成、SDXL 局部重绘
· 已有的自我改进通道：selfdev_patch / selfdev_write / selfdev_restart
前端文件：frontend/index.html、app.js(约1.2万行)、style.css
后端文件：backend/*.py（main.py 约1.4万行、tools.py、avatar_*.py、cloud_image.py、keyvault.py 等）"""


_CREATIVE = _DATA / "creative.json"


def _creative_seeds(force=False):
    """独立通道生成「创意种子」——**故意不带任何痛点/失败数据**。

    为什么需要它（2026-11-16 实测）：主反思 prompt 里塞满了摩擦点、失败统计、
    能力缺口，模型读完只会想「怎么把这些毛病补上」。连跑两轮，产出的 features
    全是「证据卡 / 冷却闸门 / 基线对照」这类内部修补 —— 用户原话是「我需要 LLM
    去自主自由创新性地生成代码来提高用户体验」，而这些改动上线后用户**看不出
    任何区别**。

    所以把「想新点子」这件事单独拎出来：输入只有产品简介 + 真实文件清单，
    温度调高到 0.9，明确禁止提修 bug。结果注入主 prompt，要求至少采纳 1 条。
    缓存 24 小时：一天最多多花一次调用，不影响日常速度。
    """
    if not force:
        try:
            if _CREATIVE.is_file():
                d = json.loads(_CREATIVE.read_text(encoding="utf-8"))
                ts = float(d.get("ts") or 0)
                # ★ 2026-11-16 修：失败时原来写 ts=0，而 0 永远「超过 24 小时」，
                #   于是**每一次反思都会重新调用一次 LLM** —— 模型要是持续给不出
                #   合法 JSON，这就是个无底洞（每天 50 个计划 × 1 次额外调用）。
                #   现在失败也记时间戳，并给 30 分钟退避。
                if d.get("seeds") and (time.time() - ts < 86400):
                    return d
                if (not d.get("seeds")) and ts and (time.time() - ts < 1800):
                    return d          # 刚失败过，30 分钟内不重试
        except Exception:                               # noqa: BLE001
            logger.debug("creative cache read failed", exc_info=True)
    prompt = (
        "你是一位产品设计师，现在要为一个「本地多模态助手」想新点子。\n\n"
        + _PROJECT_BRIEF
        + "\n\n## 硬性要求\n"
        "· **禁止**提「修 bug / 补护栏 / 加兜底 / 优化现有流程 / 提高可靠性」这类内部改动，\n"
        "  一条都不要 —— 那些属于另一条通道，这里只要**用户能明显感知到的新体验**。\n"
        "· 判断标准：如果这个点子做完，用户打开界面会觉得「咦，这个不一样了」，才算。\n"
        "· 大胆想，不用考虑实现难度；但要说清**落在哪个文件**（只能从下面清单里选）。\n\n"
        "## 请从这些角度各想一个（然后挑最好的 3 个输出）\n"
        "1. 交互方式：现在几乎都是「打字→等一大段回复」，还能怎么用？\n"
        "2. 自动化：现在要用户手动做/反复交代的事，哪些能让它自己盯着做？\n"
        "3. 信息呈现：现在结果都是文字流，换种形式会不会更好读、更好用？\n"
        "4. 连续性与记忆：它记得用户什么？能不能提前把下一步准备好？\n"
        "5. 组合：把两个原本不相干的功能接起来，会不会有新用法？\n\n"
        + _file_inventory(limit=40)
        + "\n\n## 输出格式（严格 JSON，不要任何额外文字）\n"
        '{"seeds": [{"title": "点子名（12字内）", "what": "具体做什么（80字内）",'
        ' "feel": "用户会怎么感知到不一样（50字内）",'
        ' "files": ["backend/xxx.py"], "effort": "S|M|L"}]}'
    )
    set_stage("creative", "独立通道：脱离痛点，专门想新点子…")
    txt, err = _llm_text(prompt, max_tokens=1400, temperature=0.9)
    if err:
        # 失败也要写盘（带时间戳），否则退避判断拿不到「上次什么时候失败过」，
        # 每次反思都会重试一次 —— 见上面缓存检查处的说明。
        bad = {"ok": False, "ts": time.time(), "seeds": [], "error": str(err)[:300]}
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            _CREATIVE.write_text(json.dumps(bad, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except Exception:                               # noqa: BLE001
            logger.debug("creative cache write failed", exc_info=True)
        return bad
    data = _parse_json(txt)
    seeds = []
    if isinstance(data, dict):
        for s in (data.get("seeds") or [])[:5]:
            if not isinstance(s, dict):
                continue
            t = str(s.get("title") or "").strip()
            w = str(s.get("what") or "").strip()
            if not t or not w:
                continue
            seeds.append({
                "title": t[:40], "what": w[:200],
                "feel": str(s.get("feel") or "")[:120],
                "files": [str(x) for x in (s.get("files") or [])][:4],
                "effort": str(s.get("effort") or "M")[:2],
            })
    out = {"ok": bool(seeds), "ts": time.time(), "seeds": seeds,
           "error": "" if seeds else "模型没给出可用点子"}
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _CREATIVE.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        logger.debug("creative cache write failed", exc_info=True)
    return out


def _creative_block():
    """把创意种子渲染成 prompt 里的一段（拿不到就返回空串）。"""
    try:
        d = _creative_seeds()
    except Exception:                                   # noqa: BLE001
        logger.debug("creative seeds failed", exc_info=True)
        return ""
    seeds = (d or {}).get("seeds") or []
    if not seeds:
        return ""
    lines = ["", "", "## 💡 本轮创意种子（独立通道生成，**不带痛点偏见**）",
             "下面这些是脱离「当前毛病」想出来的新体验。本轮 features 里"
             "**至少要采纳 1 条**（可以改写得更具体，但不要换成修补类）："]
    for i, s in enumerate(seeds[:4], 1):
        lines.append("%d. **%s** —— %s" % (i, s.get("title"), s.get("what")))
        if s.get("feel"):
            lines.append("   用户感知：%s" % s.get("feel"))
        if s.get("files"):
            lines.append("   落点：%s" % "、".join(s.get("files")))
    return "\n".join(lines)


def _build_reflect_prompt(signals, tech, journal, focus="", exp="", paper_brief="",
                          gap="", task_brief=""):
    sig_lines = []
    for it in signals[-30:]:
        u = (it.get("user") or "").replace("\n", " ")[:220]
        a = (it.get("assistant") or "").replace("\n", " ")[:220]
        if u:
            sig_lines.append("- 用户：%s\n  我：%s" % (u, a))
    tech_lines = []
    for k, arr in (tech or {}).items():
        for it in (arr or [])[:6]:
            tech_lines.append("- [%s] %s %s" % (it.get("source"), it.get("title"),
                                                it.get("url") or ""))
    jr = ["- %s %s %s" % (x.get("ts"), x.get("rel"), x.get("note")) for x in journal[-40:]]

    return """你是这个本地多模态助手项目的"自我迭代引擎"。请做一次**诚实、具体、不客套**的自我审视。

%s

## 最近的对话（用户说了什么、你怎么回的）
%s

## 我最近已经做过的改动（⚠️ 这些**已经修过了**，绝对不要再把它们提成 action）
%s

## 用户体验评估（真实打分，这是**最重要的输入之一**）
%s

## 最近的技术动态（真实抓取，可能与本项目无关，请自己判断相关性）
%s

## 论文/新技术的四维评估（另一条通道给出，含可行性/创新性/效益/效率打分与落地计划）
%s
⚠️ **theory 字段的硬约束**：只有写「上面这份评估里真实存在的论文/技术」才算数，
   并且要写出标题或来源（例：`arXiv:2401.xxxxx` 或论文标题）。上面没有相关论文的，
   theory 就留空 —— **不许凭记忆编造「某论文提出…」**。编出来的理论比没有更糟：
   用户会据此判断这件事值不值得做，编一个就等于骗他做决定。
⚠️ 这里的 verdict=skip 就是**明确不该做**，别把它再包装成 action 提一遍。
   verdict=adopt_now / evaluate 的，它的 plan 已经是分步骤的，你要做的是判断
   「现在值不值得插进本轮动作」——值得就把它转成 actions 里的条目（保留它的
   验证方式），不值得就在 summary 里说一句为什么先不做。

## 过往任务经验与工作效率（按当前痛点向量召回，**真实执行记录**，不是建议）
%s
⚠️ 这一段是**你自己过去做过的事**，不是别人的意见。
   开头那几行「效率概览」是硬数字：成功率、被改最多/失败最多的文件、时间跨度 ——
   这些不是自评，是从动作日志统计出来的。
   ⚠️ **成功率低 ≠ 该少做创新**：它只说明「一次改太多、难以验证」。正确对策是
   把改动拆小、每条自带可执行断言、依赖失败自动回滚 —— 而**不是**停止尝试新东西。
   · 标 ⚠️ 的是失败/低分经验 —— 重点看「问题」和「当时想到的改法」。本轮若又
     遇到同类问题，**先沿用当时的改法**，不要从零试错；同一坑踩两次就是浪费。
   · 标 ✅ 的是成功经验 —— 能复用的做法直接复用。
   · 「已构思，未执行」说明那个想法立项了却没落地。若现在依然值得做，可以重新
     提，但必须说清「上次为什么没做成、这次凭什么能做成」。
   ⚠️ 但**不要照搬**：当时的痛点可能已经变了，先判断是否仍然适用。宁可不用，
   也不要把过时的经验硬套到新问题上。

## 能力缺口（从真实执行日志统计，不是猜的）
%s
⚠️ 如果这里显示某个文件被反复改、或某类动作反复失败，说明**当前做法行不通**。
   要么先想清楚真正的根因再动手，要么换一条完全不同的思路 ——
   绝对不要在同一条路上继续叠加改动（那是"越改越乱"的来源）。

## 🎨 创意发散（**先做这一步，再往下看**）
上面那些输入**全是问题**（摩擦、失败、缺口）。只看问题，想出来的必然只有修补。
所以先做一次纯发散：**完全不考虑修 bug / 补护栏 / 优化现有流程**，只问一件事：

> 这个产品还可以变成什么样，让用户觉得「哇，这个真省事」？

从下面 5 个角度各想一个（天马行空也行，不用管现在能不能实现）：
· **交互方式**：现在几乎都是「打字 → 等一大段回复」。还能怎么用？（少打字？不用打字？边看边改？）
· **自动化**：现在需要用户手动做/反复交代的事，哪些可以变成它自己盯着做？
· **信息呈现**：现在结果都是文字流。换成别的形式会不会更好读、更好用？
· **连续性与记忆**：它记得用户什么？能不能提前把下一步准备好，而不是等用户开口？
· **组合**：把两个原本不相干的功能接起来，会不会产生新用法？

从这 5 个里挑 **1~2 个最有价值**的，写进 `features`（必须有 assert_code）。
⚠️ 判据：如果这条东西上线后**用户看不出任何区别**，那它不算创意，别拿来充数。

## 你的任务

## ⚡ 创新授权（用户 2026-11-16 明确下达，优先级最高）
用户原话：「我需要 LLM 去自主自由创新性地生成代码来提高用户体验，**只要不搞坏就行**」。
这意味着：

1. **大胆想，不要自我审查**。你有权提出任何让用户体验变好的想法 —— 新交互、
   新能力、新流程、把两个不相干的功能组合起来、借鉴论文里还没人实现的做法，
   都可以。**"以前没人这么做过"不是拒绝的理由**。
2. **"不搞坏"由系统保证，不用你牺牲想法来换**。每次改动都有：备份 → 应用 →
   语法校验 → 行为回归（含改动前基线对比）→ 新功能断言自修 → 失败自动回滚。
   所以你尽可以提得更大胆 —— 改坏了系统会自己退回去，不会留在用户机器上。
3. **唯一要守的纪律是"可验证"**：每条动作/功能都要能说清「改完怎么知道它对了」
   （verify），功能类还要给出可执行断言（assert_code）。**说不清验证方式 ≠ 想法不好，
   只是这一步还没想透**，那就在 detail 里写清还差什么，别因此把想法砍成保守的小修补。
4. **可靠性是护栏的职责，不是不创新的理由**。修 bug、加兜底属于 actions 范畴，
   可以写，但**不能占满整轮** —— 必须同时给出用户能感知的新东西。
5. ★ **每轮至少两条"用户会明显感觉到不一样"的改动**（2026-11-16 打磨实测反馈：
   只给一条时，整份计划仍被判「创新密度偏低、接近全是补丁的红线」）。
   判断标准：如果这条改动上线后用户看不出任何区别，它就不算创新，只是内部重构 ——
   这类放进 actions 可以，但**不能用来充数**满足创新配额。
   自检：写完后数一下 actions 里到底有几条是「用户会主动夸一句」的，不够 2 条就再想。

---

## 你的任务（按下面 1~8 的顺序做，编号与上面「创新授权」无关）

1. **自我反思**：最近哪些地方没做好？要具体到"哪类任务/哪个环节/为什么会这样"，
   不要写"我会更努力"这种空话。优先找**反复出现**的问题。
2. **对用户的观察**：从对话里看出用户的工作习惯、反复提出的诉求、被浪费的时间。
   只写有证据的，没证据就不要编。
3. **消除摩擦 + 创造新体验（同等重要，不设优先级压制）**：
   (a) 上面「用户体验评估」里反复出现的摩擦点，必须给出具体到环节的消除办法。
       过程繁琐就去砍步骤，评分低就去查是不是我的做法本身有问题——**不要写
       "我会更仔细"这种空话**。
   (b) **同时**必须提出「以前根本没有的能力」：用户明确要求「自主自由创新性地
       生成代码来提高用户体验，只要不搞坏就行」。所以本轮**至少要有一条**属于
       全新体验的想法（不是修 bug、不是补护栏、不是优化现有流程），并写进 features。
       找不到就说明你在偷懒 —— 任何软件都永远有可以更好的地方。
4. **预判用户接下来要做什么**（新增，很重要）：从对话里推断用户**未来可能处理
   什么业务**、**当下已经卡在哪**。只写有证据的，每条必须带证据（用户原话/反复
   出现的模式）和把握程度（high/mid/low）。没证据就不要编 —— 编出来的预判比没有更糟。
5. **可增加的功能 / 新技术**：结合技术动态、论文评估和上面的预判，提出真正值得做的。
   ⚠️ **允许提「技术还没成熟、但理论上可行」的方案**（借鉴论文即可），只要对用户
   真有用。用户明确说过：哪怕这项技术还没出来，只要有用且理论可行就可以做。
   ⚠️ 但**必须说清落在哪**：改哪些文件（files，只能从上面的清单里选）、改哪个函数
   （anchor）。说不清 files/anchor 的建议不算有效 —— 那说明你自己也没想清怎么落地。
   ⚠️ 如果上面「真实运行指标」显示成功率偏低：**用护栏提高可靠性，不要放弃创新**。
      具体做法 = 把改动拆得更小 + 自带可执行断言 + 依赖自动回滚兜底。
      「成功率低」从来不是"这轮不创新"的理由 —— 那只会让系统永远停在修补阶段。
   ★ **features 与 actions 的分工必须分清**（用户 2026-11-15 明确要求「完全能自主
     增加新功能」）：修 bug、补护栏、改现有流程 → 写进 actions；
     **凭空新增的能力**（以前根本没有的功能/技术）→ 写进 features。
     features **至少给 2 条**（确实没有值得加的才允许更少，并在 summary 里说明原因）。
     不要因为「怕重复」就把所有想法都塞进 actions —— 那样自主创新永远落不了地。
   ⚠️ **同一个需求只能写一处**：要改代码就写进 actions，只提建议就写进 features，
      绝对不要两处都写 —— 实测这样会产生两条动作改同一处，既浪费名额又互相覆盖。
   ⚠️ 也不要在 features 里写一条**概括**、再在 actions 里写同一件事的**细分**
      （例如 features 写「回复形态闸门：结论前置+过程折叠+长度上限」，actions 又拆成
      三条）—— 那会变成四条动作改同一处。粒度只选一种：要么整体写进 actions 一条，
      要么拆细写进 actions 多条。
6. **想问用户的问题**（新增）：某个预判缺关键信息、且猜错代价很大时，提一个问题。
   只问「问了能改变你怎么做」的，别凑数；没有就给空数组。
7. **可执行的动作**：这是最重要的一项。给出**具体到文件**的改动计划，
   每条动作要能被单独批准/拒绝。风险等级自己判断（low=文案/参数/样式，
   mid=新增函数/接口，high=改核心逻辑）。
   ⚠️ 只允许改 backend/ 和 frontend/ 下的 .py/.js/.css/.html。
   ⚠️ **大改也可以提**（重构、换架构、新模块都行 —— 每次改动都有备份 +
      语法校验 + 行为回归 + 失败自动回滚，搞坏会自己退回去）。
      但必须**拆成能单独验证的几步**，不要一次甩出一个无法定位问题的巨型改动：
      每一步都要能说清「改完怎么知道它对了」（verify 字段）。
   ⚠️ 如果确实没有值得改的，actions 就给空数组 —— 不要为了凑数硬提。

8. **features 里的 `assert_code` 必填**（新功能的验收断言）。它会被写成一个
   独立文件、在回归里**真的执行**，所以必须满足：
   · 完整可运行的 Python，且**必须定义 `def run():`**；
   · `run()` 里用 `assert` 判断「这个功能真的可用」，不满足就让它抛 AssertionError；
   · 只能 import 本项目模块和标准库；**禁止**联网、起子进程、读写/删除文件、
     出现 os.remove / subprocess / requests / socket / shutil.rmtree 等调用
     —— 命中会被直接拒绝写入，功能也就落不了地；
   · 必须**秒级**跑完（不加载模型、不调 LLM、不联网）；
   · 写法示意（内容按你提的功能写，别照抄）：
     def run():
         from backend import selfevolve
         st = selfevolve.status()
         assert isinstance(st, dict) and "count" in st, "status() 应返回含 count 的字典"
   ⚠️ 写不出可执行断言的 feature，宁可不提 —— 没有断言就无法验证新功能是否正常。

## 输出格式（严格 JSON，不要任何额外文字）
{
  "summary": "一句话总结这次反思（30字内）。⚠️禁止写成「先修X再谈创新」「先做Y再考虑新功能」这类把创新排到后面的总结；要体现本轮具体做了什么、加了什么用户能感知的新东西",
  "weaknesses": [{"title":"短标题","detail":"具体问题+证据","severity":"high|mid|low"}],
  "user_insights": [{"title":"短标题","detail":"观察+证据","suggestion":"可以怎么改进"}],
  "forecast": [{"biz":"用户未来可能处理的业务","evidence":"依据（原话/重复出现的模式）","confidence":"high|mid|low","prepare":"可以提前准备什么"}],
  "needs": [{"pain":"当下遇到的问题","evidence":"证据","impact":"影响了什么"}],
  "features": [{"title":"功能名","why":"为什么值得做（必须说清）","pain":"对应哪个痛点或预判","how":"大概怎么做","effort":"S|M|L","files":["backend/xxx.py","frontend/xxx.js"],"anchor":"改哪个函数","risk":"low|mid|high","verify":"怎么验证有效","theory":"理论/论文依据，没有就留空","assert_code":"必填，见下方说明"}],
  "questions": [{"q":"想问用户的一句话","why":"为什么这个信息会改变做法","options":["选项1","选项2"]}],
  "tech_takeaways": [{"title":"技术名","source":"来源","url":"链接","takeaway":"一句话要点","applicable":"对本项目有没有用，怎么用"}],
  "actions": [{"title":"短标题","detail":"具体改什么、为什么","risk":"low|mid|high","files":["backend/xxx.py"]}]
}

%s""" % (_PROJECT_BRIEF,
        "\n".join(sig_lines) or "（暂无）",
        "\n".join(jr) or "（暂无）",
        exp or experience_brief(),
        "\n".join(tech_lines) or "（本次没抓到，可能没联网）",
        paper_brief or "（本轮没有可用的论文评估）",
        task_brief or "（本轮没有召回到相似的历史任务）",
        gap or "（暂无执行记录统计）",
        ("## 用户这次的额外关注点\n" + focus) if focus else "")


# ---------------------------------------------------------------- 计划打磨
# 用户 2026-11-16 要求：「不断完善计划，直到计划可以了就可以执行实施了」，
# 判定标准他自己定了：「模型自评 8 分以上，或打磨满 3 轮就执行」。
# 所以计划生成后**不直接执行**，先让模型自己挑毛病、重写动作清单，最多 3 轮。
#
# 为什么放在这一层而不是重写 _reflect_impl：生成计划那段有两千多行、
# 牵扯论文/工作召回/功能转换等一堆输入，动它风险太高。这里只消费它的产物。

_POLISH_TARGET = 8.0
# ★ 2026-11-16 用户指出「打磨要花 3 次 LLM 调用，开销太大」，默认从 3 降到 2。
#   实测 3 轮的最好成绩也只有 7.0（8 分目标基本达不到），第 3 轮多是白跑甚至更差。
_POLISH_ROUNDS = 2


def _polish_prompt(plan, prev):
    """构造评审提示。把真实文件清单塞进去，否则模型会编不存在的路径。"""
    acts = []
    for a in (plan.get("actions") or []):
        acts.append("- [%s] %s\n  做法：%s\n  文件：%s"
                    % (a.get("risk") or "mid", a.get("title") or "",
                       (a.get("detail") or "")[:400],
                       "、".join(a.get("files") or []) or "（未指定）"))
    hist = ""
    if prev:
        hist = "\n\n上一轮自评 %.1f 分，当时指出的问题：\n%s" % (
            float(prev.get("score") or 0),
            "\n".join("- " + str(x) for x in (prev.get("issues") or [])[:6]))
    return (
        "你是这个项目的技术负责人。下面是一份**自我迭代计划**，"
        "请你严格评审它的成熟度，并给出改进后的版本。\n\n"
        "计划摘要：%s\n\n"
        "动作清单：\n%s\n\n"
        "项目真实文件（只能从这里选）：\n%s%s\n\n"
        "评分标准（1~10，两个维度**都要**看）：\n"
        "【维度A 可执行性】\n"
        "· 具体到改哪个文件、怎么改、怎么验证 → 高；\n"
        "· 太笼统、执行时还得自己猜 → 中；\n"
        "· 空泛、重复、根本不可行 → 低。\n"
        "【维度B 用户价值与创意】（⚠️ 权重不低于维度A）\n"
        "· 上线后用户能**明显感觉到变好**（新能力/新交互/砍掉一个反复的麻烦） → 高；\n"
        "· 只是内部重构、用户看不出区别 → 中；\n"
        "· 纯粹补护栏/加兜底，用户毫无感知 → 低。\n\n"
        "综合：8~10 = 两个维度都站得住；5~7 = 一个强一个弱；1~4 = 空泛或毫无用户价值。\n\n"
        "⚠️⚠️ **打磨纪律（必须遵守）**：\n"
        "1. **不许把大胆的想法改小**。如果某条动作原本是个新体验，你可以把它写得更具体、\n"
        "   拆成可验证的步骤，但**不能把它替换成保守的小修补**。那样打磨就是负价值。\n"
        "2. **不许以「风险高/实现难」为由删掉新功能**。风险高就标 high、给更严的验证方式，\n"
        "   而不是砍掉 —— 系统有备份和自动回滚，改坏了自己会退回去。\n"
        "3. **如果整份计划全是补丁式改动、没有任何用户可感知的新东西，score 直接判 ≤4**，\n"
        "   并在 issues 里明确写「缺少创新，全是内部修补」。\n\n"
        "只输出 JSON，不要任何解释：\n"
        '{"score": 分数, "issues": ["具体缺陷1", "缺陷2"],'
        ' "actions": [{"title": "...", "detail": "具体到文件和做法",'
        ' "risk": "low|mid|high", "files": ["backend/xxx.py"]}]}\n'
        "注意：actions 要给**完整清单**（不是只给改动的），最多 6 条。"
        % (str(plan.get("summary") or "")[:300],
           "\n".join(acts) or "（空）",
           _file_inventory(limit=70), hist)
    )


def polish_plan(plan, max_rounds=None, target=None):
    """自评 + 改进，直到达标或轮次用完。返回 (新计划, 元信息)。

    每轮一次 LLM 调用；轮次上限既是质量兜底，也是花钱兜底。
    任何一轮失败都**保留已有成果**继续 —— 绝不能让打磨把计划搞没。
    """
    s = load_settings()
    if max_rounds is None:
        max_rounds = int(s.get("polish_rounds") or _POLISH_ROUNDS)
    if target is None:
        target = float(s.get("polish_target") or _POLISH_TARGET)
    max_rounds = max(1, min(int(max_rounds), 5))
    meta = {"ok": True, "rounds": 0, "score": None, "target": target,
            "max_rounds": max_rounds, "log": [], "changed": 0}

    # ★ 2026-11-16 用户指出「打磨要花 3 次 LLM 调用，开销太大」。
    #   这里加一道**纯本地、不花钱**的预检：如果动作清单每条都已经指了
    #   真实文件、detail 也够长、条数够多，说明模型这次已经想得很具体，
    #   再打磨大概率只是复述 → 只给 1 轮。
    #   这是启发式：判错最多多花一轮，不会让计划变差。
    _orig_rounds = max_rounds
    _acts0 = [a for a in (plan.get("actions") or []) if isinstance(a, dict)]
    if len(_acts0) >= 3:
        _concrete = sum(
            1 for a in _acts0
            if (a.get("files") or []) and len(str(a.get("detail") or "")) >= 60)
        if _concrete == len(_acts0):
            max_rounds = 1
            meta["max_rounds"] = 1
            meta["skip"] = "already_concrete"
    prev = None
    cur = plan
    # ⚠️ 2026-11-16 实测：打磨分数可能**下降**（真实跑出 6.0 → 6.0 → 5.0）。
    #   原来无条件用最后一版，等于「越改越差还要执行最差那版」。
    #   所以这里记住每轮被打分的那一版，最后返回**得分最高**的。
    best_score = -1.0
    best_plan = plan
    prev_score = None
    # ⚠️ 2026-11-16 实测踩到的坑：原来写 `for i in range(max_rounds)`，
    #   而 range() 在循环**开始时就把次数定死**了 —— 循环体里再改 max_rounds
    #   完全无效，导致「够用即停」和「低分补轮」两个机制形同虚设。
    #   改成 while 才能真的动态调整轮数。
    i = 0
    while i < max_rounds:
        set_stage("polish", "第 %d 轮打磨：自评计划成熟度…" % (i + 1))
        # ★ 2026-11-16 用户指出「打磨要花 3 次 LLM 调用，开销太大」。
        #   实测典型序列 2.0 → 7.0 → 6.0：第 3 轮不但没变好，反而更差。
        #   token 从 3000 降到 2200（actions 最多 6 条，2000 足够），配合下面
        #   两道早停，典型情况从 3 次调用降到 2 次。
        txt, err = _llm_text(_polish_prompt(cur, prev), max_tokens=2200,
                             temperature=0.3)
        if err:
            meta["error"] = err
            break
        data = _parse_json(txt)
        if not isinstance(data, dict):
            meta["error"] = "模型返回的不是合法 JSON"
            break
        try:
            score = float(data.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        issues = [str(x)[:200] for x in (data.get("issues") or [])][:8]
        meta["rounds"] = i + 1
        meta["last_score"] = round(score, 1)
        meta["log"].append({"round": i + 1, "score": round(score, 1),
                            "issues": issues})
        # 这一轮评的就是 cur 本身 → cur 的得分就是 score
        if score > best_score:
            best_score = score
            best_plan = cur
        prev = {"score": score, "issues": issues}
        # 采纳模型重写的动作清单（只在它真的给了非空清单、且没达标时）
        new_acts = []
        for j, a in enumerate(data.get("actions") or []):
            if not isinstance(a, dict) or not (a.get("title") or "").strip():
                continue
            files = [_norm_rel(f) for f in (a.get("files") or [])
                     if isinstance(f, str)]
            new_acts.append({
                "id": "a%d" % (j + 1),
                "title": str(a.get("title"))[:120],
                "detail": str(a.get("detail") or "")[:900],
                "risk": (a.get("risk") or "mid").lower(),
                "files": [f for f in files if f][:_MAX_FILES_PER_ACTION],
                "status": "pending", "note": "", "error": "", "backup": "",
            })
        if new_acts and score < target:
            cur = dict(cur)
            cur["actions"] = new_acts
            meta["changed"] = int(meta.get("changed") or 0) + 1
        # ⚠️ 预检跳过有个坑：只评了一次就给 2 分的话，计划会被后面的
        #   「自评<5 → 只出报告」判死，而它可能只是第一轮没评准。
        #   所以低分时把轮数恢复回来，补一轮再说 —— 省钱的启发式不能反过来
        #   把好计划毙掉。
        if i == 0 and meta.get("skip") == "already_concrete" and score < 5.0:
            max_rounds = _orig_rounds
            meta["skip"] = "already_concrete_retry"
            meta["max_rounds"] = max_rounds
        if score >= target:
            meta["stop"] = "reached"
            break
        # ★ 2026-11-16 用户要求「打磨别花 3 次调用」。两道早停：
        #   ① 停滞即停：比上一轮涨不到 0.5 分，说明已经收敛，再跑纯烧钱
        #      （实测 2.0 → 7.0 → 6.0，第 3 轮就是白花的）；
        #   ② 够用即停：首轮就到 target-1 且没有 high 级缺陷，只再给 1 轮机会。
        #   注意：早停只影响「还要不要继续评」，**不影响 best_plan 的选取** ——
        #   本轮得分和它评的那一版已经记进 best 了，不会丢成果。
        if prev_score is not None and score < prev_score + 0.5:
            meta["stop"] = "stalled"
            break
        # 首轮就够好的，不必再跑：
        #   ≥ target-0.5（如 7.5/8）→ 直接停，再打磨基本到不了 8，纯白花；
        #   ≥ target-1.0（如 7.0）  → 只再给 1 轮机会。
        # 有 high 级缺陷时不走这条，那种必须继续打磨。
        if i == 0 and not any("high" in str(x).lower() for x in issues):
            if score >= (target - 0.5):
                meta["stop"] = "good_enough"
                break
            if score >= (target - 1.0):
                max_rounds = min(max_rounds, 2)
                meta["stop"] = "near"
        prev_score = score
        i += 1
    if best_score >= 0:
        meta["score"] = round(best_score, 1)
    meta["done"] = bool((meta.get("score") or 0) >= target)
    return best_plan, meta


def reflect(trigger="manual", use_web=True, focus="", auto_execute=None,
            polish=None):
    """跑一次完整反思（带运行状态包装）。

    auto_execute=None → 看设置里的 trust_mode（信任模式开着就自动执行）；
    auto_execute=True/False → 强制指定（前端手动触发时用）。

    包装的目的：内部实现有多条 return（LLM 报错、JSON 非法、正常完成），
    每条都要把 running 置回 False，否则前端会一直转圈。
    """
    _run_begin(trigger)
    try:
        out = _reflect_impl(trigger=trigger, use_web=use_web,
                            focus=focus, auto_execute=auto_execute,
                            polish=polish)
    except Exception as e:                              # noqa: BLE001
        _run_end(err="%s: %s" % (type(e).__name__, e))
        raise
    _run_end(err="" if out.get("ok") else str(out.get("error") or "未知错误"),
             plan_id=str(((out.get("plan") or {}).get("id")) or ""))
    return out


def _reflect_impl(trigger="manual", use_web=True, focus="", auto_execute=None,
                  polish=None):
    _ensure()
    tech, tech_total = ({}, 0)
    if use_web:
        set_stage("fetch", "并发抓取 4 个来源…")
        tech, tech_total = fetch_tech_all(
            progress=lambda n, done: set_stage(
                "fetch", "已抓 %d 条（%s）" % (n, " · ".join(done))))
        set_stage("fetch", "抓到 %d 条技术动态" % (tech_total or 0))
    sig = load_signals().get("items", [])
    journal = _journal_tail()
    exp_txt = experience_brief()

    # ★ 2026-11-15 用户要求：「抓取到的论文也要向量化提高召回率，并且要
    #   主动思考可行性、创新性、效益、效率，再给出周密的自迭代计划」。
    #   这里做三件事：
    #     ① 本次抓到的全部入库（去重）—— 历史抓过的不会再丢；
    #     ② 用**当前痛点**当查询，从累积库里向量召回相关论文
    #        （两周前抓的那篇也可能正是现在需要的）；
    #     ③ 对「新抓的 arXiv + 召回的历史」做四维评估，产出分步计划。
    paper_brief = ""
    paper_meta = {}
    try:
        from . import papervec
        set_stage("paper", "论文入库去重…")
        flat = [x for arr in (tech or {}).values() for x in (arr or [])]
        if flat:
            paper_meta["added"] = papervec.add_batch(flat)
        # ★ 2026-11-15 修致命 bug：add_batch() 返回的是 dict
        #   {"added":2,"dup":28,"total":32}，原来直接拿它去 %d 格式化 →
        #   TypeError: %d format: a real number is required, not dict，
        #   整个论文块被 except 吞掉 → paper_brief 为空 → features 的 theory
        #   全空 → 模型提的新功能**完全没有论文依据**（实测 20261115_202048 那轮）。
        _added = paper_meta.get("added")
        if isinstance(_added, dict):
            _n_added = _added.get("added") or 0
        else:
            _n_added = _added or 0
        set_stage("paper", "新增入库 %d 篇，向量召回中…" % _n_added)
        # ★ 2026-11-15 新增：按**当前痛点**定向抓 arXiv 论文入库。
        #   为什么需要：库里 32 篇里 16 篇正文不足 20 字，内容全是「SpaceX 星舰」
        #   「折纸飞机」这类新闻，跟本项目痛点毫无关系 → search(痛点) 经常 0 条
        #   → features 的 theory 只能写「无论文，工程可行」。
        #   这里先用痛点转英文检索词，直接查 arXiv 官方 API 补一批**对口**的，
        #   再走下面的召回 + 四维评估。抓取失败只是少一批输入，不影响主流程。
        try:
            from . import need2query
            set_stage("paper", "按痛点定向检索 arXiv…")
            paper_meta["harvest"] = need2query.harvest(
                ((exp_txt or "")[:400] + " " + (focus or "")).strip(),
                max_queries=3, per_query=3, timeout=20)
        except Exception as _he:                          # noqa: BLE001
            paper_meta["harvest_error"] = "%s: %s" % (type(_he).__name__, _he)
        q = ((exp_txt or "")[:500] + " " + (focus or "")).strip()
        recalled = papervec.search(q, top_k=6, only_unevaluated=True) if q else []
        fresh = [i for i in flat if str(i.get("source") or "") == "arXiv"][:5]
        to_eval, seen_t = [], set()
        for it in (fresh + recalled):
            t = str(it.get("title") or "")
            if t and t not in seen_t:
                seen_t.add(t)
                to_eval.append(it)
        if to_eval:
            brief = _PROJECT_BRIEF
            try:
                brief += _file_inventory(limit=70)
            except Exception:
                logger.debug("silent-except", exc_info=True)
            set_stage("paper", "四维评估 %d 篇（可行性/创新性/效益/效率）…"
                      % len(to_eval[:6]))
            r = papervec.evaluate(to_eval[:6], project_brief=brief,
                                  pains=exp_txt, max_n=6)
            paper_meta["eval"] = {"ok": r.get("ok"), "n": r.get("evaluated"),
                                  "error": r.get("error") or ""}
            set_stage("paper", "已评估 %d 篇" % (r.get("evaluated") or 0))
        paper_brief = papervec.brief_for_prompt(5)
        paper_meta["stats"] = papervec.stats()
        paper_meta["recalled"] = len(recalled)
    except Exception as e:                              # noqa: BLE001
        # 论文这条通道挂了不能拖垮整个反思（它只是输入之一）
        paper_meta["error"] = "%s: %s" % (type(e).__name__, e)

    # ★ 能力缺口：从**真实执行日志**统计「反复做不到的事」（成功率 / 反复改同一文件 /
    #   反复失败的动作）。原来 _build_reflect_prompt 有 gap 参数却从没传过 ——
    #   等于这条输入一直是空的，模型只能靠对话摘要猜。
    gap_txt = ""
    try:
        from . import selfevolve
        gap_txt = selfevolve.gap_prompt()
        paper_meta["gaps"] = (selfevolve.gaps() or {}).get("summary") or ""
    except Exception as e:                              # noqa: BLE001
        paper_meta["gap_error"] = "%s: %s" % (type(e).__name__, e)

    # ★ 2026-11-15 用户要求：「不仅是论文，包括平时工作任务上的效率和成果，
    #   是否能够向量检索思考并成功与其他内容结合如论文进行构思」。
    #   论文那条通道给的是**外部理论**，这里补**内部实践**：从自己过去的
    #   任务/构思/评估里向量召回「上次遇到类似问题怎么做的、成没成」。
    #   两条一起喂给模型，它才可能既参考论文、又复用自己踩过的坑。
    task_brief = ""
    _tq = ((exp_txt or "")[:300] + " " + (focus or "")).strip()

    # ---- 通道 A：workvec（做成了什么：action/plan/journal）----
    try:
        from . import workvec
        set_stage("task", "向量召回过往工作过程与成效…")
        try:
            workvec.sync()          # 文本没变时是秒回的（只比指纹）
        except Exception:           # noqa: BLE001
            logger.debug("workvec sync skipped", exc_info=True)
        _ws = workvec.eff_stats()
        paper_meta["work"] = _ws if isinstance(_ws, dict) else {}
        task_brief = workvec.brief_for_prompt(_tq, top_n=5) or ""
        paper_meta["work"]["chars"] = len(task_brief)
    except Exception as e:                              # noqa: BLE001
        paper_meta["work_error"] = "%s: %s" % (type(e).__name__, e)

    # ---- 通道 B：taskvec（上次同样的问题，用户给了几分、为什么低）----
    # ⚠️ 2026-11-15：原来这段嵌在上面 workvec 的 try 里，workvec 一旦 import 失败
    #   或抛异常，taskvec 会**连带失效**，而且 task_brief 直接变空、毫无提示。
    #   两条通道互相独立才可靠：一条挂了另一条照常给输入。
    try:
        from . import taskvec
        set_stage("task", "向量召回历史任务成败经验…")
        taskvec.build_index()       # 文本没变时只比指纹，秒回
        _tb2 = taskvec.brief_for_prompt(top_n=3, query=_tq)
        if _tb2:
            task_brief = ((task_brief or "") + "\n\n" + _tb2).strip()
        paper_meta.setdefault("work", {})["taskvec_chars"] = len(_tb2 or "")
    except Exception as e:                              # noqa: BLE001
        paper_meta["taskvec_error"] = "%s: %s" % (type(e).__name__, e)
    prompt = _build_reflect_prompt(sig, tech, journal, focus=focus,
                                   exp=exp_txt, paper_brief=paper_brief,
                                   gap=gap_txt, task_brief=task_brief)
    # ★ 2026-11-16：注入「创意种子」。主 prompt 里全是痛点和失败数据，模型只会
    #   想修补；这里塞进一条独立通道想出来的、不带痛点偏见的新点子，并要求采纳。
    try:
        prompt += _creative_block()
    except Exception:                                   # noqa: BLE001
        logger.debug("creative block failed", exc_info=True)
    # 工作过程 / 效率 / 成果已并入上面的 task_brief（同一个 workvec 数据源）。
    # 分两段拼会把同样的内容进 prompt 两遍，白烧 token。
    # 真实文件清单：不给它，模型会编出 `avatar_warp.py` 这种不存在的路径
    try:
        prompt += _file_inventory()
    except Exception:
        logger.debug("silent-except", exc_info=True)
    set_stage("think", "把 %d 轮对话 + %d 条技术动态喂给模型…"
              % (len(sig), tech_total or 0))
    txt, err = _llm_text(prompt, max_tokens=8192, temperature=0.55,
                         want_json=True)
    if err:
        return {"ok": False, "error": err}
    set_stage("plan", "模型已返回，整理动作清单…")

    data = _parse_json(txt)
    if not isinstance(data, dict):
        # 落盘原始输出 —— 2026-11-16 那次失败就是因为没留证据，只能现查
        _bad = _dump_bad_raw(txt, "reflect")
        return {"ok": False,
                "error": "模型返回的不是合法 JSON（原始输出已存 %s）" % _bad,
                "raw": (txt or "")[:600]}

    actions = []
    for i, a in enumerate(data.get("actions") or []):
        if not isinstance(a, dict) or not (a.get("title") or "").strip():
            continue
        files = [f for f in (a.get("files") or []) if isinstance(f, str)]
        files = [_norm_rel(f) for f in files if _norm_rel(f)]
        if not files:
            files = []
        actions.append({
            "id": "a%d" % (i + 1),
            "title": str(a.get("title"))[:120],
            "detail": str(a.get("detail") or "")[:900],
            "risk": (a.get("risk") or "mid").lower(),
            "files": files[:_MAX_FILES_PER_ACTION],
            "status": "pending",          # pending/approved/rejected/done/failed
            "note": "",
            "error": "",
            "backup": "",
        })

    # 用户要求（2026-11-15）：「只要是对用户有用，且理论上可行……主动迭代增加
    # 这项新技术，然后记得需要跟用户说明为什么」。原来 features 只展示、永不执行，
    # 这里把它转成可执行动作，和 bug 修复走同一条链路（同四道护栏）。
    feats = [f for f in (data.get("features") or []) if isinstance(f, dict)]
    # ★ 两阶段闭环第二步（2026-11-15 用户要求「主动根据潜在需求去寻找可能有用的
    #   论文并且参考构思」）：模型先想功能，再拿功能去论文库真检索，然后用**真实
    #   论文**回头校准 theory / how / assert_code。
    #   开关：设置里 feature_paper_enrich=false 可关掉（会省一次 LLM 调用）。
    enrich_meta = {"ok": False, "skipped": True}
    try:
        _on = load_settings().get("feature_paper_enrich")
        if _on is None or _on:
            set_stage("enrich", "拿 %d 条候选功能去论文库检索…" % len(feats))
            feats, enrich_meta = _enrich_features_with_papers(feats)
            if enrich_meta.get("papers"):
                set_stage("enrich", "召回 %d 篇，已校准 %d 处"
                          % (enrich_meta["papers"], enrich_meta.get("changed") or 0))
    except Exception as e:                              # noqa: BLE001
        enrich_meta = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        logger.warning("功能论文校准失败（不影响主流程）：%s", enrich_meta["error"])
    paper_meta["enrich"] = enrich_meta
    feat_actions = []
    try:
        feat_actions = _features_to_actions(
            feats, start=len(actions), limit=4,
            existing=[a.get("title") for a in actions])
    except Exception as e:                              # noqa: BLE001
        logger.warning("features 转动作失败：%s: %s", type(e).__name__, e)
    if feat_actions:
        actions.extend(feat_actions)
        set_stage("plan", "另有 %d 条新功能要落地" % len(feat_actions))

    plan = {
        "id": _pid(),
        "ts": _now(),
        "trigger": trigger,
        "summary": str(data.get("summary") or "")[:200],
        "weaknesses": data.get("weaknesses") or [],
        "user_insights": data.get("user_insights") or [],
        "features": feats,
        "forecast": [x for x in (data.get("forecast") or []) if isinstance(x, dict)],
        "needs": [x for x in (data.get("needs") or []) if isinstance(x, dict)],
        "questions": [x for x in (data.get("questions") or []) if isinstance(x, dict)],
        "feature_actions": len(feat_actions),
        "tech_takeaways": data.get("tech_takeaways") or [],
        "actions": actions,
        "tech_raw": {k: (v or [])[:6] for k, v in (tech or {}).items()},
        "tech_total": tech_total,
        "signals_used": len(sig),
        "paper": paper_meta,
        "paper_brief": paper_brief[:2000],
    }

    plan["notice"] = _plan_notice(plan)

    d = load_plans()
    d["items"].insert(0, plan)
    d["items"] = d["items"][:60]          # 最多留 60 次
    save_plans(d)

    # ★ 2026-11-15 接线 record_round：把本轮结构化摘要记一条，
    #   供「自主创新流水线可观测卡片」回看「每轮提了什么、论文抓到几篇」。
    #   只记摘要（标题/数量），不记全文 —— 这文件每轮追加一行。
    try:
        record_round(plan)
    except Exception:                                     # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    # ★ 2026-11-16 用户要求「不断完善计划，直到计划可以了再执行」，
    #   判定标准是他定的：自评 8 分以上，或打磨满 3 轮。
    #   只有真的有计划动作时才值得打磨（空计划打磨纯属烧钱）。
    if polish is None:
        polish = bool(load_settings().get("polish_plan", True))
    if polish and plan.get("actions"):
        try:
            plan2, pol = polish_plan(plan)
            plan2["polish"] = pol
            plan2["notice"] = _plan_notice(plan2)
            # 打磨会重写 actions，磁盘上那份也得同步，
            # 否则 iter_runner 执行的是打磨**之前**的旧动作。
            _pd, _pp = _find_plan(plan["id"])
            if _pp is not None:
                _pp["actions"] = plan2.get("actions") or []
                _pp["polish"] = pol
                _pp["notice"] = plan2["notice"]
                save_plans(_pd)
            plan = plan2
            actions = plan.get("actions") or []
            set_stage("polish", "打磨完成：自评 %s 分（%s 轮）"
                      % (pol.get("score"), pol.get("rounds")))
        except Exception as e:                          # noqa: BLE001
            # 打磨挂了就用原始计划继续 —— 它是增强，不是必经环节
            plan["polish"] = {"ok": False,
                              "error": "%s: %s" % (type(e).__name__, e)}
            logger.warning("计划打磨失败（用原始计划继续）：%s",
                           plan["polish"]["error"])

    out = {"ok": True, "plan": plan, "notice": plan["notice"]}

    # ★ 2026-11-16 用户要求：「活跃度太低就要看要不要执行，构思不出来实在
    #   不执行就以报告形式汇报」。也就是**宁可只汇报，也不硬改代码**。
    #   判定：打磨后自评 < 5 分，或可执行动作不足 2 条 → 这轮没想出值得动手
    #   的东西。硬执行只会把代码改坏（历史成功率 66% 正是这么来的）。
    #   这时保留计划、标 report_only，**不提交执行队列**，改成一份可读报告。
    #   注意：polish 失败（score 为 None）不触发 —— 那说明评不了，不是想不出。
    _pol = plan.get("polish") or {}
    try:
        _score = float(_pol.get("score")) if _pol.get("score") is not None else None
    except (TypeError, ValueError):
        _score = None
    _n_act = len(actions or [])
    # ⚠️ 2026-11-16 实测踩到的坑：这里原来还带了「动作数 < 2 就只出报告」，
    #   结果一份**自评 8.5 分、只有 1 条动作**的成熟计划被判成「构思不出来」
    #   直接不执行。1 条动作完全可以是一次完整改动，不能按条数判死。
    #   真正的判据只有分数：低于 5 分才算「没想出值得动手的东西」。
    # ★ 2026-11-16 用户要求「自主自由创新性地生成代码，只要不搞坏就行」，
    #   所以门槛从 5 分下调到 4 分：
    #   · 1~4 分 = 模型自己判定的「空泛/重复/根本不可行」→ 不执行，出报告；
    #   · 4~5 分 = 「方向对但粗糙」→ **照常执行**（有备份+语法校验+回归+回滚兜底，
    #     改坏了自己退回去）。原来卡在 5 分，会把一份大胆但粗糙的创新计划判死，
    #     系统就永远只会做保守修补 —— 那正是用户抱怨「没有创新」的机制性原因。
    if _score is not None and actions and _score < 4.0:
        _wk = []
        for w in (plan.get("weaknesses") or []):
            if isinstance(w, dict) and (w.get("title") or "").strip():
                _wk.append(str(w.get("title"))[:80])
        plan["report"] = {
            "why": "自评 %.1f 分（低于 4 分，即模型自判「空泛/不可行」），这轮没想出值得动手的东西"
                   % (_score,),
            "summary": str(plan.get("summary") or "")[:200],
            "weaknesses": _wk[:5],
            "needs": [str(n.get("pain"))[:80] for n in (plan.get("needs") or [])
                      if isinstance(n, dict) and n.get("pain")][:5],
            "next": "等出现更明确的信号再动手；也可以点「立即反思」并给出具体关注点。",
        }
        out["report_only"] = True
        out["report"] = plan["report"]
        # ★ 用户要求「构思不出来就以报告形式汇报」—— 所以报告要**看得见**，
        #   不能只写一行原因。这里拼成完整可读文本，前端 pushChat 直接显示。
        _lines = ["", "", "📋 本轮只出报告，没有改代码", plan["report"]["why"]]
        if _wk:
            _lines.append("")
            _lines.append("发现的问题：")
            _lines.extend("· " + x for x in _wk[:5])
        if plan["report"]["needs"]:
            _lines.append("")
            _lines.append("你当下卡在：")
            _lines.extend("· " + x for x in plan["report"]["needs"][:5])
        _lines.append("")
        _lines.append("下一步：" + plan["report"]["next"])
        out["notice"] = (out.get("notice") or "") + "\n".join(_lines)
        try:
            _pd, _pp = _find_plan(plan["id"])
            if _pp is not None:
                _pp["report"] = plan["report"]
                _pp["report_only"] = True
                save_plans(_pd)
        except Exception:                                 # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    # 信任模式：用户要求「不用批准，自动迭代，改完提示重启」。
    # 只在真的有计划动作时才执行，避免空跑。
    if auto_execute is None:
        auto_execute = bool(load_settings().get("trust_mode"))
    if auto_execute and is_paused():
        # 暂停期间：计划照常生成（留着恢复后批准），但绝不自动改代码。
        auto_execute = False
        out["paused"] = True
        out["notice"] = (out.get("notice") or "") + (
            "\n\n⏸ 自动化已暂停：本轮计划已生成，但没有自动执行。")
    if auto_execute and actions and not out.get("report_only"):
        # 2026-11-15 用户要求：计划一确定就直接开始，但**不阻塞聊天**，
        # 并且要在聊天界面显示「正在迭代中」+ 代码卡片；已有任务在跑就排队。
        # 所以这里只「批准 + 提交队列」，真正的执行在 iter_runner 的后台线程里。
        set_stage("exec", "把 %d 条动作提交到迭代队列…" % len(actions))
        pre = auto_approve_only(plan["id"])
        _d2, _p2 = _find_plan(plan["id"])
        if _p2:
            out["plan"] = _p2
        try:
            from . import iter_runner as _ir
            sub = _ir.submit(plan["id"], source="auto",
                             title=(plan.get("title") or "")[:80])
            out["auto"] = {"submitted": True, "queue": sub,
                           "auto_approved": pre.get("approved") or 0,
                           "high_risk": pre.get("high_risk") or []}
            out["task"] = sub
            out["need_restart"] = False
        except Exception as e:                          # noqa: BLE001
            out["auto"] = {"submitted": False,
                           "error": "%s: %s" % (type(e).__name__, e)}
    return out


# ---------------------------------------------------------------- 审批

def _find_plan(pid):
    d = load_plans()
    for p in d["items"]:
        if p.get("id") == pid:
            return d, p
    return d, None


def approve(pid, action_ids=None, note=""):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in p.get("actions") or []:
        if action_ids and a["id"] not in action_ids:
            continue
        if a["status"] in ("pending", "rejected", "need_review"):
            if a["status"] == "need_review":
                # 批量批准时也要能放行熔断动作，否则它们永远出不去
                a["error"] = ""
                a["reviewed"] = _now()
                p["_force"] = list(dict.fromkeys(
                    list(p.get("_force") or []) + [str(a.get("id"))]))
            a["status"] = "approved"
            a["note"] = str(note or "")[:800]
            hit += 1
    save_plans(d)
    return {"ok": True, "approved": hit, "plan": p}


def reject(pid, action_ids=None, note=""):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in p.get("actions") or []:
        if action_ids and a["id"] not in action_ids:
            continue
        if a["status"] in ("pending", "approved"):
            a["status"] = "rejected"
            a["note"] = str(note or "")[:800]
            hit += 1
    save_plans(d)
    return {"ok": True, "rejected": hit, "plan": p}


def force_execute(pid, action_ids=None):
    """人工复核通过：把「待人工复核」的动作放行，并重置这些文件的熔断计数。

    用户要求（2026-11-16）：「这么多个失败，彻底解决这个问题，如果要人工复核
    就给我在前端一个审核的按钮或方框」。熔断是护栏、不是故障，所以出路必须是
    「人看一眼、点一下、继续做」，而不是让那个文件被永久锁死。
    """
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    want = set(str(x) for x in (action_ids or []) if x)
    ids, files = [], []
    for a in (p.get("actions") or []):
        if a.get("status") != "need_review":
            continue
        if want and str(a.get("id")) not in want:
            continue
        a["status"] = "approved"
        a["error"] = ""
        a["reviewed"] = _now()
        ids.append(str(a.get("id")))
        files.extend([str(f) for f in (a.get("files") or []) if f])
    if not ids:
        return {"ok": False, "error": "没有待人工复核的动作"}
    p["_force"] = list(dict.fromkeys(list(p.get("_force") or []) + ids))
    save_plans(d)
    reset_loop(files)
    # ★ 2026-11-16 收尾核对：确认 force_execute 真的把动作放行到执行器。
    #   上一轮把熔断类 failed 迁移成 need_review 后成功率仍 55%，怀疑「前端批量
    #   放行」只改了状态、没真正进执行器。这里加一条断言日志：放行后动作状态
    #   必须是 approved 且 id 已进 _force，否则说明放行链路断了。
    _forced_set = set(ids)
    _bad = [str(a.get("id")) for a in (p.get("actions") or [])
            if str(a.get("id")) in _forced_set
            and a.get("status") != "approved"]
    _force_ok = (not _bad) and all(x in (p.get("_force") or []) for x in ids)
    logger.info("force_execute 放行核对 plan=%s forced=%d files=%d ok=%s%s",
                pid, len(ids), len(set(files)), _force_ok,
                (" bad=%s" % _bad) if _bad else "")
    if not _force_ok:
        return {"ok": False, "forced": ids,
                "files": list(dict.fromkeys(files)), "plan": p,
                "error": "放行后状态核对失败：%s" % (_bad or "_force 未写入")}
    return {"ok": True, "forced": ids,
            "files": list(dict.fromkeys(files)), "plan": p}


# ---------------------------------------------------------------- 执行（带备份/校验/回滚）

def _norm_rel(rel):
    """规范化并校验路径；不合法返回空串。"""
    if not isinstance(rel, str):
        return ""
    s = rel.replace("\\", "/").strip().lstrip("./")
    low = s.lower()
    # ⚠️ 这段原来是 `pass`（等于没生效），只靠下面的前缀白名单兜着 ——
    #    一旦将来放宽 _ALLOWED_PREFIX，禁写目录就彻底失守。改成真的拒绝。
    for bad in _FORBIDDEN_PARTS:
        if low.startswith(bad) or ("/" + bad) in ("/" + low):
            return ""
    if not any(low.startswith(p) for p in _ALLOWED_PREFIX):
        return ""
    if not low.endswith(_ALLOWED_EXT):
        return ""
    p = (_ROOT / s)
    try:
        p.resolve().relative_to(_ROOT.resolve())
    except Exception:                                   # noqa: BLE001
        return ""
    return s


_SYM_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)", re.M)


def _change_summary(patches, fail_kind=""):
    """把一次动作的补丁整理成「到底实现了什么 / 改了什么」的可读说明。

    2026-11-15 用户要求：「每次自动迭代能不能详细说明实现了什么功能或改了什么功能」。
    原来执行日志只有一句「改了：xxx.py」，看不出动了什么。这里从补丁本身提取：
    是新建还是修改、增删行数、新增/改动了哪些函数或类。

    只做静态归纳，不猜语义 —— 猜错了比不说更糟。函数名来自补丁里的 def/class，
    所以「新增 format_reply」这种是可信的；具体行为由动作的 detail/note 说明。

    2026-11-16 追加：带上真实 fail_kind 计数。
    用户要求「针对 fail_kind=X 共 N 次」写进 changelog；若计数为 0 则标记
    phantom 并记录原因，不再静默通过（对应 arXiv:2611.17336v1 Phantom Guardrails
    的幽灵修复问题）。计数来自 actlog.fail_kind_counts，异常时按「未知」处理，
    不阻断摘要生成。
    """
    by, order = {}, []
    for pt in (patches or []):
        if not isinstance(pt, dict):
            continue
        rel = _norm_rel(pt.get("file"))
        if not rel:
            # 路径校验不通过的补丁本来也不会被应用；这里不能退回原样显示，
            # 否则摘要里会出现「新建 ../../etc/passwd」这种假改动。
            continue
        if rel not in by:
            by[rel] = {"rel": rel, "add": 0, "del": 0, "new_file": False,
                       "added": [], "touched": []}
            order.append(rel)
        e = by[rel]
        find = str(pt.get("find") or "")
        rep = str(pt.get("replace") or "")
        if not find.strip():
            e["new_file"] = True
        e["add"] += len([ln for ln in rep.splitlines() if ln.strip()])
        e["del"] += len([ln for ln in find.splitlines() if ln.strip()])
        fs = _SYM_RE.findall(find)
        for s in _SYM_RE.findall(rep):
            if s not in fs and s not in e["added"]:
                e["added"].append(s)
        for s in fs:
            if s not in e["touched"]:
                e["touched"].append(s)

    files = [by[r] for r in order]
    parts = []
    for f in files:
        head = "新建" if f["new_file"] else "改"
        s = "%s %s（+%d −%d" % (head, f["rel"], f["add"], f["del"])
        if f["added"]:
            s += "，新增 " + "、".join(f["added"][:5])
        elif f["touched"]:
            s += "，动到 " + "、".join(f["touched"][:5])
        s += "）"
        parts.append(s)

    # ---- fail_kind 真实计数（2026-11-16 用户要求）----
    # 把「针对 fail_kind=X 共 N 次」写进 changelog；N=0 时标记 phantom 并
    # 记录原因，不再静默通过。计数异常时按「未知」处理，不阻断摘要生成。
    fk = str(fail_kind or "").strip()
    fk_info = {"fail_kind": fk, "count": None, "phantom": False,
               "reason": ""}
    if fk:
        try:
            from . import actlog as _actlog_mod
            # ★ 2026-11-16 修 [missing-attr]：actlog.py 里并没有
            #   `fail_kind_counts`，直接调用会抛 AttributeError；外面套了
            #   except 就会静默失效（计数永远「未知」，phantom 检测形同虚设）。
            #   这里做兼容：优先用 actlog 提供的计数函数（若将来补上），
            #   否则回落到本模块自己的 `_count_fail_kind`（读 actlog.jsonl）。
            _counter = getattr(_actlog_mod, "fail_kind_counts", None)
            if callable(_counter):
                counts = _counter(days=7) or {}
            else:
                counts = _count_fail_kind(fk, days=7)
            n = int((counts or {}).get(fk) or 0)
            fk_info["count"] = n
            if n > 0:
                parts.append("针对 fail_kind=%s 共 %d 次" % (fk, n))
            else:
                fk_info["phantom"] = True
                fk_info["reason"] = (
                    "fail_kind=%s 近 7 天真实计数为 0（幽灵修复），"
                    "本次改动无真实失败依据" % fk)
                parts.append("针对 fail_kind=%s 共 0 次（phantom：%s）"
                             % (fk, fk_info["reason"]))
        except Exception as e:                          # noqa: BLE001
            fk_info["reason"] = "fail_kind 计数失败：%s: %s" % (
                type(e).__name__, e)
            parts.append("针对 fail_kind=%s 计数未知（%s）"
                         % (fk, fk_info["reason"]))

    return {"summary": "；".join(parts), "files": files,
            "fail_kind": fk_info}


def _read_text(rel):
    try:
        with open(_ROOT / rel, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:                                   # noqa: BLE001
        return ""


def _backup_files(files, tag):
    """备份要改的文件。

    返回 (tag, saved, created)：
      saved   = 已存在、已备份的文件
      created = 原本不存在（属于新建）的文件 —— 回滚时要把它们删掉
    """
    _ensure()
    dst = _BACKUPS / tag
    dst.mkdir(parents=True, exist_ok=True)
    saved, created = [], []
    for rel in files:
        src = _ROOT / rel
        if not src.is_file():
            created.append(rel)
            continue
        out = dst / rel.replace("/", "__")
        try:
            shutil.copy2(src, out)
            saved.append(rel)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    _write_json(dst / "_manifest.json",
                {"files": saved, "created": created, "ts": _now(), "tag": tag})
    return tag, saved, created


def _outline_block(body, limit=800, max_chars=11000):
    """给超长文件生成「结构大纲」：所有 def/class 的名字 + 行号。

    2026-11-15 加：main.py 有 56 万字符 / 14820 行，26000 的额度只覆盖 4.6%，
    模型连文件里有哪些函数都不知道，自然写不出精确的 find
    （实测 2 条动作失败在「文件内容被截断，看不到 xxx 的实现」）。
    有了大纲，它至少能判断要改的东西在不在，以及该请求哪一段。

    格式用紧凑的一行式（名字(行号)），因为实测 main.py 有 554 个定义，
    一条一行的签名式会吃掉 2.7 万字符、反而挤掉正文。
    """
    parts = []
    for i, ln in enumerate(body.split("\n"), 1):
        s = ln.lstrip()
        if s.startswith("async def "):
            nm = s[10:].split("(")[0].strip()
        elif s.startswith("def "):
            nm = s[4:].split("(")[0].strip()
        elif s.startswith("class "):
            nm = s[6:].split("(")[0].split(":")[0].strip()
        else:
            continue
        if not nm:
            continue
        parts.append("%s(%d)" % (nm, i))
        if len(parts) >= limit:
            parts.append("…还有更多")
            break
    if not parts:
        return ""
    txt = " ".join(parts)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + " …（大纲过长已截断）"
    return ("\n【文件结构大纲：函数名(所在行号)，共 %d 个。"
            "要改的代码若不在下面正文里，用 {\"need\":\"函数名\"} 请求补段】\n"
            "%s\n【大纲结束，下面是正文开头】\n" % (len(parts), txt))


def _find_need_segments(files, key, budget=9000):
    """模型说「我要改的代码没给我」时，按它给的关键词从原文里抠出上下文。

    这是「按需补段」：不再盲目截断，而是让模型自己说缺什么。
    关键词可以是函数名、`def xxx`、或一行特征代码。
    """
    key = (key or "").strip()
    if not key:
        return ""
    # ⚠️ 实测 files 传 None 会直接 TypeError：for rel in None 崩掉。
    #    调用方虽然现在都传列表，但补段发生在失败处理路径上，
    #    这里崩掉会把「补丁没生成」变成「整轮报错」，代价不对等。
    if isinstance(files, str):
        files = [files]
    files = [f for f in (files or []) if isinstance(f, str) and f.strip()]
    if not files:
        return ""
    # ★ 2026-11-15 实测修 bug：模型经常把 need 写成**自然语言**而不是函数名，
    #   例如「backend/main.py 中 selfiter_actlog 函数定义（约 2492 行）」。
    #   原来直接 re.escape(整句) 去搜，必然搜不到 → 整个动作判失败。
    #   实测 selfiter_actlog 确实就在 main.py 第 2492 行，纯属匹配方式太死。
    #   这里：① 从请求里提取标识符候选；② 支持「约 N 行」的行号兜底。
    _STOP = {"backend", "frontend", "data", "tools", "main", "py", "js",
             "css", "html", "json", "def", "class", "import", "self",
             "function", "file"}
    cands = [key]
    for _w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", key):
        if _w.lower() in _STOP or _w in cands:
            continue
        cands.append(_w)
    # 长名优先：短名（如 run）容易先命中无关位置，把真正的目标挤掉
    cands = [cands[0]] + sorted(cands[1:], key=len, reverse=True)
    _mline = re.search(r"(\d{2,6})\s*行", key)
    _hint = int(_mline.group(1)) if _mline else 0

    out = []
    for rel in files:
        body = _read_text(rel)
        if not body:
            continue
        # 定位优先级：函数/类定义 > 函数调用 > 普通出现。
        # ⚠️ 原来只有 body.find(key)，实测 `diag_nowin` 命中的是文件里
        #    别处的字符串/注释，补出来的 1500 字前缀全是无关内容，
        #    模型拿到也看不出要改哪个函数。
        pos, used = -1, ""
        for _c in cands:
            k = re.escape(_c)
            for pat in (r"(?<![.\w])(?:async\s+)?def\s+%s\s*\(" % k,
                        r"(?<![.\w])class\s+%s\s*[\(:]" % k,
                        r"(?<![.\w])%s\s*\(" % k,
                        k):
                m = re.search(pat, body)
                if m:
                    pos, used = m.start(), _c
                    break
            if pos >= 0:
                break
        if pos < 0 and _hint > 0:
            # 行号兜底：模型说了「约 N 行」却没给准函数名
            _lines = body.split("\n")
            if 0 < _hint <= len(_lines):
                pos = len("\n".join(_lines[:_hint - 1]))
                used = "第 %d 行" % _hint
        if pos < 0:
            continue
        seg = body[max(0, pos - 1500):pos + 3500]
        out.append("### 文件：%s（按你请求的 `%s` 定位到的原文）\n```\n%s\n```"
                   % (rel, used[:60], seg))
        if sum(len(x) for x in out) > budget:
            break
    return "\n\n".join(out)


def _verify(rel):
    """改完做语法校验：.py 用 py_compile，.js 用 node --check（没有 node 就跳过）。"""
    p = _ROOT / rel
    if not p.is_file():
        return False, "文件不存在"
    if rel.endswith(".py"):
        try:
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                return False, (r.stderr or b"").decode("utf-8", "replace")[-600:]
            return True, ""
        except Exception as e:                          # noqa: BLE001
            return False, "py_compile 失败：%s" % e
    if rel.endswith(".js"):
        try:
            r = subprocess.run(["node", "--check", str(p)],
                               capture_output=True, timeout=40)
            if r.returncode != 0:
                return False, (r.stderr or b"").decode("utf-8", "replace")[-600:]
            return True, ""
        except FileNotFoundError:
            return True, ""                             # 没装 node 就跳过
        except Exception:                               # noqa: BLE001
            return True, ""
    return True, ""


def _looks_mojibake(s):
    """检测「UTF-8 字节被 GBK 解码」这类乱码。

    2026-11-15 加：实测有 1 条动作把中文注释写成了 `Ϊʲô...`，
    py_compile 报 `invalid character` 后整轮回滚，白跑 3 轮自修
    （模型每次还是输出同样的乱码，自修救不回来）。
    提前拦下并说明原因，比让它写完再语法失败更有用。

    判据刻意保守，但覆盖两类经典乱码：
      · 希腊/西里尔字母、拉丁重音字母成片出现——正常中文代码里 0x80-0x2FF
        区间只会有零星重音字母（café、résumé），连续 ≥3 个必然是解码错；
      · GBK 双重解码的三个固定汉字组合（「锟」「斤」「拷」连写、「烫」字三连）
        ——它们是**合法汉字**，按字符区间永远数不到，只能靠精确短语认。
        （这里刻意拆开写，否则本函数的说明文字会把自己判成乱码。）
    实测 18 个样本：6 个乱码全中，12 个正常内容（希腊字母变量、法语/西语变量名、
    emoji、温度单位、长中文注释）零误伤。
    """
    if not s or not isinstance(s, str):
        return False
    if "\ufffd" in s:
        return True
    # GBK 双重解码的固定产物，正常代码里不会出现这三个字连写。
    # ⚠️ 用 \u 转义而不是字面量：字面写出来这一行自己就命中词表，
    #    本文件会被自己判成乱码（实测 129 个文件里唯一误报就是本文件）。
    if any(t in s for t in ("\u951f\u65a4\u62f7",
                            "\u70eb\u70eb\u70eb",
                            "\u5c6f\u5c6f\u5c6f")):
        return True
    # ⚠️ 区间必须从 0x0080 起：原来只到 0x00C0，导致 `ä½ å¥½` 里的
    #    ½(0x00BD)、¥(0x00A5) 数不进去，实测只数到 2 个 → 漏检。
    odd = sum(1 for ch in s
              if 0x0080 <= ord(ch) <= 0x052F        # 拉丁补充/扩展 + 希腊 + 西里尔
              or 0x1E00 <= ord(ch) <= 0x1EFF)       # 扩展拉丁附加
    han = sum(1 for ch in s if 0x4E00 <= ord(ch) <= 0x9FFF)
    return odd >= 3 and han <= odd // 2


def _apply_find_replace(rel, find, replace):
    """精确替换（必须恰好命中 1 处），与 selfdev_patch 同一套语义。

    特例：find 为空且目标文件不存在 → 视为**新建文件**，直接写入 replace。
    （"建一个回归脚本"这类动作必须支持，否则永远执行不了。）
    """
    if _looks_mojibake(replace):
        return False, ("补丁内容里出现编码乱码（中文变成了 Ϊʲô 这类字符），"
                       "写进去会导致语法错误。请重新输出，确保中文正常。")
    path = _ROOT / rel
    if not find and not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(replace or "")
            return True, ""
        except Exception as e:                          # noqa: BLE001
            return False, "新建文件失败：%s" % e
    txt = _read_text(rel)
    if not txt:
        return False, "读不到文件"
    if not find:
        return False, "find 为空"
    n = txt.count(find)
    if n == 0:
        return False, "find 在文件里找不到（可能已被改过）"
    if n > 1:
        return False, "find 命中 %d 处，不唯一" % n
    new = txt.replace(find, replace, 1)
    try:
        with open(_ROOT / rel, "w", encoding="utf-8") as f:
            f.write(new)
        return True, ""
    except Exception as e:                              # noqa: BLE001
        return False, "写入失败：%s" % e


def classify_fail(msg):
    """把失败原因归类，让用户一眼分清「护栏正常工作」和「真出故障」。

    2026-11-15 用户问：「为什么信任模式下迭代会显示失败，这个能不能解决？」
    查了 data/self_iterate/plans.json 里全部 11 条历史失败，性质完全不同：
      · 5 条是护栏**按设计**拦下的（反复修改熔断 4 条、拒绝占位式修补 1 条）
        —— 这些失败是**对的**，说明防护生效了，没让项目被改乱；
      · 6 条是真问题（模型没给合法 JSON 2 条、文件内容被截断 2 条、
        回归子进程超时 1 条、模型写出乱码被语法校验拦下 1 条）。
    以前两者在界面上长得一模一样（都是红底「失败」），用户自然以为系统坏了。
    现在每条失败带一个 kind，前端据此显示不同颜色和说明。

    返回值：
      guard    护栏主动拦下 —— 不是故障，可以放心
      model    模型输出质量问题 —— 已加重试，仍失败需人工看
      env      环境/依赖/超时 —— 通常是临时性的，可重跑
      unsolved 功能确实没做成 —— 已完整回滚，无残留
      other    未归类
    """
    s = str(msg or "")
    if not s:
        return "unknown"
    # ⚠️ 顺序很重要：「截断」必须排在「无法安全实施」之前。
    # 实测那条「临时验证脚本自动清理」的 error 是
    #   「模型判断无法安全实施：…文件内容在 read() 处被截断（显示"后略"）…」
    # 表面是「无法安全实施」（看着像护栏），但**根因是我们只给了前 14000 字符**，
    # 属于可修的真问题。按 guard 归类会把真 bug 藏起来。
    if "截断" in s:
        return "model"
    # ⚠️ 「先停手」「远超阈值」都要认：_loop_guard 有两条文案
    #   · 冷却闸门：反复修改熔断：xxx 在最近 60 分钟内已被自动改成功 N 次…
    #   · 硬熔断：xxx 已自动改 16 次、远超阈值（阈值 3），先停手换思路
    # 只匹配前者会把后者归到 other，看着像故障。
    for k in ("反复修改熔断", "占位式修补", "无法安全实施", "涉及文件过多",
              "未接入主链路", "先停手", "远超阈值", "改动让自检问题"):
        if k in s:
            return "guard"
    for k in ("没给出合法 JSON", "没有给出任何 patch", "模型没返回合法",
              "不是合法 JSON", "无法生成"):
        if k in s:
            return "model"
    for k in ("ModuleNotFoundError", "No module named", "ImportError",
              "断言文件不存在", "读不到文件", "超时", "timeout",
              "FileNotFoundError", "PermissionError", "SyntaxError"):
        if k in s:
            return "env"
    if "自修" in s and "仍未通过" in s:
        return "unsolved"
    return "other"


def _plan_patch_for_action(plan, action):
    """让 LLM 针对这一条动作产出具体 patch（此时才读相关文件内容）。"""
    want = [f for f in (action.get("files") or []) if isinstance(f, str)]
    if not want:
        # 兜底（2026-11-15）：模型产出的动作常常只在标题/说明里写路径，
        # 不填 files 字段 —— 那样执行必然报「没有可读的目标文件」，白排一次队。
        # 这里从文本里把路径抠出来。**只认已存在的文件**：凭空造新文件风险太大，
        # 新建类动作必须显式写 files，否则宁可报错让人看见。
        _blob = "%s %s %s" % (action.get("title") or "",
                              action.get("detail") or "",
                              action.get("note") or "")
        _cand = re.findall(
            r"((?:backend|frontend|data|tools)/[\w./\-]+\.(?:py|js|css|html|json|md))",
            _blob)
        want = [c for c in dict.fromkeys(_cand) if (_ROOT / c).is_file()][:3]
    files = [f for f in want if (_ROOT / f).is_file()]
    new_files = [f for f in want if not (_ROOT / f).is_file()]
    if not files and not new_files:
        return None, "没有可读的目标文件（动作里没写 files，文本里也没找到已存在的路径）"
    # 2026-11-15 修：原来单文件硬截 14000 字符。selfdev.py 有 47904 字节，
    # 模型只看到前 30%，而要改的函数在后面 → 生成不出「一字不差」的 find，
    # 实测 2 条动作因此失败（error 都是「文件内容被截断，无法生成 find」）。
    # 现在：单文件额度提到 26000、总预算 46000；截断时如实报「已给到第几行/共几行」，
    # 并且**按动作文本里点名的函数名去定位**，把截断点之后那一段也附上 ——
    # 否则模型永远看不到它真正要改的地方。
    _CTX_TOTAL = 46000
    _CTX_ONE = 26000
    # ★ 2026-11-16 修真 bug（用户要求「自主自由创新性地生成代码」时查出来的）：
    #   功能类动作的执行上限是 _MAX_FILES_FEATURE=6，但**读内容**这里一律只读
    #   前 3 个（_MAX_FILES_PER_ACTION）—— 后 3 个文件模型根本看不到，
    #   它生成的 patch 必然缺文件，或者在 find 上瞎猜被拒。
    #   实测失败原文：「文件内容在 read() 处被截断…无法安全实施」。
    #   创新要落地的第一步是「让它看得见要改的东西」，所以按动作类型取上限。
    _maxf_ctx = (_MAX_FILES_FEATURE if action.get("origin") == "feature"
                 else _MAX_FILES_PER_ACTION)
    _nfiles = max(1, min(len(files), _maxf_ctx))
    _per = max(6000, min(_CTX_ONE, _CTX_TOTAL // _nfiles))
    _ids_all = re.findall(
        r"[A-Za-z_][A-Za-z0-9_]{3,}",
        "%s %s" % (action.get("title") or "", action.get("detail") or ""))
    ctx = []
    for rel in files[:_maxf_ctx]:
        body = _read_text(rel)
        if len(body) > _per:
            _tot_lines = body.count("\n") + 1
            _cut = body[:_per]
            _shown = _cut.count("\n") + 1
            _tail = ""
            _rest = body[_per:]
            # 标识符按长度降序：`_read_text` 比 `read` 有用得多。
            # 实测按原顺序直接 find("read")，命中的是 subprocess 参数
            # （`.executable, entry)`），补出来的根本不是要改的函数 ——
            # 白占上下文还误导模型。改成「函数定义处 > 调用处 > 裸词」。
            _STOP = {"self", "file", "data", "with", "from", "return", "import",
                     "print", "open", "path", "text", "true", "false", "none",
                     "str", "int", "list", "dict", "json", "os", "sys"}
            for _id in sorted(dict.fromkeys(_ids_all), key=len, reverse=True):
                if len(_id) < 4 or _id.lower() in _STOP:
                    continue
                _pos = _rest.find("def " + _id)
                if _pos < 0:
                    # ⚠️ 不能用 find(id + "(")：实测 `read(` 会命中 `f.read()`，
                    # 那是文件对象的属性调用，不是要改的函数。
                    # 用 (?<![.\w]) 排除「点号或字母数字之后」的匹配。
                    _mm = re.search(r"(?<![.\w])" + re.escape(_id) + r"\s*\(", _rest)
                    _pos = _mm.start() if _mm else -1
                if _pos < 0:
                    _mm = re.search(r"(?<![.\w])" + re.escape(_id) + r"(?![.\w])", _rest)
                    _pos = _mm.start() if _mm else -1
                if _pos < 0:
                    continue
                _seg = _rest[max(0, _pos - 1200):_pos + 2400]
                if _seg and _seg not in _tail:
                    _tail += ("\n…（下面这段是按你需求里提到的 `%s` 定位到的，"
                              "它位于上面截断点之后）\n%s" % (_id, _seg))
                if len(_tail) > 7000:
                    break
            _full_len = len(body)
            body = (_outline_block(body)
                    + _cut
                    + "\n…（后略：上面给了前 %d 行，全文共 %d 行 / %d 字符）"
                      % (_shown, _tot_lines, _full_len)
                    + _tail)
        ctx.append("### 文件：%s\n```\n%s\n```" % (rel, body))
    for rel in new_files[:_maxf_ctx]:
        ctx.append("### 待新建文件：%s\n（这个文件还不存在。要新建它，"
                   "patch 里把 find 写成空字符串 \"\"，replace 写完整文件内容。）" % rel)

    prompt = """你要在这个项目里实施一个已经获得用户批准的小改动。请输出**精确的查找替换**。

## 改动需求
标题：%s
说明：%s
用户备注：%s

## 涉及文件（当前内容）
%s

## 硬性要求
1. 只输出 JSON，不要任何解释文字。
2. find 必须与文件内容**一字不差**（含缩进和换行），并且在整个文件里**只出现一次**。
3. ★ **鼓励大胆、创新的改法**（用户 2026-11-16 原话授权：「LLM 去自主自由创新性地
   生成代码来提高用户体验，只要不搞坏就行」）：
   · 可以新增函数/类/模块、重构某一段、换成更好的实现或更优雅的交互，
     **不要为了"最小化"把改动做残** —— 半截的实现比不实现更糟；
   · 但**必须保证现有功能不被破坏**：不要删掉现有能力、不要改无关代码的行为、
     不要留下未接线的死代码；
   · 改动面越大，越要**一次改完整**（所有相关处一起给），因为任何一处失败
     都会让整条动作回滚，等于白改；
   · 拿不准要不要动某处时，宁可多给一处 patch，也别让它跑不通。
4. 如果发现无法安全实施（比如需求与代码不符），就输出 {"skip":"原因"}。
5. **如果上面没给到你要改的那段代码**（文件太长被截断了），**不要猜、不要编 find**。
   输出 {"need":"函数名或一行特征代码"} 来请求补段，我会把那段原文原样给你。

## 输出格式
{"patches":[{"file":"backend/xxx.py","find":"原文片段","replace":"替换后片段"}]}
（需要补段时：{"need":"函数名或特征代码"}）
""" % (action.get("title"), action.get("detail"),
       action.get("note") or "（无）", "\n\n".join(ctx))

    txt, err = _llm_text(prompt, max_tokens=3600, temperature=0.2)
    if err:
        return None, err
    data = _parse_json(txt)
    if not isinstance(data, dict):
        # 2026-11-15 修：原来一次解析失败就直接放弃（实测有 2 条动作因此判死：
        # 「回复长度硬上限」「信任模式与自迭代状态自报」，error 都是「模型没给出
        # 合法 JSON」）。这类失败往往只是它多写了几个字或包了代码围栏，
        # 重试一次、把格式要求再说一遍、温度压到 0，就能救回来。
        _retry = prompt + (
            "\n\n## 特别注意（你上一次的回答无法解析）\n"
            "上一次的输出不是合法 JSON。这次请**只输出一个 JSON 对象**：\n"
            "- 不要 ``` 代码围栏\n- 不要任何解释文字\n- 不要前后缀\n"
            "- 字符串里的换行写成 \\n\n")
        txt2, err2 = _llm_text(_retry, max_tokens=3600, temperature=0.0)
        if not err2:
            data = _parse_json(txt2)
        if not isinstance(data, dict):
            return None, ("模型没给出合法 JSON（已重试 1 次，两次都无法解析；"
                          "首次输出前 120 字：%s）" % (txt or "")[:120])
    # 2026-11-15 加：模型可以请求补段。它要改的代码被截断时，
    # 原来只能硬猜 find（必然匹配不上）或直接放弃，两种都算失败。
    _need_rounds = 0
    while isinstance(data, dict) and data.get("need") and _need_rounds < 2:
        _need_rounds += 1
        _key = str(data.get("need") or "")[:80]
        _extra = _find_need_segments(files, _key)
        if not _extra:
            return None, ("模型请求补段（%s），但没在文件里找到这段代码 —— "
                          "可能是它记错了函数名" % _key)
        _p2 = (prompt + "\n\n## 补充内容（你上一轮说需要的片段，这是原文）\n"
               + _extra + "\n\n现在请输出 JSON 补丁。\n")
        _t2, _e2 = _llm_text(_p2, max_tokens=3600, temperature=0.1)
        if _e2:
            return None, _e2
        data = _parse_json(_t2)
        if not isinstance(data, dict):
            return None, ("模型请求补段后仍未给出合法 JSON（前 120 字：%s）"
                          % (_t2 or "")[:120])
    if data.get("skip"):
        return None, "模型判断无法安全实施：%s" % str(data.get("skip"))[:200]
    patches = data.get("patches") or []
    if not isinstance(patches, list) or not patches:
        return None, "模型没有给出任何 patch"
    return patches, ""


# ------------------------------------------------------ 新功能：失败 → 自修 → 复验
# 2026-11-15 用户要求：「加入之后要自动验证是否完全正常，并反复改到完全正常为止」。
# 原来的流程是「断言不过 → 立刻回滚 → 放弃」，等于把「差一点就成」的功能直接判死。
# 这里补上修复循环：把断言的失败输出喂回模型 → 产出修复 patch → 应用 → 再跑断言，
# 直到通过或用尽轮次。轮次上限默认 3（settings.feature_repair_rounds 可调）。
_FT_DIR_MARK = "_feature_tests"


def _feature_assert_source(title):
    """读回新功能的验收断言源码 —— 它是这一轮修复的「规格说明」。"""
    try:
        from . import selfevolve
        p = selfevolve._test_path(title)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")[:6000]
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return ""


def _repair_feature(plan, action, rels, why, rounds=3, ev=None):
    """带着验收失败原因反复改，直到断言通过或用尽轮次。

    返回 {"ok":bool,"why":str,"rounds":int,"history":[...],"patches":[...]}

    护栏（每条都是踩过坑才加的）：
      · **禁止修改断言文件** —— 否则模型会把断言改成恒真，等于自己判自己及格；
      · 每轮独立备份，某轮 patch 应用失败就回到本轮起点，不留半成品；
      · 每轮先过语法校验，语法不过不进断言验证（省一次子进程）；
      · 全部轮次失败 → 由调用方恢复到动作前状态，绝不留残缺功能。
    """
    try:
        from . import selfevolve
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "why": "selfevolve 不可用：%s" % e,
                "rounds": 0, "history": [], "patches": []}

    title = action.get("title") or ""
    spec = _feature_assert_source(title)
    cap = int(globals().get("_MAX_FILES_FEATURE") or 6)
    uniq = list(dict.fromkeys([r for r in rels if r]))
    hist, cur_why, last_pts = [], why, []
    # 自修轮里可能新建文件；失败时必须由调用方精确删掉，否则留孤儿
    # （实测：自修失败后 _repair_orphan.py 仍留在项目里）。
    made = []
    n = max(1, min(int(rounds or 3), 6))

    for i in range(n):
        # 每轮都重读文件：模型必须看到上一轮改完的真实状态
        ctx = []
        for rel in uniq[:cap]:
            body = _read_text(rel)
            if len(body) > 12000:
                body = body[:12000] + "\n…（后略，只针对你确定存在的片段给 find）"
            ctx.append("### 文件：%s\n```\n%s\n```" % (rel, body))
        tried = "\n".join("- 第%d轮：%s" % (j + 1, h) for j, h in enumerate(hist[-3:]))
        prompt = """新功能已经写进项目了，但它的验收断言跑不过。请输出**精确的查找替换**把它修到通过。

## 功能
标题：%s
说明：%s

## 验收断言（这是规格，必须让它通过；**严禁修改这个文件**）
```
%s
```

## 断言当前的失败输出
%s

## 已经试过但没成功的改法
%s

## 涉及文件（当前真实内容）
%s

## 硬性要求
1. 只输出 JSON，不要任何解释文字。
2. find 必须与文件内容**一字不差**（含缩进换行），且在整个文件里**只出现一次**。
3. **绝对不能修改 %s/ 下的断言文件**，也不能让它被跳过、改宽或不再执行。
4. 允许换实现方式（可以重构、可以重写这一段），只要①现有功能不被破坏
   ②断言能通过。**不要为了"改动小"而把实现做残** —— 断言要的是真能用，
   不是刚好卡过。
5. 如果这个功能在当前项目里根本做不到，输出 {"skip":"原因"}，不要硬凑。

## 输出格式
{"patches":[{"file":"backend/xxx.py","find":"原文片段","replace":"替换后片段"}]}
""" % (title, (action.get("detail") or "")[:1500],
       spec or "（读不到断言文件）", cur_why[:700],
       tried or "（无）", "\n\n".join(ctx), _FT_DIR_MARK)

        if ev:
            ev("step", text="%s 第%d轮自修：按失败原因重改…" % (
                action.get("id"), i + 1))
        txt, err = _llm_text(prompt, max_tokens=3600, temperature=0.2)
        if err:
            hist.append("模型调用失败：%s" % err)
            break
        data = _parse_json(txt)
        if not isinstance(data, dict):
            hist.append("模型没给出合法 JSON")
            continue
        if data.get("skip"):
            hist.append("模型判断做不到：%s" % str(data.get("skip"))[:150])
            break
        pts = data.get("patches") or []
        if not isinstance(pts, list) or not pts:
            hist.append("模型没给出 patch")
            continue
        # 防作弊：不许碰断言文件（否则等于自己判自己及格）
        if any(_FT_DIR_MARK in str(p.get("file") or "") for p in pts):
            hist.append("模型试图修改断言文件，已拒绝")
            continue

        # 本轮备份（把 patch 里新出现的文件也算进来）
        allf = list(dict.fromkeys(
            uniq + [str(p.get("file")) for p in pts if p.get("file")]))
        tag_r, _sv, _cr = _backup_files(
            allf, "repair_%s_%d" % (action.get("id") or "x", i + 1))
        bad = ""
        for pt in pts:
            rel = _norm_rel(pt.get("file"))
            if not rel:
                bad = "文件路径不合法：%s" % pt.get("file")
                break
            _existed = (_ROOT / rel).is_file()
            ok, m = _apply_find_replace(rel, pt.get("find") or "",
                                        pt.get("replace") or "")
            if not ok:
                bad = "%s：%s" % (rel, m)
                break
            if not _existed and rel not in made:
                made.append(rel)
            if rel not in uniq:
                uniq.append(rel)
        if bad:
            _restore(tag_r)
            hist.append("patch 没应用上（%s）" % bad[:160])
            cur_why = bad
            continue

        # 语法先过一道，再花子进程跑断言
        syn = ""
        for rel in uniq:
            ok, m = _verify(rel)
            if not ok:
                syn = "%s 语法错误：%s" % (rel, m)
                break
        if syn:
            _restore(tag_r)
            hist.append(syn[:160])
            cur_why = syn
            continue

        _vr = selfevolve.verify_one(
            title, path=((action or {}).get("assert_file") or None))
        if _vr.get("ok"):
            hist.append("第%d轮通过（%ss）" % (i + 1, _vr.get("seconds")))
            return {"ok": True, "why": "", "rounds": i + 1,
                    "history": hist, "patches": pts, "made": made}
        cur_why = str(_vr.get("why") or "断言失败")
        last_pts = pts
        hist.append("第%d轮仍失败：%s" % (i + 1, cur_why[:140]))

    return {"ok": False, "why": cur_why, "rounds": len(hist),
            "history": hist, "patches": last_pts, "made": made}


# ---------------------------------------------------------------- 修复质量闸门
# 2026-11-15 实测暴露的问题一：自动修复里有一类是**占位式修补** ——
# 把「调用不存在的函数」改成 `return 0`、把「未定义变量」改成 `return 1`。
# 告警确实消失了（自检 0 条），但返回值是模型猜的，语义完全不对。
# 这类改动比不改更危险：它把「会崩」变成了「悄悄返回错数据」，更难发现。
# 所以改完必须先过这道闸：判为占位式 → 回滚，并如实标成「需人工确认」。
_LIT_RETURN_RE = re.compile(
    r"^\s*return\s+(0|1|-1|0\.0|None|True|False|''|\"\"|\[\]|\{\})\s*(?:#.*)?$", re.M)
_PLACEHOLDER_RE = re.compile(r"\b(TODO|FIXME|XXX|NotImplementedError|未实现)\b")
# 任意裸常量（数字 / None / True / False），用来抓「表达式被整体换成常量」
_LIT_ANY_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?|None|True|False)(?![\w.])")
_SWALLOW_RE = re.compile(r"except[^\n:]*:\s*\n\s*pass\b")
_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_KEYWORDS = set((
    "def", "class", "return", "if", "else", "elif", "for", "while", "in", "not",
    "and", "or", "is", "None", "True", "False", "try", "except", "finally",
    "with", "as", "import", "from", "pass", "break", "continue", "raise",
    "lambda", "yield", "global", "nonlocal", "assert", "del", "self", "cls"))


def _idents(s):
    return set(w for w in _IDENT_RE.findall(s or "") if w not in _KEYWORDS)


def _fix_quality(action, patches):
    """判断这次改动是「真修好了」还是「只让告警消失了」。返回 [] 表示通过。

    只查**能确定**的占位式修补，不做语义猜测 —— 误杀一个正确修法和放过一个
    占位修补同样有害，所以判据要窄：
      ① 常量返回值替换掉了原来真实存在的名字/调用（`return 0` 换掉 `f(x)`）
      ② 新加 `except: pass`（换个方式把异常藏起来）
      ③ 引入 TODO / NotImplementedError 占位
      ④ 把整个函数体改成 `pass`（功能被删掉）
    """
    reasons = []
    for pt in patches or []:
        find = str(pt.get("find") or "")
        repl = str(pt.get("replace") or "")
        if not find:
            continue                      # 新建文件，没有「改前」可比
        r_lit = _LIT_RETURN_RE.findall(repl)
        lost = _idents(find) - _idents(repl)
        if r_lit and lost:
            reasons.append(
                "把 `%s` 换成了常量返回值 `return %s` —— 只是让告警消失，"
                "返回值是模型猜的，语义不可靠"
                % ("、".join(sorted(lost)[:3]), r_lit[0]))
        elif lost and not _idents(repl) and _LIT_ANY_RE.search(repl):
            # 不是整行 `return 常量`，而是把表达式整体换成裸常量（`f(x)` → `0`）。
            # 原判据只认整行 return，这类会绕过闸门（实测被改成 `return 0`）。
            reasons.append(
                "把 `%s` 整体换成了裸常量 `%s` —— 只是让告警消失，"
                "返回值是模型猜的，语义不可靠"
                % ("、".join(sorted(lost)[:3]), repl.strip()[:24]))
        # ①b 给「未定义的名字」直接赋一个常量 —— 和 `return 0` 同一性质，
        #     只是换了个写法（实测模型真这么干：`undefined_variable_xyz = 0`）。
        #     判据要窄：只认「这个名字在改前**只出现一次**、且不是赋值语句」，
        #     改后却多出一句 `name = 常量`。这样才不会误杀「默认参数改 None」
        #     那种正确修法（那里的名字在改前出现多次、且是形参）。
        for _nm in sorted(_idents(find)):
            if len(re.findall(r"\b%s\b" % re.escape(_nm), find)) != 1:
                continue
            if re.search(r"^\s*%s\s*=" % re.escape(_nm), find, re.M):
                continue
            if re.search(r"^\s*%s\s*=\s*(0|1|-1|0\.0|None|True|False|''|\"\"|\[\]|\{\})"
                         r"\s*(?:#.*)?$" % re.escape(_nm), repl, re.M):
                reasons.append("给未定义的 `%s` 直接赋了常量值 —— 只是让告警消失，"
                               "这个值是模型猜的" % _nm)
                break
        # ①c 把「调用不存在的函数」整段删掉（`return f(x)` → `return x`）：
        #     告警消失了，但原来那次计算也没了 —— 是不是原意只能人来判断。
        if any(str(t.get("kind") or "") == "undefined-call"
               for t in (action.get("targets") or [])):
            # ⚠️ 必须排除关键字：`assert (x, "b")` 长得像函数调用，第一版把它
            # 当成「调用不存在的函数 assert」误杀了 assert 的正确修法（实测踩到）。
            _called = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", find)) - _KEYWORDS
            _gone = [c for c in _called
                     if c not in ("print", "len", "str", "int", "float", "list",
                                  "dict", "set", "tuple", "range", "isinstance",
                                  "super", "open", "sorted", "any", "all", "min", "max")
                     and c not in _idents(repl)]
            if _gone:
                reasons.append("把对不存在函数 `%s` 的调用整段删掉了 —— 那次计算也随之消失，"
                               "需人工确认这是不是原意" % _gone[0])
        if len(_SWALLOW_RE.findall(repl)) > len(_SWALLOW_RE.findall(find)):
            reasons.append("新加了一个 `except: pass`，把异常吞掉了（问题只是被藏起来）")
        if _PLACEHOLDER_RE.search(repl) and not _PLACEHOLDER_RE.search(find):
            reasons.append("改动里留下了 TODO/未实现占位")
        if (re.search(r"def\s+\w+\s*\([^)]*\):\s*\n\s*pass\s*$", repl)
                and not re.search(r"def\s+\w+\s*\([^)]*\):\s*\n\s*pass\s*$", find)):
            reasons.append("把整个函数体改成了 pass，功能被删掉了")
    return reasons


def _precheck_actions(actions):
    """动手前的最后一道复核：确认动作针对的问题**现在仍然存在**。

    用户要求「修之前一定要再次或多次检验」。复核层在建动作前已经跑了四轮，
    但计划可能躺很久才被批准 —— 期间问题可能已被别的改动修掉了。
    这时再照旧结论去改，就会改错地方或改出重复代码。

    返回 (verdicts, note, base)。base 是改动前的 (high+mid) 基线条数，
    供执行后对比「有没有把程序改差」。自检本身跑不起来时不阻断
    （后面还有 py_compile / 回滚兜底）。
    """
    from . import code_audit as CA
    try:
        res = CA.audit()
    except Exception as e:                              # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        # ⚠️ 必须返回三元组：调用方是 `precheck, note, base = ...`。
        # base=0 表示「拿不到基线」，后面就不会做退化对比（不能误判成 0 条）。
        return {}, "执行前复核跑不起来（%s），按原计划继续" % e, 0
    # 按 (文件, 种类) 归类，行号留集合 —— 允许行号小幅漂移（别的改动会让行号平移）
    cur = {}
    by_file = {}
    for f in (res.get("findings") or []):
        if f.get("level") not in ("high", "mid"):
            continue
        rel = f.get("file") or ""
        cur.setdefault((rel, f.get("kind")), set()).add(int(f.get("line") or 0))
        by_file[rel] = by_file.get(rel, 0) + 1
    base = sum(by_file.values())
    out = {}
    for a in actions or []:
        targets = a.get("targets") or []
        files = [f for f in (a.get("files") or []) if f]
        if not files:
            out[a.get("id")] = (True, "")
            continue
        if targets:
            # 精确模式：至少有一处目标问题**还在原位附近**才放行
            hit = 0
            for t in targets:
                lines = cur.get((t.get("file"), t.get("kind"))) or set()
                ln = int(t.get("line") or 0)
                if any(abs(x - ln) <= 4 for x in lines):
                    hit += 1
            if hit:
                out[a.get("id")] = (True, "")
            else:
                out[a.get("id")] = (
                    False,
                    "这条动作针对的 %d 处问题现在都找不到了（可能已被修过），跳过以免改错地方"
                    % len(targets))
            continue
        # 没有 targets 的动作 = 「加功能 / 改流程」类（由体验评估、手动提出），
        # 它们**本来就不对应任何静态问题**，拿「问题还在不在」去判断会全部误杀
        # （实测 6 条功能改进动作被 100% 跳过）。这类一律放行，交给
        # 熔断 + 行为回归 + 基线不退化三道兜底。
        out[a.get("id")] = (True, "")
    return out, "", base


def _residual_findings(plan):
    """上一轮改完之后，**同一批文件里的同类问题**是否还有残留。

    2026-11-15 实测：探针里 9 处问题，第一轮只修掉 6 处（剩死代码、finally return、
    裸 except 各一处），要人再点一次才收敛到 0。用户要求「彻底解决」——
    所以一轮执行完自动接续下一轮，直到没有残留或到上限。
    只认「同类问题」（kind 相同）且「已过复核」的，避免顺手改到无关代码。
    """
    from . import code_audit as CA
    files, kinds = set(), set()
    for a in (plan.get("actions") or []):
        if a.get("status") != "done":
            continue
        for f in (a.get("files") or []):
            if f:
                files.add(f)
        for t in (a.get("targets") or []):
            if t.get("kind"):
                kinds.add(str(t["kind"]))
    if not files:
        return []
    try:
        res = CA.audit()
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return []
    out = []
    for f in (res.get("findings") or []):
        if f.get("level") not in ("high", "mid"):
            continue
        if (f.get("file") or "") not in files:
            continue
        if kinds and (f.get("kind") or "") not in kinds:
            continue
        out.append(f)
    if not out:
        return []
    # 复核门槛不能跳：残留问题同样要 verified 才允许动手
    try:
        from . import reverify as RV
        out, _st = RV.verify_findings(out, use_llm=False)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return []
    return [f for f in out
            if (f.get("verify") or {}).get("verdict") == "verified"]


@_exec_guard
def execute_approved(pid, on_event=None, should_cancel=None, _depth=0, force=None):
    """执行这次反思里所有 status=approved 的动作。带备份 + 校验 + 失败回滚。

    `force`：人工复核放行的动作 id —— 这些跳过「反复修改熔断」这一道。
    用户要求（2026-11-16）：「如果要人工复核就在前端一个审核的按钮」。

    2026-11-15 用户要求「迭代要像普通对话一样在聊天界面跑、能看到详细操作、
    能随时终止」—— 所以加了两个可选回调：
      on_event(kind, **kw)  进度事件（step / code），由 iter_runner 收集成事件流；
      should_cancel()       返回 True 时，**当前动作跑完（含校验/回滚）后**停下，
                            剩余动作退回 pending。绝不中途打断 patch。
    """
    def _ev(kind, **kw):
        if on_event:
            try:
                on_event(kind, **kw)
            except Exception:                           # noqa: BLE001
                logger.debug("silent-except", exc_info=True)

    def _stop():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:                               # noqa: BLE001
            return False

    d, plan = _find_plan(pid)
    if not plan:
        return {"ok": False, "error": "找不到这次反思"}
    todo = [a for a in (plan.get("actions") or []) if a.get("status") == "approved"]
    if not todo:
        return {"ok": False, "error": "这次反思没有已批准的动作"}
    # 人工复核放行名单：本次调用传进来的 + 计划里持久化的（重启后仍然有效）
    _force = set(str(x) for x in (force or []))
    _force |= set(str(x) for x in (plan.get("_force") or []))

    # 动手前最后一道复核：确认这些问题现在还真的存在
    precheck, _pre_note, _base = _precheck_actions(todo)

    def _card(a, patches, ok, error="", rolled_back=False, files=None, backup=""):
        """把一次动作的结果整理成前端代码卡片要的数据。

        patches 里的 find/replace 本身就是 diff 的原料 —— 不在后端算 diff，
        前端用现成的逐行对比渲染更省事，也不会因为行尾空白对不齐。
        """
        by_file, seen = [], {}
        for pt in (patches or []):
            rel = _norm_rel(pt.get("file")) or str(pt.get("file") or "")
            if rel not in seen:
                seen[rel] = {"rel": rel, "hunks": []}
                by_file.append(seen[rel])
            seen[rel]["hunks"].append({
                "find": str(pt.get("find") or "")[:2400],
                "replace": str(pt.get("replace") or "")[:2400],
            })
        return {"action_id": a.get("id"), "title": a.get("title"),
                "risk": a.get("risk") or "mid",
                "ok": bool(ok), "error": str(error or ""),
                "rolled_back": bool(rolled_back),
                "files": by_file,
                "changed": list(files or []),
                "backup": backup or a.get("backup") or ""}

    results = []
    cancelled = False
    skipped = 0
    for _i, a in enumerate(todo):
        _rp = {}                     # 自修轮的结果（没走自修就是空）
        if _stop():
            cancelled = True
            skipped = len(todo) - _i
            _ev("step", text="收到终止请求，剩余 %d 条动作不再执行" % skipped)
            break
        _pk, _pwhy = precheck.get(a.get("id"), (True, ""))
        if not _pk:
            a["status"] = "done"
            a["note"] = ((a.get("note") or "") + "｜" + _pwhy).strip("｜")
            results.append({"id": a["id"], "ok": True, "skipped": True, "why": _pwhy})
            _log_act(plan, a, True, _pwhy)
            _ev("step", text="%s 跳过：%s" % (a.get("id"), _pwhy))
            continue
        # 第二道：反复修改熔断（防止「越改越乱」）
        _lk, _lwhy = _loop_guard(
            a.get("files"),
            root_cause=(a.get("root_cause") or a.get("detail") or ""))
        if not _lk and str(a.get("id")) not in _force:
            # ★ 2026-11-16 用户报「这么多个失败」：截图里 7 条动作全是红的，
            #   原因清一色「反复修改熔断：app.js 最近 60 分钟已改成功 10 次」。
            #   熔断的本意就是「先停手**等人工确认**」，原来却直接判 failed ——
            #   既看不出这是护栏正常动作（不是故障），也没有任何出路。
            #   现在：熔断 → 待人工复核（不是失败），前端给「复核通过并执行」按钮。
            if is_review_reason(_lwhy):
                a["status"] = "need_review"
                a["error"] = _lwhy
                a["review_reason"] = _lwhy
                results.append({"id": a["id"], "ok": False, "need_review": True,
                                "error": _lwhy})
                _log_act(plan, a, False, "待人工复核：" + _lwhy)
                _ev("step", text="%s 待人工复核：%s" % (a.get("id"), _lwhy))
                _ev("code", **_card(a, [], False, _lwhy))
                continue
            a["status"] = "failed"
            a["error"] = _lwhy
            results.append({"id": a["id"], "ok": False, "error": _lwhy})
            _log_act(plan, a, False, _lwhy)
            _ev("step", text="%s 熔断：%s" % (a.get("id"), _lwhy))
            _ev("code", **_card(a, [], False, _lwhy))
            continue
        tag = "%s_%s_%s" % (pid, a["id"], _pid())
        _ev("step", text="%s %s · 正在生成改动方案…" % (a.get("id"), a.get("title")))
        patches, err = _plan_patch_for_action(plan, a)
        if err:
            a["status"] = "failed"
            a["error"] = err
            results.append({"id": a["id"], "ok": False, "error": err})
            _log_act(plan, a, False, err)
            _ev("code", **_card(a, [], False, err))
            continue

        # 校验所有目标文件都在白名单内
        rels = []
        bad = ""
        for pt in patches:
            rel = _norm_rel(pt.get("file"))
            if not rel:
                bad = "目标文件不在允许范围：%s" % pt.get("file")
                break
            rels.append(rel)
        if bad:
            a["status"] = "failed"
            a["error"] = bad
            results.append({"id": a["id"], "ok": False, "error": bad})
            _log_act(plan, a, False, bad)
            _ev("code", **_card(a, patches, False, bad))
            continue
        # ★ 用户要求「即使要下载或重构或构建新模块也可以」—— 真正的新功能是
        #   「新模块 + 路由 + 前端 + 验收断言」4 个起步，而 _MAX_FILES_PER_ACTION=3
        #   会让它永远做不了。所以只对**功能类**动作放宽到 _MAX_FILES_FEATURE，
        #   bug 修复类仍然限 3（改得越少越安全）。
        _maxf = (_MAX_FILES_FEATURE if a.get("origin") == "feature"
                 else _MAX_FILES_PER_ACTION)
        if len(set(rels)) > _maxf:
            a["status"] = "failed"
            a["error"] = "一次动作涉及文件过多（上限 %d）" % _maxf
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            _log_act(plan, a, False, a["error"])
            _ev("code", **_card(a, patches, False, a["error"]))
            continue

        # 质量闸门（计划期，逐条 hunk）：只丢掉「占位式」的那几处，保留真实修法。
        # 实测教训：第一版是整条动作回滚 —— 9 处里 1 处占位，结果另外 8 处
        # 正确修法也一起作废，等于什么都没修。所以改成先筛后改。
        _bad, _good = [], []
        for pt in patches:
            _why = _fix_quality(a, [pt])
            (_bad if _why else _good).append((pt, _why))
        if _bad:
            a["quality_rejected"] = [
                {"file": _norm_rel(pt.get("file")) or str(pt.get("file")),
                 "why": why[0]} for pt, why in _bad]
            _ev("step", text="%s 丢弃 %d 处占位式修补（需人工确认语义），保留 %d 处真实修法"
                % (a.get("id"), len(_bad), len(_good)))
        if not _good:
            a["status"] = "failed"
            a["error"] = ("全部是占位式修补，已放弃（需人工确认语义）："
                          + "；".join(w[0] for _p, w in _bad[:2]))
            results.append({"id": a["id"], "ok": False, "error": a["error"],
                            "quality": [w[0] for _p, w in _bad]})
            _log_act(plan, a, False, a["error"])
            _ev("code", **_card(a, [p for p, _w in _bad], False, a["error"], False))
            continue
        patches = [pt for pt, _w in _good]

        # ★ 回归基线：改盘之前先记下「哪些断言本来就失败」。
        #   为什么必须这么做（2026-11-15 实测踩到）：动作后的全量回归里，
        #   任何**与本动作无关**的断言失败都会让本动作被判失败并回滚。
        #   实测 ZZVERIFY_EXEC 那个「新建模块」的动作，明明模块建好了、断言也
        #   过了，却因为库里另外两条断言是红的而被连坐回滚（fail_kind=env）。
        #   后果是：只要库里存在任何一条红色断言，自动迭代就永远成功不了。
        _base_fails = set()
        _base_ok = False
        if load_settings().get("regress_after_change", True):
            try:
                from . import regress as _RG0
                _b0 = _RG0.run_disk(scope="quick")
                _bf = list(_b0.get("failed_items") or [])
                # ★ 2026-11-15 实测修死锁：基线没跑起来（超时/崩溃/解析失败）时，
                #   run_disk 会返回一个占位失败项 {"name":"regress"}。
                #   原来直接拿它当基线 → 改动后回归里**所有**失败都成了「新增」
                #   → 任何动作都必然被判失败回滚，自动迭代实际无法成功。
                #   实测：回归本身要 164 秒，而 run_disk 超时 180 秒，基线极易超时。
                #   现在：基线不可用就置 _base_ok=False，后面只留痕、不判死。
                if not any(str(x.get("name")) == "regress" for x in _bf):
                    _base_fails = {str(x.get("name")) for x in _bf}
                    _base_ok = True
                else:
                    logger.warning("基线回归不可用（超时或崩溃），本轮不做新增失败判定")
            except Exception:                           # noqa: BLE001
                logger.debug("基线回归没跑起来", exc_info=True)

        _ev("step", text="%s 备份并应用改动（%s）…" % (
            a.get("id"), "、".join(dict.fromkeys(rels))))
        # 备份（created 里是本次要新建的文件，回滚时会被删除）
        tag2, saved, created = _backup_files(list(dict.fromkeys(rels)), tag)
        a["backup"] = tag2

        # 应用
        done_rels, fail_msg = [], ""
        for pt in patches:
            rel = _norm_rel(pt.get("file"))
            ok, m = _apply_find_replace(rel, pt.get("find") or "", pt.get("replace") or "")
            if not ok:
                fail_msg = "%s：%s" % (rel, m)
                break
            done_rels.append(rel)

        if fail_msg:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = fail_msg + "（已自动回滚）"
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            _log_act(plan, a, False, a["error"])
            _ev("step", text="%s 应用失败，已自动回滚" % a.get("id"))
            _ev("code", **_card(a, patches, False, a["error"], True))
            continue

        # 语法校验
        vbad = ""
        for rel in dict.fromkeys(done_rels):
            ok, m = _verify(rel)
            if not ok:
                vbad = "%s 语法校验失败：%s" % (rel, m)
                break
        if vbad:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = vbad + "（已自动回滚）"
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            _log_act(plan, a, False, a["error"])
            _ev("step", text="%s 语法校验没通过，已自动回滚" % a.get("id"))
            _ev("code", **_card(a, patches, False, a["error"], True))
            continue

        # 行为级回归：语法通过 ≠ 逻辑没坏。
        # 静态检查只看名字/结构，off-by-one、条件写反、返回值类型错位这类逻辑 bug
        # 它永远看不见（实测 6 类逻辑 bug，自检报 0 条）。所以改完立刻真跑一遍断言，
        # 失败就回滚 —— 这是「自动改坏代码」的最后一道兜底。
        rbad = ""
        if load_settings().get("regress_after_change", True):
            try:
                from . import regress as _RG
                _rr = _RG.run_disk(scope="quick")
                _now_fails = {str(x.get("name"))
                              for x in (_rr.get("failed_items") or [])}
                _new_fails = _now_fails - _base_fails
                if _new_fails and _base_ok:
                    # 只有**本动作引入**的失败才算它的问题
                    rbad = "行为回归新增失败 %d 条：%s" % (
                        len(_new_fails), "；".join(sorted(_new_fails)[:3]))
                elif _new_fails:
                    # 基线不可用：无法判断这些失败是不是本动作引入的。
                    # 判死会让「回归里本来就有红用例」永久卡住所有自动改动，
                    # 所以只留痕放行，把事实摆在界面上让人看见。
                    _ev("step", text="%s 基线回归不可用，%d 条失败无法归因，已放行（请人工确认：%s）"
                        % (a.get("id"), len(_new_fails),
                           "；".join(sorted(_new_fails)[:2])))
                elif not _rr.get("ok"):
                    # 既有失败（改动前就是红的）：与本动作无关，放行但要留痕
                    _ev("step", text="%s 回归有 %d 条既有失败（改动前就是红的，与本动作无关），已放行"
                        % (a.get("id"), len(_base_fails)))
                else:
                    _ev("step", text="%s 行为回归通过（%d 条 / %s 秒）" % (
                        a.get("id"), _rr.get("passed") or 0, _rr.get("seconds") or 0))
            except Exception as _e:                     # noqa: BLE001
                # ⚠️ fail-safe：回归跑不起来 = 无法确认改动安全 → 回滚。
                # 原来是「跳过、放行改动」，但那样一旦回归自身坏掉，就等于所有
                # 自动改动都不受检验 —— 用户要求「保证程序稳定」，所以宁可回滚。
                logger.warning("行为回归跑不起来", exc_info=True)
                rbad = "行为回归跑不起来（%s: %s），无法确认改动安全" % (
                    type(_e).__name__, _e)
        if rbad:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = rbad + "（已自动回滚）"
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            _log_act(plan, a, False, a["error"])
            _ev("step", text="%s 行为回归没通过，已自动回滚" % a.get("id"))
            _ev("code", **_card(a, patches, False, a["error"], True))
            continue

        # 修复质量闸门：语法过了、回归过了，也可能是「占位式修补」
        # （把出错的调用换成 return 0 —— 告警消失但语义是猜的）。
        # 用户要求「有风险就得修，但不能因为模型判断失误把好代码改坏」，
        # 这类改动属于「看起来修好了、其实埋了静默错误」，宁可回滚让人确认。
        _q = _fix_quality(a, patches)
        if _q:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = "占位式修补，已回滚（需人工确认语义）：" + "；".join(_q[:2])
            results.append({"id": a["id"], "ok": False, "error": a["error"],
                            "quality": _q})
            _log_act(plan, a, False, a["error"])
            _ev("step", text="%s 判为占位式修补，已回滚（需人工确认）" % a.get("id"))
            _ev("code", **_card(a, patches, False, a["error"], True))
            continue

        # ★ 新功能类动作：启用它自带的验收断言并**在子进程里真跑一遍**。
        #   用户要求「需要验证所有功能是否正常」—— 通用回归里没有新功能的用例，
        #   只有它自己的断言能证明「这次改动真的让功能可用了」。
        #   2026-11-15 用户追加要求：「加入之后要自动验证是否完全正常，
        #   并反复改到完全正常为止」—— 所以断言不过**不再一次判死**，
        #   而是带着失败输出自修（默认 3 轮），仍不过才回滚。
        if a.get("origin") == "feature" and a.get("assert_file"):
            try:
                from . import selfevolve
                selfevolve.unpark_test(a.get("title"),
                                       path=(a.get("assert_file") or None))
                _vr = selfevolve.verify_one(
                    a.get("title"), path=(a.get("assert_file") or None))
                if not _vr.get("ok"):
                    _rp = _repair_feature(
                        plan, a, list(dict.fromkeys(done_rels)),
                        str(_vr.get("why") or "断言失败"),
                        rounds=int(load_settings().get(
                            "feature_repair_rounds") or 3),
                        ev=_ev)
                    a["repair"] = {"rounds": int(_rp.get("rounds") or 0),
                                   "history": _rp.get("history") or []}
                    if not _rp.get("ok"):
                        _restore(tag2)
                        # 自修可能新建了 tag2 不知道的文件，一并删掉，别留孤儿。
                        # 实测漏过：自修轮里新建的文件只有成功时才并入 done_rels，
                        # 失败路径必须从 _rp["made"] 里取，否则残留在项目里。
                        _orphans = list(dict.fromkeys(
                            [r for r in done_rels if r] +
                            [r for r in (_rp.get("made") or []) if r]))
                        for _r3 in _orphans:
                            if _r3 not in rels:
                                try:
                                    _p3 = _ROOT / _r3
                                    if _p3.is_file():
                                        _p3.unlink()
                                except Exception:       # noqa: BLE001
                                    logger.debug("silent-except", exc_info=True)
                        selfevolve.park_test(a.get("title"))
                        a["status"] = "failed"
                        a["error"] = ("新功能验收断言自修 %d 轮后仍未通过，已回滚：%s"
                                      % (int(_rp.get("rounds") or 0),
                                         str(_rp.get("why") or "")[:200]))
                        results.append({"id": a["id"], "ok": False,
                                        "error": a["error"], "assert": _vr,
                                        "repair": a["repair"]})
                        _log_act(plan, a, False, a["error"])
                        _ev("step", text="%s 自修 %d 轮仍未通过，已回滚" % (
                            a.get("id"), int(_rp.get("rounds") or 0)))
                        _ev("code", **_card(a, patches, False, a["error"], True))
                        continue
                    a["note"] = ((a.get("note") or "")
                                 + "｜验收断言经 %d 轮自修后通过"
                                 % int(_rp.get("rounds") or 0)).strip("｜")
                    # 自修改过的文件可能超出原 patch，补进结果，方便查看与回滚
                    for _p2 in (_rp.get("patches") or []):
                        _r2 = _norm_rel(_p2.get("file"))
                        if _r2 and _r2 not in done_rels:
                            done_rels.append(_r2)
                    _ev("step", text="%s 自修 %d 轮后验收断言通过" % (
                        a.get("id"), int(_rp.get("rounds") or 0)))
                else:
                    a["note"] = ((a.get("note") or "")
                                 + "｜验收断言通过（%ss）" % _vr.get("seconds")).strip("｜")
            except Exception as e:                      # noqa: BLE001
                # 断言机制本身出问题不能把已经正确的改动判死，只记录
                a["note"] = ((a.get("note") or "")
                             + "｜断言验证异常：%s: %s"
                             % (type(e).__name__, e)).strip("｜")

        a["status"] = "done"
        a["error"] = ""
        # 2026-11-15 用户要求「每次自动迭代详细说明实现了什么功能或改了什么」。
        # 补丁本身就是原料（find/replace）；自修轮的补丁也必须算进来，
        # 否则「自修 2 轮才通过」的功能，摘要里看不到那 2 轮到底改了什么。
        _allpt = list(patches or []) + list((_rp or {}).get("patches") or [])
        a["changelog"] = _change_summary(_allpt)
        results.append({"id": a["id"], "ok": True, "files": list(dict.fromkeys(done_rels)),
                        "backup": tag2, "changelog": a["changelog"]})
        _log_act(plan, a, True, (a["changelog"].get("summary")
                                 or "改了：" + "、".join(dict.fromkeys(done_rels))))
        _ev("step", text="%s 完成（%s）" % (
            a.get("id"), "、".join(dict.fromkeys(done_rels))))
        _ev("code", **_card(a, patches, True, "", False,
                            list(dict.fromkeys(done_rels)), tag2))

    if cancelled:
        # 被用户终止：没跑的那些退回「待批准」，下次还能接着批，不会丢
        for a in (plan.get("actions") or []):
            if a.get("status") == "approved":
                a["status"] = "pending"
                a["error"] = "上一轮被终止，尚未执行"
    save_plans(d)
    ok_n = sum(1 for r in results if r["ok"])
    out = {"ok": ok_n > 0, "done": ok_n, "total": len(results), "results": results,
           "plan": plan,
           "cancelled": cancelled, "skipped": skipped,
           "need_restart": ok_n > 0}

    # 用户要求：每次代码被改完就自动跑一次自检。
    # ⚠️ create_plan=False —— 否则自检又会建计划、又会被信任模式执行，
    #    变成「自检→改→自检→改」的死循环。
    if ok_n > 0 and load_settings().get("audit_after_change"):
        try:
            a = run_audit(source="after_change", create_plan=False)
            out["post_audit"] = a.get("audit") or {}
            out["post_audit_findings"] = (a.get("findings") or [])[:12]
            # 第三道护栏：基线不退化。
            # 用户要求「确保原来没问题的程序不因为模型判断失误被改坏」。
            # 单条动作都过了回归，但多动作叠加、或回归没覆盖到的静态问题，
            # 仍可能让整体变差 —— 一旦 high+mid 比动手前多，就回滚最后一个改动。
            if _base:
                _pc = (a.get("audit") or {}).get("counts") or {}
                _post_n = int(_pc.get("high") or 0) + int(_pc.get("mid") or 0)
                out["degrade"] = {"base": _base, "after": _post_n}
                if _post_n > _base:
                    _last = None
                    for _r in reversed(results):
                        if _r.get("ok") and not _r.get("skipped") and _r.get("backup"):
                            _last = _r
                            break
                    if _last:
                        _restore(_last["backup"])
                        for _a2 in (plan.get("actions") or []):
                            if _a2.get("id") == _last.get("id"):
                                _a2["status"] = "pending"
                                _a2["error"] = ("改动让自检问题从 %d 增到 %d，已自动回滚"
                                                % (_base, _post_n))
                        save_plans(d)
                        try:
                            from . import code_audit as _CA2
                            _c2 = _CA2.audit().get("counts") or {}
                            _after = int(_c2.get("high") or 0) + int(_c2.get("mid") or 0)
                        except Exception:               # noqa: BLE001
                            _after = -1
                        out["degrade"]["rolled_back"] = _last.get("id")
                        out["degrade"]["after_rollback"] = _after
                        out["need_restart"] = any(
                            _r.get("ok") and not _r.get("skipped") and _r is not _last
                            for _r in results)
                        _ev("step", text="改动引入了新的自检问题（%d→%d），已回滚 %s"
                            % (_base, _post_n, _last.get("id")))
        except Exception as e:                          # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
            out["post_audit"] = {"error": "%s: %s" % (type(e).__name__, e)}

    # ---------------- 单轮内自动收敛（问题二：修不全要人再点一次）
    # 实测：探针 9 处问题，第一轮只修掉 6 处。这里自动接续下一轮，直到
    # 没有同类残留或到上限（默认 2 轮），用户不用手动再触发一次。
    # 残留问题同样要过复核（verified）才允许动手，门槛不降。
    if (not cancelled) and ok_n > 0 and _depth < int(
            load_settings().get("converge_rounds", 2) or 0):
        try:
            resid = _residual_findings(plan)
            if resid:
                more = _actions_from_findings(resid, limit=3)
                if more:
                    # ⚠️ 必须自己分配 id：_actions_from_findings 只产出「候选动作」，
                    # id 原本是 _make_plan 建计划时统一编号的。收敛是直接往现有计划
                    # 里追加，不补 id 的话执行时 `a["id"]` 直接 KeyError ——
                    # 实测就是这样静默失败了（异常被 except 吞掉，第 2 轮根本没跑）。
                    _base_n = len(plan.get("actions") or [])
                    for _k, na in enumerate(more):
                        na["id"] = "a%d" % (_base_n + _k + 1)
                        na["status"] = "approved"
                        na["note"] = "第 %d 轮收敛：上一轮改完仍有残留" % (_depth + 2)
                    plan["actions"].extend(more)
                    save_plans(d)
                    _ev("step", text="第 %d 轮：还有 %d 处同类残留，自动接续修复"
                        % (_depth + 2, len(resid)))
                    sub = execute_approved(pid, on_event=on_event,
                                           should_cancel=should_cancel,
                                           _depth=_depth + 1)
                    out["converge"] = {"round": _depth + 2,
                                       "residual": len(resid),
                                       "done": sub.get("done") or 0}
                    out["done"] = ok_n + int(sub.get("done") or 0)
                    out["results"] = results + (sub.get("results") or [])
                    out["need_restart"] = bool(out.get("need_restart")
                                               or sub.get("need_restart"))
                    if sub.get("converge"):
                        out["converge"].update(sub["converge"])
        except Exception as _e:                         # noqa: BLE001
            # 收敛失败不能静默：第一版就是因为异常被 debug 吞掉，
            # 第 2 轮其实没跑，但界面上看起来「已经收敛过了」。
            logger.warning("自动收敛失败", exc_info=True)
            out["converge_error"] = "%s: %s" % (type(_e).__name__, _e)
            _ev("step", text="自动收敛失败：%s: %s" % (type(_e).__name__, _e))

    # 2026-11-15：给每条失败补上「失败类型」（用户问「为什么老显示失败」）。
    # 查 11 条历史失败发现：5 条是护栏按设计拦下的、6 条是真问题，但界面上一模一样。
    # 这里统一打标，前端据此分色显示「护栏拦截（正常）」/「模型输出」/「环境」/「未做成」。
    try:
        for _a in (plan.get("actions") or []):
            if isinstance(_a, dict) and _a.get("status") == "failed":
                _a["fail_kind"] = classify_fail(_a.get("error") or "")
        save_plans(d)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return out


def _restore(tag):
    """把备份覆盖回去；本次新建的文件则删除。"""
    dst = _BACKUPS / tag
    man = _read_json(dst / "_manifest.json", {"files": []})
    for rel in man.get("files") or []:
        src = dst / rel.replace("/", "__")
        if src.is_file():
            try:
                shutil.copy2(src, _ROOT / rel)
            except Exception:                       # noqa: BLE001
                logger.debug("silent-except", exc_info=True)
    for rel in man.get("created") or []:
        try:
            p = _ROOT / rel
            if p.is_file():
                p.unlink()
        except Exception:                           # noqa: BLE001
            logger.debug("silent-except", exc_info=True)


def rollback(pid):
    """把某次反思里所有 done 的动作回滚到改动前。"""
    d, plan = _find_plan(pid)
    if not plan:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in plan.get("actions") or []:
        if a.get("status") != "done" or not a.get("backup"):
            continue
        _restore(a["backup"])
        a["status"] = "pending"
        a["error"] = ""
        a["note"] = (a.get("note") or "") + " [已回滚 %s]" % _now()
        hit += 1
    save_plans(d)
    return {"ok": hit > 0, "rolled_back": hit, "plan": plan}


# ---------------------------------------------------------------- 计划构造

def _make_plan(trigger, summary, actions, extra=None):
    """建一个 plan 并落盘（反思、自检、诊断都走这里，保证结构一致）。"""
    norm = []
    for i, a in enumerate(actions or []):
        if not isinstance(a, dict):
            continue
        a = dict(a)
        a.setdefault("id", "a%d" % (i + 1))
        a.setdefault("status", "pending")     # pending/approved/rejected/done/failed
        a.setdefault("note", "")
        a.setdefault("error", "")
        a.setdefault("backup", "")
        a.setdefault("risk", "mid")
        a.setdefault("files", [])
        a.setdefault("detail", "")
        a.setdefault("title", "（未命名动作）")
        norm.append(a)
    plan = {"id": _pid(), "ts": _now(), "trigger": trigger,
            "summary": str(summary or "")[:200],
            "weaknesses": [], "user_insights": [], "features": [],
            "tech_takeaways": [], "actions": norm,
            "tech_raw": {}, "tech_total": 0, "signals_used": 0}
    if extra:
        plan.update(extra)
    d = load_plans()
    d["items"].insert(0, plan)
    d["items"] = d["items"][:60]
    save_plans(d)
    return plan


# ---------------------------------------------------------------- 代码自检

def _audit_history():
    d = _read_json(_AUDITS, {"items": []})
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"items": []}
    return d


def _save_audit(rec):
    d = _audit_history()
    d["items"].insert(0, rec)
    d["items"] = d["items"][:20]
    _write_json(_AUDITS, d)


def _file_catalog():
    """允许改的文件清单（白名单内的真实文件）。

    ⚠️ 必须喂给 LLM：实测让它诊断「闭嘴效果不好」，它编出了
    `backend/mouth_inpaint.py`、`backend/face_edit_pipeline.py` 这种
    **根本不存在**的文件名（真实文件是 avatar_mouth.py / avatar2.py）。
    不给清单，它就会一本正经地指向空气，执行时要么失败要么凭空造垃圾文件。
    """
    out = []
    for base in ("backend", "frontend"):
        p = _ROOT / base
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in _ALLOWED_EXT:
                continue
            if "__pycache__" in f.parts:
                continue
            out.append(str(f.relative_to(_ROOT)).replace("\\", "/"))
    return out


def _catalog_text():
    cat = _file_catalog()
    return "、".join(cat) if cat else "（空）"


def _keep_real_files(files):
    """只保留真实存在的文件（防止 LLM 编文件名）。"""
    exist = set(_file_catalog())
    return [f for f in (files or []) if f in exist]


def _phrase_set(s):
    """把标题切成「短语集合」，用于判断两条建议是不是同一件事。

    为什么需要它：2-gram Jaccard 会被长英文标识和不同词序稀释。
    实测「接线 reply_gate.format_reply：空输出兜底 + 结论前置」和
    「回复收尾协议：结论前置 + 空输出兜底」是同一条需求，但 Jaccard 只有 0.219；
    切成短语后「空输出兜底」「结论前置」两个短语都重合，一眼就看得出来。
    """
    out = set()
    for p in re.split(r"[^\u4e00-\u9fa5a-zA-Z0-9_.]+", str(s or "")):
        p = p.strip("._")
        if not p:
            continue
        if re.search(r"[\u4e00-\u9fa5]", p):
            if len(p) >= 2:
                out.add(p)
        elif len(p) >= 4:
            out.add(p.lower())
    return out


def _similar_title(a, b):
    """判断两条建议是不是在说同一件事（用于 actions/features 去重）。

    实测：模型会把同一个需求**同时**写进 actions 和 features（比如
    「验证类回复强制结论优先模板」和「验证类请求的结论优先回复模板」），
    转成动作后就是两条动作改同一处 —— 既浪费名额，又可能互相覆盖。

    ★ 2026-11-15 下调阈值（0.45 → 双档 0.20 / 0.09+短语）：
      原阈值 0.45 太严，实测最新一轮计划里 a1/f5（回复收尾协议）和
      a2/f6（失败分类看板）是同一件事写了两遍，Jaccard 只有 0.219 和 0.098，
      一条都没拦住 —— 于是同一处代码被两条动作各改一遍。
      拿 28 条历史计划里 38 对「改同一批文件」的动作做回归：
        · jac >= 0.098 的 5 对**全是**真重复（回复收尾/失败看板/基线快照/
          断点续跑/强制 patch），
        · jac <= 0.083 的**都不是**（如「修脖子乱扭」vs「修静默 bug」，
          两件不同的事恰好改同一批文件）。
      所以在 0.083 和 0.098 之间取 0.09 作下限，并要求至少 1 个短语重合兜底，
      避免只靠低 Jaccard 误伤。0.20 以上直接判重（高置信区）。
    """
    ra, rb = str(a or ""), str(b or "")

    def _n(s):
        return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", str(s or ""))
    a, b = _n(ra), _n(rb)
    if not a or not b:
        return False
    if a in b or b in a:
        return True

    def _g(s):
        return set(s[i:i + 2] for i in range(max(0, len(s) - 1)))
    ga, gb = _g(a), _g(b)
    if not ga or not gb:
        return False
    r = len(ga & gb) / float(len(ga | gb))
    if r >= 0.45:
        return True
    if r >= 0.20:
        return True
    if r >= 0.09 and (_phrase_set(ra) & _phrase_set(rb)):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# 两阶段反思：论文回灌闭环
#
# 用户要求（2026-11-15）：「模型可以主动根据用户潜在需求去寻找可能有用的论文并且
# 参考构思，然后设计出对用户有用的新功能」「即使是只出现在论文上的也可以」。
#
# 原来的缺口（实测确认）：features 和论文召回是**两条互不相干的线** —— 论文是用
# 「旧痛点」召回的，而 features 是模型在同一轮里凭记忆产出的，theory 字段也只能
# 靠它自己回忆。于是「理论上可行」这句话**没有任何依据**，编了也看不出来。
#
# 这里补上闭环：模型先想功能 → 拿功能去论文库真检索 → 用真实论文回头校准功能
# （补 theory / 改 how / 写 assert_code）。召不到就如实标注，不许编。
# ═══════════════════════════════════════════════════════════════════════════

# 模型**不许**改的字段：title 是去重键，files 已经过 _norm_rel 校验（改坏了会写错
# 文件），pain 是溯源。只允许它补理论、改做法、写断言。
_ENRICH_LOCKED = ("title", "files", "pain")


def _papers_for_features(feats, top_k=3):
    """给每个候选功能召回相关论文。

    返回 (按功能下标分组的命中, 全局去重扁平清单, 错误信息)。
    ⚠️ 检索失败必须降级成「没有论文」，**不能**让整个反思挂掉 —— 论文只是锦上添花，
    没有它功能照样能立项（只是 theory 要如实留空）。
    """
    grouped, flat, err = {}, [], ""
    try:
        from . import papervec
    except Exception as e:                              # noqa: BLE001
        return grouped, flat, "导入 papervec 失败：%s: %s" % (type(e).__name__, e)
    for i, f in enumerate(feats or []):
        if not isinstance(f, dict):
            continue
        q = " ".join(str(f.get(k) or "")
                     for k in ("title", "why", "how", "pain")).strip()
        if not q:
            continue
        try:
            got = papervec.search(q[:300], top_k=top_k) or []
        except Exception as e:                          # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
            logger.warning("功能论文召回失败（第 %d 条）：%s", i, err)
            continue
        keep = []
        for it in got:
            if not isinstance(it, dict):
                continue
            t = str(it.get("title") or "").strip()
            if not t:
                continue
            keep.append({
                "title": t[:200],
                "zh_title": str(it.get("zh_title") or "")[:80],
                "zh_points": str(it.get("zh_points") or "")[:300],
                "summary": str(it.get("summary") or "")[:300],
                "url": str(it.get("url") or "")[:200],
            })
        if keep:
            grouped[i] = keep
            for it in keep:
                if not any(x["title"] == it["title"] for x in flat):
                    flat.append(it)
    return grouped, flat, err


def _enrich_prompt(feats, papers):
    """第二轮 LLM 的提示词：拿真实论文校准候选功能。"""
    import json as _json
    slim = []
    for f in feats:
        slim.append({k: f.get(k) for k in
                     ("title", "why", "how", "pain", "anchor", "files", "risk")
                     if f.get(k)})
    return """你在为一个本地多模态应用做「新功能立项评审」。

下面第一部分是它自己想出来的候选新功能（还没有任何理论依据）；
第二部分是我**用每个功能的描述去论文库里真实检索到的论文**（可能为空）。

请对每个功能做四件事，然后**只返回 JSON**：

1. 判断理论可行性：有论文直接支撑写「有论文支撑」；纯工程可做写「工程可行」；
   明显做不到写「不可行」——写进 theory 字段的开头。
2. theory 字段只能引用第二部分里**真实存在的论文标题**（可以只写标题前 40 字）。
   如果第二部分是空的，就写「（无论文，工程可行）」，**绝对不许编造论文名、
   作者、年份或「某论文提出…」**。编出来的理论比没有更糟，会被当成依据去改代码。
3. 如果论文里有更具体的做法，改写 how 和 anchor（要落到真实函数名上）。
4. 补一条 assert_code：一段能直接 `python xxx.py` 跑完的断言代码，
   用 assert 判断「这个功能做完之后成立的事」。不许 input()、不许联网、
   不许读 data/ 以外的路径、不许超过 60 秒。

硬约束（违反会被丢弃）：
- title / files / pain 三个字段**必须原样返回**，一个字都不许改。
- 不许新增、删除、合并功能；返回数量必须正好 %d 条。
- 每条只允许出现下面这些字段：title, files, pain, why, how, anchor, theory, verify, assert_code, risk

候选功能：
%s

检索到的真实论文：
%s

返回格式：{"features":[{...}, ...]}（正好 %d 条）""" % (
        len(feats),
        _json.dumps(slim, ensure_ascii=False, indent=1)[:4000],
        _json.dumps(papers, ensure_ascii=False, indent=1)[:4000] if papers
        else "[]（本轮没召回到相关论文）",
        len(feats),
    )


def _enrich_features_with_papers(feats, timeout=90):
    """两阶段闭环的第二步：用真实论文校准候选功能。

    返回 (增强后的 features, 元信息)。任何一步失败都返回**原始 features**，
    只把原因写进元信息 —— 宁可不增强，也不能让反思因为论文环节挂掉。
    """
    meta = {"ok": False, "papers": 0, "recalled": 0, "error": "",
            "with_theory": 0, "no_theory": 0}
    feats = [f for f in (feats or []) if isinstance(f, dict)]
    if not feats:
        return feats, meta

    def _mark_no_paper(items, reason):
        """没拿到论文时**显式**把 theory 写成「查过、没有」，而不是留空。

        ⚠️ 为什么不能留空（2026-11-15 实测）：theory 为空时
        `_features_to_actions` 里 `if theory:` 直接跳过，动作 detail 里
        连一行「理论依据」都没有 —— 用户看到的是「这个功能没提依据」，
        分不清是「查了没查到」还是「压根没查」。实测 38 条 feature 动作里
        有 11 条属于这种静默状态。
        """
        out = []
        for f in items:
            g = dict(f)
            if not str(g.get("theory") or "").strip():
                g["theory"] = reason
            out.append(g)
        return out

    t0 = time.time()
    grouped, flat, perr = _papers_for_features(feats, top_k=3)
    meta["recalled"] = len(grouped)
    meta["papers"] = len(flat)
    if perr:
        meta["error"] = perr
    if not flat:
        # 没有论文就不浪费一次 LLM 调用；但要如实标注「查过、没有」。
        meta["error"] = meta["error"] or "论文库没有召回到相关论文"
        feats = _mark_no_paper(feats, "（未召回到相关论文，工程可行）")
        meta["no_theory"] = len(feats)
        return feats, meta
    try:
        txt, err = _llm_text(_enrich_prompt(feats, flat),
                             max_tokens=4200, temperature=0.35)
    except Exception as e:                              # noqa: BLE001
        meta["error"] = "%s: %s" % (type(e).__name__, e)
        # ★ 论文召回到了、但 LLM 校准挂了：也要标注原因。
        #   原来这里直接返回原始 feats，theory 是空的 —— 用户分不清
        #   是「论文库没查到」还是「LLM 挂了」，实测两种都表现为「没有依据」。
        feats = _mark_no_paper(feats, "（论文校准未完成：%s，工程可行）"
                               % type(e).__name__)
        meta["no_theory"] = len(feats)
        return feats, meta
    if err:
        meta["error"] = err
        feats = _mark_no_paper(feats, "（论文校准未完成：LLM 调用失败，工程可行）")
        meta["no_theory"] = len(feats)
        return feats, meta
    data = _parse_json(txt)
    got = (data or {}).get("features") if isinstance(data, dict) else None
    if not isinstance(got, list) or not got:
        meta["error"] = "模型没返回合法 features"
        feats = _mark_no_paper(feats, "（论文校准未完成：模型返回格式不对，工程可行）")
        meta["no_theory"] = len(feats)
        return feats, meta
    # 按 title 对齐（title 被锁死，理论上不会变；变了就按位置兜底）
    by_title = {}
    for g in got:
        if isinstance(g, dict):
            t = str(g.get("title") or "").strip()
            if t:
                by_title[t] = g
    merged, changed = [], 0
    for i, f in enumerate(feats):
        g = by_title.get(str(f.get("title") or "").strip())
        if g is None and i < len(got) and isinstance(got[i], dict):
            g = got[i]
        if not isinstance(g, dict):
            merged.append(f)
            continue
        new = dict(f)
        for k in ("why", "how", "anchor", "theory", "verify",
                  "assert_code", "risk"):
            v = g.get(k)
            if isinstance(v, str) and v.strip() and v.strip() != str(f.get(k) or "").strip():
                if k == "risk" and v.strip().lower() not in ("low", "mid", "high"):
                    continue
                new[k] = v.strip() if k != "risk" else v.strip().lower()
                changed += 1
        # ★ 模型可能压根没返回 theory（或返回空）：这里显式兜底，
        #   让「查过但没有」和「忘了查」在界面上长得不一样。
        if not str(new.get("theory") or "").strip():
            new["theory"] = "（本轮未给出论文依据，工程可行）"
        merged.append(new)

    # 统计「有几条真的引到了论文」——用户要看的是「创新有依据吗」，
    # 不是「跑了几次 LLM」。判据：theory 里出现了「论文支撑」或书名号《》。
    # ★ 2026-11-15：统计口径必须和动作里的 has_paper **完全一致**，否则
    #   同一条功能会出现「统计说有依据、动作说没有」的自相矛盾。
    #   原来这里是关键词判定（含《》就算），会把「无论文直接支撑…可参考《X》」
    #   也算成有依据 —— 现在统一交给 papervec 核验。
    def _has_paper(f):
        try:
            from . import papervec
            return bool(papervec.check_citation(
                str(f.get("theory") or ""),
                "\n".join(str(f.get(k) or "") for k in ("title", "pain", "why", "how")),
            ).get("counts"))
        except Exception:                  # noqa: BLE001
            return False

    with_p = [f for f in merged if _has_paper(f)]
    meta["with_theory"] = len(with_p)
    meta["no_theory"] = len(merged) - len(with_p)
    meta["ok"] = changed > 0
    meta["changed"] = changed
    meta["seconds"] = round(time.time() - t0, 2)
    if not meta["ok"]:
        meta["error"] = meta["error"] or "模型没有提出任何改进（原样返回）"
    return merged, meta


def _ensure_run(code):
    """把断言归一化成「带 run() 函数」的形式。

    ⚠️ 为什么必须做（2026-11-15 实测）：selfevolve.write_test 的 check_code 硬性
    要求代码里有 `def run(`，而模型**经常直接写顶层 assert** —— 实测 3 条候选断言
    3 条全被「缺少 run() 函数」拒收，等于断言功能形同虚设，一个都没落盘。
    这里自动包一层，模型两种写法都能用。
    """
    code = str(code or "").strip()
    if not code:
        return ""
    if "def run(" in code:
        return code
    body = "\n".join(("    " + ln) if ln.strip() else ln
                     for ln in code.splitlines())
    return "def run():\n" + body


def _runnable(code):
    """变成「能独立跑完」的脚本：有 run() 就调它，没有就包一层再调。

    ⚠️ 不这么做的话，模型写 `def run(): assert ...` 时 dryrun 只定义函数、从不调用，
    永远 returncode 0 —— 会**假通过**，把写错的断言当成好的收下。
    """
    code = str(code or "").strip()
    if not code:
        return ""
    return _ensure_run(code) + "\n\nrun()\n"


def _dryrun_assert(code, timeout=25):
    """把候选断言真跑一遍，判断它属于哪种情况。

    为什么必须跑（2026-11-15 实测）：模型写的断言**自己都可能跑不过** ——
    实测出现过 `assert h0 == h1, 'except-wrapped same behavior keeps AST hash equal'`
    直接 AssertionError。如果这种断言直接落盘，等动作做完再 unpark，回归会永久红，
    而且没人知道到底是功能没做好还是断言写错了。

    返回 (verdict, detail)：
      "ok"      现在就通过（纯逻辑断言，很理想）
      "pending" 断言**执行了但不成立**，或因名字/属性/导入不存在而失败
                —— 功能还没实现，**这是正常的**，保留并挂起
      "broken"  断言自身写错（语法错，或 TypeError/ValueError 这类逻辑错）
                —— 丢弃，不污染回归
      "timeout" 超时 —— 丢弃（可能死循环）

    ★ 2026-11-15 修致命设计错误：原来把 AssertionError 归进 "broken" 丢弃。
      但 AssertionError 恰恰是「断言跑通了、功能还没做所以不成立」——这是
      新功能的**正常待验状态**。实测后果：两条新功能的断言都因
      「AssertionError: workspace 应有写入能力」被整条丢弃 → assert_file 为空
      → 功能没有验收标准 → 用户要求的「加完自动验证、反复改到正常」永远不触发。
      NameError 同理（引用了还没实现的函数名）。
      现在只有**语法错**和 TypeError/ValueError 这类真逻辑错才算 broken。
    """
    import subprocess
    import tempfile
    code = str(code or "").strip()
    if not code:
        return "broken", "断言为空"
    full = _runnable(code)
    # 1) 先编译 —— 语法错才是「断言本身写错了」，这是唯一可以确定的 broken
    try:
        compile(full, "<feature_assert>", "exec")
    except SyntaxError as e:
        return "broken", "语法错误：第 %s 行 %s" % (e.lineno, e.msg)
    fd, p = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(full)
        r = subprocess.run([sys.executable, p], capture_output=True, text=True,
                           timeout=timeout, cwd=str(_ROOT))
        if r.returncode == 0:
            return "ok", ""
        err = (r.stderr or "")[-600:]
        tail = err.strip().splitlines()[-1] if err.strip() else "rc=%s" % r.returncode
        # 2) 断言执行了但不成立 / 引用了还不存在的名字 → 功能未实现，保留
        if ("AssertionError" in err or "ImportError" in err
                or "ModuleNotFoundError" in err or "AttributeError" in err
                or "NameError" in err):
            return "pending", tail[:200]
        # 3) 其余（TypeError/ValueError…）是断言逻辑写错 → 丢弃
        return "broken", tail[:200]
    except subprocess.TimeoutExpired:
        return "timeout", "超过 %ds" % timeout
    except Exception as e:                              # noqa: BLE001
        return "broken", "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            os.unlink(p)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)


def _features_to_actions(features, start=0, limit=4, existing=None):
    """把「可增加的功能」转成**可执行动作** —— 用户要求主动迭代落地新技术。

    2026-11-15 用户原话：「只要是对用户有用，且理论上可行，可以借鉴论文参考，
    主动迭代增加这项新技术，然后记得需要跟用户说明为什么」。

    原来的 features 只做展示（前端一行 listHtml），**永远不会变成改动** ——
    用户看得到、点不了。这里把它接进同一条执行链路：信任模式下自动批准，
    并和 bug 修复一样过预检/熔断/回归/基线四道护栏。

    ⚠️ 刻意**不**给功能动作设 targets：targets 是「这条动作针对哪几处静态问题」，
    功能类本来就不对应静态问题；设了会被预检当成「问题已不存在」全部跳过
    （实测踩过：6 条功能改进动作被 100% 误杀）。
    """
    out = []
    try:
        exist = set(_file_catalog())
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        exist = set()
    seen = [str(x or "") for x in (existing or []) if x]
    for f in features or []:
        if len(out) >= limit:
            break
        if not isinstance(f, dict):
            continue
        title = str(f.get("title") or "").strip()
        if not title:
            continue
        if any(_similar_title(title, t) for t in seen):
            logger.debug("功能与已有动作重复，跳过：%s", title[:40])
            continue
        seen.append(title)
        # ★ 2026-11-15 修致命缺陷：原来这里要求 `n in exist`（文件必须已存在），
        #   于是**新建模块类的功能 files 永远是空的** → _plan_patch_for_action 报
        #   「没有可读的目标文件」→ 自主创新 100% 落不了地（实测：新建
        #   backend/_verify_feature_demo.py 直接失败）。
        #   而 _plan_patch_for_action 本来**就支持新建**（find="" + replace=全文），
        #   只是被这一行挡住了。_norm_rel 已经做了目录白名单，保留新路径是安全的。
        files, fresh = [], []
        for x in (f.get("files") or []):
            if not isinstance(x, str):
                continue
            n = _norm_rel(x)
            if not n or n in files:
                continue
            files.append(n)
            if n not in exist:
                fresh.append(n)
        if fresh:
            # 明确告诉执行器/用户：这些是**新建**的，不是改已有的
            f.setdefault("_new_files", fresh)
        why = str(f.get("why") or "").strip()
        how = str(f.get("how") or "").strip()
        anchor = str(f.get("anchor") or "").strip()
        theory = str(f.get("theory") or "").strip()
        verify = str(f.get("verify") or "").strip()
        pain = str(f.get("pain") or "").strip()
        risk = str(f.get("risk") or "mid").lower()
        if risk not in ("low", "mid", "high"):
            risk = "mid"
        # ★ 2026-11-15：theory 原来排在 parts 第 5 位，而 detail 是
        #   "\n".join(parts)[:900] —— pain/why/how/anchor 一长，理论依据
        #   直接被截断，界面上等于没有。现在提到第 2 位，保证它一定看得见。
        if not theory:
            theory = "（未给出论文依据，按工程可行性判断）"
        # detail 里只放摘要：theory 全文（可到 600 字）如果整段塞进
        # parts 第 2 位，会把后面的 how/verify 挤出 900 字上限 ——
        # 那是给模型看的执行指令，丢了会直接影响改得对不对。
        _t_short = theory if len(theory) <= 220 else (theory[:220] + "…")
        # ★ 2026-11-15 用户要求：has_paper 不能只是「有没有书名号」。原来
        #   模型写「无论文直接支撑X…可参考《Y》」也会被算成有依据，编造的
        #   标题更看不出来。这里核两件事：标题是否真在库里、与功能多相关。
        try:
            from . import papervec as _pv
            _rel = _pv.check_citation(
                theory, "\n".join(x for x in (title, pain, why, how) if x))
        except Exception as _e:            # noqa: BLE001
            _rel = {"level": "unknown", "score": 0.0, "rank": 0,
                    "titles": [], "found": [], "missing": [], "disclaimer": False,
                    "why": "核验失败：%s: %s" % (type(_e).__name__, _e)}
        parts = []
        if pain:
            parts.append("对应痛点：%s" % pain)
        parts.append("理论/论文依据：%s" % _t_short)
        parts.append("依据核验：%s" % _rel.get("why"))
        if why:
            parts.append("为什么值得做：%s" % why)
        if how:
            parts.append("怎么做：%s" % how)
        if anchor:
            parts.append("改哪里：%s" % anchor)
        if verify:
            parts.append("怎么验证：%s" % verify)
        if not files:
            parts.append("⚠️ 没给出可改的真实文件，执行时只能靠模型自己找位置。")
        elif fresh:
            parts.append("🆕 将新建：%s（新模块，失败会整份删掉）"
                         % "、".join(fresh))
        # ★ 新功能必须自带**可执行**的验收断言。用户明确要求「需要验证所有功能
        #   是否正常」—— 没有断言就只能靠肉眼，等于没验证。
        #   落盘后立刻挂起（_pending_ 前缀，回归跳过）：功能还没实现，它现在必然
        #   失败，留在回归里会把回归弄成永久红。等动作执行成功后 unpark + verify。
        tcode = str(f.get("assert_code") or "").strip()
        tfile = ""
        if tcode:
            # ★ 落盘前真跑一遍：模型写的断言**自己都可能跑不过**（实测出现过
            #   AssertionError）。写错了的断言一旦进回归，会永久红且分不清是
            #   功能没做好还是断言写错了 —— 必须在落盘前就拦掉。
            verdict, vwhy = _dryrun_assert(tcode)
            if verdict in ("broken", "timeout"):
                parts.append("⚠️ 模型给的断言自身跑不过（%s：%s），已丢弃 —— "
                             "功能仍会实现，但没有自动验收，需人工确认。"
                             % (verdict, vwhy))
                tcode = ""
            elif verdict == "ok":
                parts.append("断言自测：现在就能通过（纯逻辑断言）")
            else:
                parts.append("断言自测：待功能实现后生效（%s）" % vwhy)
        if tcode:
            try:
                from . import selfevolve
                _tok, _tp = selfevolve.write_test(title, _ensure_run(tcode),
                                                  note=pain or why)
                if _tok:
                    selfevolve.park_test(title)
                    tfile = str(_tp)
                    parts.append("验收断言：%s（功能做完后自动启用并执行）" % _tp)
                else:
                    parts.append("⚠️ 验收断言被拒绝写入：%s" % _tp)
            except Exception as e:                      # noqa: BLE001
                parts.append("⚠️ 验收断言写入失败：%s: %s"
                             % (type(e).__name__, e))
        else:
            parts.append("⚠️ 没给验收断言（assert_code 为空），做完只能人工看效果。")
        out.append({
            "id": "f%d" % (start + len(out) + 1),
            "title": title[:120],
            "detail": "\n".join(parts)[:900],
            "risk": risk,
            "files": files[:_MAX_FILES_FEATURE],
            "origin": "feature",
            "assert_file": tfile,
            "why": why[:400],
            # ★ 2026-11-15：theory 原来只拼进 detail 长文本，动作对象上
            #   没有这个字段 —— 实测 38 条 feature 动作全部查不到依据，
            #   想统计「有几条创新真有论文支撑」只能靠正则去抠 detail。
            #   现在独立存一份（不截断到 900），前端可单独展示/筛选。
            "theory": theory[:600],
            # 只认 counts（= strong/weak）。unverified（编造）、none（不相关）、
            # disclaimed（模型自称非直接依据）都不算 —— 这正是用户要的区分。
            "has_paper": bool(_rel.get("counts")),
            "paper_rel": _rel,
            "status": "pending",
            "note": "",
            "error": "",
            "backup": "",
        })
    return out


def _plan_notice(plan):
    """把这次反思压成一条**给用户看的**摘要（聊天区推送用）。

    用户要求：「做完告诉我改了什么、为什么」+「两者都要：聊天区推一条摘要，
    详情在抽屉里」。所以这里只挑「为什么」和「要改什么」，不堆细节。
    """
    p = plan or {}
    acts = p.get("actions") or []
    feats = p.get("features") or []
    fc = p.get("forecast") or []
    needs = p.get("needs") or []
    qs = p.get("questions") or []
    lines = ["🌱 反思完成：%s" % (p.get("summary") or "（无摘要）")]
    if fc:
        lines.append("\n【我猜你接下来会做】")
        for x in fc[:3]:
            if isinstance(x, dict):
                lines.append("· %s（把握 %s）—— 依据：%s" %
                             (x.get("biz") or "", x.get("confidence") or "?",
                              str(x.get("evidence") or "")[:80]))
    if needs:
        lines.append("\n【你当下卡在哪】")
        for x in needs[:3]:
            if isinstance(x, dict):
                lines.append("· %s" % (x.get("pain") or ""))
    if feats:
        lines.append("\n【我想加的新功能（以及为什么）】")
        for x in feats[:4]:
            if isinstance(x, dict):
                lines.append("· %s —— %s" % (x.get("title") or "",
                                             str(x.get("why") or "")[:100]))
    if acts:
        lines.append("\n【本轮要改的代码 %d 条】" % len(acts))
        for a in acts[:4]:
            if isinstance(a, dict):
                lines.append("· %s%s" % (a.get("title") or "",
                                         "（高风险）" if a.get("risk") == "high" else ""))
    if qs:
        lines.append("\n【想问你 %d 个问题】在「AI 迭代想法」抽屉里 —— "
                     "答案会改变我下一步怎么做。" % len(qs))
    return "\n".join(lines)[:1800]


def _actions_from_findings(findings, limit=6):
    """把静态检查发现按文件聚合成「可执行的动作」。

    聚合而不是一条一个动作：一个文件里同类的 5 处问题，一次改完比改 5 次
    安全得多（每次改都要备份 + 校验 + 回滚，来回 5 次更容易撞车）。
    """
    groups = {}
    for f in findings or []:
        if f.get("level") not in ("high", "mid"):
            continue
        # 双保险：带复核结果且不是 verified 的，绝不允许变成可执行动作
        v = (f.get("verify") or {}).get("verdict")
        if v and v != "verified":
            continue
        groups.setdefault(f.get("file") or "", []).append(f)
    out = []
    for rel, items in groups.items():
        if len(out) >= limit:
            break
        if not _norm_rel(rel):
            continue
        lines = []
        for it in items[:6]:
            lines.append("第 %d 行 [%s] %s%s"
                         % (it.get("line") or 0, it.get("kind") or "",
                            it.get("msg") or "",
                            ("　→ " + it["hint"]) if it.get("hint") else ""))
        top = "high" if any(i.get("level") == "high" for i in items) else "mid"
        out.append({
            "title": "修 %s 里的 %d 处问题" % (rel, len(items)),
            "detail": "\n".join(lines),
            "risk": "mid" if top == "high" else "low",
            "files": [rel],
            # 结构化记录「这条动作到底针对哪几处问题」。
            # 用户要求「确保原来没问题的程序不因模型误判被反复修改」——
            # 执行前要靠它精确判断问题是否还在（按文件判断太粗：
            # 文件里只要还有别的问题就放行，会改错地方）。
            # ⚠️ 上限给到 20（原来是 6）：实测一个文件 9 处问题时，第 7 条以后的
            # kind 没被记进 targets，自动收敛就看不到那几类残留（会误判「已收敛」）。
            "targets": [{"file": rel, "kind": i.get("kind"),
                         "line": i.get("line") or 0} for i in items[:20]],
        })
    return out


def _llm_deep_review(audit_res):
    """让 LLM 在静态检查结果里挑出真该修的，并给出最小修法。

    ⚠️ 刻意**不**把整个代码库塞进去：token 太贵，而且模型拿到一大坨代码
    容易越改越乱。它的任务只有一个——在已知问题里判断「哪些真会出事」。
    """
    from . import code_audit as CA
    items = (audit_res or {}).get("findings") or []
    if not items:
        return [], "静态检查没发现问题，无需深度审查"
    brief = CA.for_llm(audit_res, limit=40)
    prompt = """你是这个项目的维护者。下面是对代码做静态检查的结果。请判断**哪些是真该修的**，并给出最小修法。

%s

## 你可以改的文件（files 只能从下面这个清单里选，不许编文件名）
%s

## 判断原则
1. 只挑「会真的导致功能失效 / 崩溃 / 静默错误」的；为了整洁好看而重构的一律不挑。
2. 每条给出最小改动方案：改哪个文件、哪一段、改成什么。
3. 拿不准的宁可标 low，不要瞎猜。
4. 最多 5 条，宁少勿滥。

## 输出（只输出 JSON，不要解释）
{"actions":[{"title":"...","detail":"...","risk":"low|mid|high","files":["backend/xxx.py"]}],
 "summary":"一句话总结"}
""" % (brief, _catalog_text())
    txt, err = _llm_text(prompt, max_tokens=2600, temperature=0.3)
    if err:
        return [], err
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return [], "模型没给出合法 JSON"
    out = []
    for a in data.get("actions") or []:
        if not isinstance(a, dict) or not str(a.get("title") or "").strip():
            continue
        files = [_norm_rel(f) for f in (a.get("files") or []) if isinstance(f, str)]
        files = _keep_real_files([f for f in files if f])
        if not files:
            continue                      # 指向不存在的文件 → 这条没法执行，直接丢
        out.append({"title": str(a["title"])[:120],
                    "detail": str(a.get("detail") or "")[:900],
                    "risk": str(a.get("risk") or "mid").lower(),
                    "files": files[:_MAX_FILES_PER_ACTION]})
    return out, str(data.get("summary") or "")


_AUDIT_LOCK = threading.Lock()
_AUDIT_LAST = {}          # source -> 上次触发的时间戳


def run_audit(deep=False, source="manual", create_plan=True, auto_fix=None, verify=None):
    """自检入口：防重入 + 同源冷却，防止「反复建计划、反复改代码」。

    ⚠️ 2026-11-15 实测事故：一个卡死的测试脚本每分钟调一次自检，配合信任模式
    连续建了 3 个计划并自动改代码 —— 用户看到的就是「程序被反复修改」。
    所以这里必须：
      ① 同一时刻只允许一个自检在跑（并发会让 plans.json 读改写互相覆盖）；
      ② 同一来源短时间内不重复触发。
    manual（用户点的）/ after_change（改完自动跑的）豁免冷却。
    """
    now = time.time()
    cool = float(load_settings().get("audit_cooldown_sec", 60) or 0)
    if cool > 0 and source not in ("manual", "after_change"):
        last = _AUDIT_LAST.get(source)
        if last and (now - last) < cool:
            return {"ok": True, "skipped": True, "findings": [], "plan": None,
                    "confirmed": 0, "rejected_n": 0, "unsure_n": 0,
                    "error": "同一来源 %d 秒内已跑过自检，本次跳过（防重复建计划）"
                             % int(cool)}
    if not _AUDIT_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": True, "findings": [], "plan": None,
                "confirmed": 0, "rejected_n": 0, "unsure_n": 0,
                "error": "已有一次自检正在运行，本次跳过（防并发重复建计划）"}
    try:
        _AUDIT_LAST[source] = now
        return _run_audit_impl(deep=deep, source=source, create_plan=create_plan,
                               auto_fix=auto_fix, verify=verify)
    finally:
        _AUDIT_LOCK.release()


def _run_audit_impl(deep=False, source="manual", create_plan=True, auto_fix=None,
                    verify=None):
    """跑一次代码自检。

    deep=True 时再让 LLM 审一遍（费 token，手动点才用）。
    create_plan=True 且发现 high/mid 问题时，自动建一个待执行计划。
    auto_fix=True（信任模式）→ 建完计划直接执行。

    ⚠️ 2026-11-15 用户要求「修之前一定要再次或多次检验，防止误诊乱改」：
    自检发现的问题**必须先过复核层**（backend/reverify.py），只有判定为
    verified 的才允许变成可执行动作；rejected 直接丢弃，inconclusive
    保留在计划里但**不进待办**，只给人看。
    """
    from . import code_audit as CA
    try:
        res = CA.audit()
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "自检器崩了：%s: %s" % (type(e).__name__, e)}

    findings = res.get("findings") or []

    # ---------------- 行为回归（补上静态检查的盲区）
    # 静态检查只看名字/结构；off-by-one、条件写反、返回值错位这类逻辑 bug
    # 它永远查不出来（实测 6 类逻辑 bug 报 0 条）。所以每次自检都真跑一遍断言。
    # 约 1.5~2 秒（子进程读磁盘，验的是最新代码）。
    rg = {}
    try:
        from . import regress as RG
        rg = RG.run_disk(scope="quick")
    except Exception as e:                              # noqa: BLE001
        logger.warning("行为回归跑不起来", exc_info=True)
        rg = {"ok": False, "summary": "回归跑不起来：%s: %s" % (type(e).__name__, e),
              "failed_items": []}
    if rg and not rg.get("ok"):
        res["logic_regress"] = rg

    # ---------------- 复核（多轮换方法确认，防误诊）
    vstats = {}
    if verify is None:
        verify = True
    if verify:
        try:
            from . import reverify as RV
            # 要自动改代码时**必须**做语义复核（最贵但最能防误改）
            use_llm = bool(deep or auto_fix)
            findings, vstats = RV.verify_findings(findings, use_llm=use_llm)
        except Exception as e:                          # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
            vstats = {"error": "%s: %s" % (type(e).__name__, e)}
            for f in findings:
                f["verify"] = {"verdict": "inconclusive", "rounds": [],
                               "why": "复核层自己出错了：%s" % e}
        res["findings"] = findings

    confirmed = [f for f in findings
                 if (f.get("verify") or {}).get("verdict") == "verified"]
    rejected = [f for f in findings
                if (f.get("verify") or {}).get("verdict") == "rejected"]
    unsure = [f for f in findings
              if (f.get("verify") or {}).get("verdict") == "inconclusive"]

    actions, llm_summary = [], ""
    if deep:
        actions, llm_summary = _llm_deep_review({**res, "findings": confirmed})
    if not actions:
        actions = _actions_from_findings(confirmed)

    def _brief(items, n=12):
        return [{"file": f.get("file"), "line": f.get("line"),
                 "kind": f.get("kind"),
                 "why": str((f.get("verify") or {}).get("why") or "")[:160]}
                for f in items[:n]]

    plan = None
    if create_plan and actions:
        cnt = res.get("counts") or {}
        plan = _make_plan(
            trigger="audit:" + str(source),
            summary=("代码自检：%d 处问题 → 复核确认 %d 处（排除误报 %d / 存疑 %d）"
                     "→ 整理成 %d 条待办"
                     % (res.get("total") or 0, len(confirmed),
                        len(rejected), len(unsure), len(actions))),
            actions=actions,
            extra={"audit_counts": cnt, "verify": vstats,
                   "rejected": _brief(rejected), "unsure": _brief(unsure)})

    rec = {"ts": res.get("ts"), "source": source, "_ts": time.time(),
           "files": res.get("files"), "js_files": res.get("js_files"),
           "total": res.get("total"), "counts": res.get("counts"),
           "kinds": res.get("kinds"), "seconds": res.get("seconds"),
           "findings": (res.get("findings") or [])[:60],
           "plan": (plan or {}).get("id") or "",
           "verify": vstats,
           "regress": {k: rg.get(k) for k in
                       ("ok", "total", "passed", "failed", "skipped",
                        "seconds", "summary", "failed_items")} if rg else {},
           "llm_summary": llm_summary}
    try:
        _save_audit(rec)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    out = {"ok": True,
           "audit": {k: res.get(k) for k in
                     ("ts", "files", "js_files", "seconds", "counts", "kinds", "total")},
           "findings": res.get("findings") or [],
           "regress": {k: rg.get(k) for k in
                       ("ok", "total", "passed", "failed", "skipped",
                        "seconds", "summary", "failed_items")} if rg else {},
           "verify": vstats,
           "confirmed": len(confirmed), "rejected_n": len(rejected),
           "unsure_n": len(unsure),
           "rejected": _brief(rejected), "unsure": _brief(unsure),
           "plan": plan, "llm_summary": llm_summary}

    if plan and auto_fix:
        out["auto"] = auto_approve_and_execute(plan["id"])
        out["need_restart"] = bool((out.get("auto") or {}).get("need_restart"))
    return out


def audit_overview():
    """给前端：最近几次自检的摘要 + 最近一次的 findings。"""
    d = _audit_history()
    items = d.get("items") or []
    return {"ok": True,
            "settings": load_settings(),
            "latest": items[0] if items else None,
            "history": [{"ts": i.get("ts"), "source": i.get("source"),
                         "total": i.get("total"), "counts": i.get("counts"),
                         "seconds": i.get("seconds")} for i in items[:10]]}


# ---------------------------------------------------------------- 信任模式

def auto_approve_only(pid, max_n=0):
    """信任模式：把待批准的动作全部标成 approved，**不执行**。

    用户明确要求「全部自动，包括高风险；高风险也自动进行，但会在前端跟用户
    说一声」—— 所以这里不按风险过滤，只把高风险挑出来放进返回值，让前端提示。

    2026-11-15 拆出来：执行改由 iter_runner 的队列负责（要排队、要能终止、
    要在聊天界面出代码卡片），这里只负责「批准」这一步。
    """
    s = load_settings()
    d, plan = _find_plan(pid)
    if not plan:
        return {"ok": False, "error": "找不到这次计划"}
    cap = int(max_n or s.get("max_auto_actions") or 10)
    # ★ 2026-11-16（用户要求「把成功率可能下降这点彻底解决」）：
    #   放开创新后成功率会波动，真实数据 68~70%（剔除验证用例）。失败原因不是
    #   「想法太大胆」，而是「一次改太多、每条缺可验证断言」→ 难以一次改对。
    #   所以这里**不停止创新**，而是成功率过低时自动把单次粒度改小：
    #   一次只动 4 条，每条更容易通过语法/回归/断言三重校验。
    #
    # ⚠️ 实测踩过的坑：这里写成 `cap = min(cap, 4)` 是**无效的** —— 下面
    #   `cap = max(cap, len(pend))` 会把它拉回 10（验证时 rate=45% 仍批准 10 条）。
    #   所以单独用一个 ceiling，最后统一 min 一次，任何 max() 都突破不了。
    # ⚠️ 只在「真的降级」时设 ceiling。若一开始就设成 cap，正常情况会被
    #   `cap = max(cap, len(pend))` 顶回 10 之后再被 min 压回 6（settings 里的旧值），
    #   等于把「不超过硬上限 10 就不截断」这条规则废掉 —— 实测 10 条只批了 6 条。
    _cap_ceiling = None
    try:
        _m = runtime_metrics()
        if ((_m.get("total") or 0) >= 12 and _m.get("rate") is not None
                and _m["rate"] < 50):
            _cap_ceiling = 4
            cap = min(cap, _cap_ceiling)
            logger.info("成功率 %.1f%% 偏低 → 本轮动作上限自动降到 %d",
                        _m["rate"], _cap_ceiling)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    pend = [a for a in (plan.get("actions") or []) if a.get("status") == "pending"]
    # 用户要求「全部自动做」。设置文件里可能残留旧默认值 6（老版本留下的），
    # 实测它会把排在后面的功能动作整批切掉（4 条功能只进 1 条）。所以本轮
    # 动作数不超过硬上限 10 时就不截断；超过才按配额截。
    _HARD_CAP = 10
    if len(pend) <= _HARD_CAP:
        cap = max(cap, len(pend))
    else:
        cap = min(cap, _HARD_CAP)
    if _cap_ceiling is not None:      # ← 保证成功率降级不被上面的 max() 覆盖
        cap = min(cap, _cap_ceiling)
    # 用户要求「新功能也要自动落地」。但 bug 修复动作排在数组前面，会先占满名额，
    # 把功能动作整批挤掉 —— 实测 cap=6 时 4 条功能动作只进了 1 条。所以给功能
    # 留保底配额（约 cap 的 1/3），剩下的名额再按原顺序给 bug 修复。
    feat = [a for a in pend if a.get("origin") == "feature"]
    quota = min(len(feat), max(1, cap // 3)) if feat else 0
    picked = []
    for a in feat[:quota]:
        a["status"] = "approved"
        a["auto"] = True
        picked.append(a)
    for a in pend:
        if len(picked) >= cap:
            break
        if a.get("status") != "pending":
            continue
        a["status"] = "approved"
        a["auto"] = True
        picked.append(a)
    save_plans(d)
    return {"ok": bool(picked), "approved": len(picked),
            "plan": plan,
            "high_risk": [{"id": a.get("id"), "title": a.get("title")}
                          for a in picked if str(a.get("risk") or "mid") == "high"]}


def auto_approve_and_execute(pid, max_n=0):
    """信任模式：批准 + 立刻执行（同步，兼容老调用方）。

    ⚠️ 新流程（2026-11-15 起）不再走这里 —— 用户要求迭代要在聊天界面里跑、
    要排队、要能终止，所以改由 iter_runner.submit() 提交到队列。
    这个函数留着是为了兼容可能还在直接调它的地方，行为不变。
    """
    s = load_settings()
    if not s.get("trust_mode"):
        return {"ok": False, "error": "没开信任模式，不能免批准执行"}
    pre = auto_approve_only(pid, max_n=max_n)
    if not pre.get("ok"):
        return {"ok": False, "error": pre.get("error") or "没有待执行的动作（可能已经处理过了）"}
    r = execute_approved(pid)
    r["auto_approved"] = pre.get("approved")
    r["trust"] = True
    r["high_risk"] = pre.get("high_risk") or []
    return r


# ---------------------------------------------------------------- 任务不达标诊断

def diagnose(task, expected="", actual="", evidence="", auto_fix=None,
             create_plan=True):
    """任务结果没达到预设效果时，判断「是不是代码有问题」。

    用户要求（2026-11-15）：「当在做任务时完成结果没有达到预设效果时，
    也能够让模型思考是不是代码有问题，从而去主动寻找发现解决 bug 和漏洞
    以及错误」。

    这条和自检的区别：自检是「代码自己看自己」，诊断是「从结果反推代码」——
    后者能抓到静态检查永远看不出来的问题（比如参数被忽略、分支没走到）。
    """
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "error": "要告诉我「做的是什么任务」"}
    prompt = """用户交给我一个任务，结果没达到预期。请判断**是不是代码的问题**，以及问题在哪。

## 任务
%s

## 预期效果
%s

## 实际结果
%s

## 证据（日志 / 数值 / 现象）
%s

## 项目里真实存在的文件（suspects.file 和 actions.files 只能从这里选，不许编）
%s

## 你要判断的
1. 这更可能是**代码 bug**、还是参数/数据/环境/需求理解的问题？给出你的置信度。
2. 如果是代码问题：最可能出问题的文件 + 函数 + 原因（越具体越好）。
3. 怎么用最小的验证确认你的判断（说清看什么数值 / 跑什么）。
4. 要改的话，给出最小修复动作（改哪个文件、改成什么）。

## 输出（只输出 JSON，不要解释）
{"is_code_issue": true/false,
 "confidence": 0.0~1.0,
 "reason": "一句话结论",
 "suspects": [{"file":"backend/xxx.py","func":"函数名","why":"为什么怀疑这里"}],
 "checks": ["验证方法1","验证方法2"],
 "actions": [{"title":"...","detail":"...","risk":"low|mid|high","files":["backend/xxx.py"]}],
 "summary":"给用户看的一段话"}
""" % (task, expected or "（未说明）", actual or "（未说明）",
       evidence or "（无）", _catalog_text())

    txt, err = _llm_text(prompt, max_tokens=2600, temperature=0.3)
    if err:
        return {"ok": False, "error": err}
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型没给出合法 JSON", "raw": (txt or "")[:500]}

    actions = []
    for a in data.get("actions") or []:
        if not isinstance(a, dict) or not str(a.get("title") or "").strip():
            continue
        files = [_norm_rel(f) for f in (a.get("files") or []) if isinstance(f, str)]
        files = _keep_real_files([f for f in files if f])
        if not files:
            continue                      # 指向不存在的文件 → 没法执行，丢掉
        actions.append({"title": str(a["title"])[:120],
                        "detail": str(a.get("detail") or "")[:900],
                        "risk": str(a.get("risk") or "mid").lower(),
                        "files": files[:_MAX_FILES_PER_ACTION]})

    real = set(_file_catalog())
    suspects = [s for s in (data.get("suspects") or [])
                if isinstance(s, dict) and _norm_rel(s.get("file") or "") in real]

    plan = None
    if create_plan and actions:
        plan = _make_plan(
            trigger="diagnose",
            summary=str(data.get("summary") or data.get("reason") or "")[:200],
            actions=actions,
            extra={"diagnose": {"task": task[:300], "expected": expected[:300],
                                "actual": actual[:300],
                                "is_code_issue": bool(data.get("is_code_issue")),
                                "confidence": data.get("confidence"),
                                "suspects": suspects,
                                "checks": data.get("checks") or []}})

    out = {"ok": True,
           "is_code_issue": bool(data.get("is_code_issue")),
           "confidence": data.get("confidence"),
           "reason": str(data.get("reason") or ""),
           "suspects": suspects,
           "checks": data.get("checks") or [],
           "summary": str(data.get("summary") or ""),
           "plan": plan}
    if plan and auto_fix:
        out["auto"] = auto_approve_and_execute(plan["id"])
        out["need_restart"] = bool((out.get("auto") or {}).get("need_restart"))
    return out


# ---------------------------------------------------------------- 汇总

def stats():
    d = load_plans()
    proposed = approved = done = rejected = failed = 0
    for p in d["items"]:
        for a in p.get("actions") or []:
            proposed += 1
            st = a.get("status")
            if st == "approved":
                approved += 1
            elif st == "done":
                done += 1
                approved += 1
            elif st == "rejected":
                rejected += 1
            elif st == "failed":
                failed += 1
    rate = round(done / proposed * 100.0, 1) if proposed else 0.0
    return {"proposed": proposed, "approved": approved, "done": done,
            "rejected": rejected, "failed": failed, "rate": rate,
            "plans": len(d["items"])}


def overview():
    """前端抽屉一次拿全：最新一次反思、待批准数、采纳率、信号数。"""
    d = load_plans()
    items = d.get("items") or []
    latest = items[0] if items else None
    pending = 0
    approved = 0
    for p in items:
        for a in p.get("actions") or []:
            if a.get("status") == "pending":
                pending += 1
            elif a.get("status") == "approved":
                approved += 1
    sig = load_signals().get("items", [])
    try:
        _last_audit = (audit_overview().get("latest") or None)
    except Exception:                                   # noqa: BLE001
        _last_audit = None
    if latest:
        fill_fail_kind(latest)
    return {
        "ok": True,
        "latest": latest,
        "pending": pending,
        "approved_waiting": approved,
        "signals": len(sig),
        "settings": load_settings(),
        "audit": _last_audit,
        "stats": stats(),
        "history": [{"id": p.get("id"), "ts": p.get("ts"), "summary": p.get("summary"),
                     "trigger": p.get("trigger"),
                     "actions": len(p.get("actions") or [])} for p in items[:12]],
    }


def fill_fail_kind(p):
    """给历史失败动作补上 fail_kind（按 error 文本现算）。

    2026-11-15 用户问「解决了吗」时发现：`fail_kind` 只在**新执行**的动作上
    落盘，而库里那 11 条历史失败一个都没有。前端读的是 `a.fail_kind || "other"`，
    于是它们全被归成灰色「其他」—— 护栏拦下的和真故障还是分不开，
    等于这个功能对历史数据完全没生效。

    这里在**出口**统一补齐：历史记录、以及任何没走 execute_approved 的
    写入路径，都能得到正确分类。不改磁盘上的旧数据（避免动到审计痕迹）。
    """
    if not isinstance(p, dict):
        return p
    for a in (p.get("actions") or []):
        if not isinstance(a, dict):
            continue
        if a.get("status") != "failed":
            continue
        if not a.get("fail_kind"):
            a["fail_kind"] = classify_fail(a.get("error") or a.get("detail") or "")
    return p


def get_plan(pid):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    return {"ok": True, "plan": fill_fail_kind(p)}
