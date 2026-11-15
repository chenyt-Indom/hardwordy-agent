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
    # 信任模式：反思产出的动作**不再等批准**，直接执行。
    # 用户明确选了「全部自动，包括高风险（改 main.py、一次改多个文件），
    # 但高风险会单独在前端提示一声」。
    "trust_mode": False,
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
            "trust_mode": bool(s.get("trust_mode"))}


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


def load_plans():
    d = _read_json(_PLANS, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
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


def _log_act(plan, act, ok, detail=""):
    """记录一条动作执行日志（用户可查「到底改没改」）。"""
    _actlog({"ts": _now(),
             "plan_id": (plan or {}).get("id"),
             "action": (act or {}).get("id"),
             "title": str((act or {}).get("title") or "")[:90],
             "files": (act or {}).get("files") or [],
             "result": "ok" if ok else "fail",
             "detail": str(detail or "")[:500],
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
_LOOP_MAX_HITS = 3          # 窗口内同一文件被自动改成功的上限


def _loop_guard(files):
    """反复修改熔断。返回 (allow, why)。读不到日志时放行（不误伤）。"""
    files = [f for f in (files or []) if f]
    if not files:
        return True, ""
    try:
        rows = list_actlog(limit=200)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return True, ""
    now = time.time()
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
                hit[f] = hit.get(f, 0) + 1
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
           "by_file": []}
    try:
        logs = list_actlog(300)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return out
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
            real.append("⚠️ 成功率偏低（<70%），说明我的改动经常失败/被回滚 —— "
                        "**应该优先提高改动的可靠性，而不是加新功能**。")
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
             "请针对每一条摩擦点给出**具体到环节**的改法，"
             "优先砍步骤、减少返工，而不是加新功能。" %
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

def _llm_text(prompt, max_tokens=4200, temperature=0.55):
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
    try:
        buf = []
        for kind, txt in chat_stream(
                [{"role": "user", "content": prompt}],
                "deepseek-flash", key, temperature=temperature,
                max_tokens=max_tokens, thinking=False):
            if kind == "content" and txt:
                buf.append(txt)
        return "".join(buf).strip(), None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    """LLM 常把 JSON 包在 ```json 里，或前后带解释文字。这里尽量抠出来。"""
    if not txt:
        return None
    s = txt.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:                               # noqa: BLE001
            return None
    return None


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
    out = []
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
        out.extend("%s/%s" % (sub, n) for n in names)
    if not out:
        return ""
    shown = out[:limit]
    tail = "" if len(out) <= limit else "（另有 %d 个未列出）" % (len(out) - limit)
    return ("\n\n## 现有文件清单（**只能从这里挑**，不要编造不存在的文件名）\n"
            + "、".join(shown) + tail)


_PROJECT_BRIEF = """本项目是一个"本地多模态助手"（Python FastAPI 后端 + 原生 JS 前端）：
· 对话/工具调用（联网搜索、读网页、地图、天气、图片生成/微改、文档 PPT Excel 生成、视频剪辑、语音合成、数字人）
· 知识库检索、长期/短期记忆、技能（skill）系统、定时任务、自开发（能改自己的源码）
· 数字人模块：warp 形变口型（纯 CPU、6ms/帧）、闭嘴基准合成、SDXL 局部重绘
· 已有的自我改进通道：selfdev_patch / selfdev_write / selfdev_restart
前端文件：frontend/index.html、app.js(约1.2万行)、style.css
后端文件：backend/*.py（main.py 约1.4万行、tools.py、avatar_*.py、cloud_image.py、keyvault.py 等）"""


def _build_reflect_prompt(signals, tech, journal, focus="", exp="", paper_brief=""):
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
⚠️ 这里的 verdict=skip 就是**明确不该做**，别把它再包装成 action 提一遍。
   verdict=adopt_now / evaluate 的，它的 plan 已经是分步骤的，你要做的是判断
   「现在值不值得插进本轮动作」——值得就把它转成 actions 里的条目（保留它的
   验证方式），不值得就在 summary 里说一句为什么先不做。

## 你的任务
1. **自我反思**：最近哪些地方没做好？要具体到"哪类任务/哪个环节/为什么会这样"，
   不要写"我会更努力"这种空话。优先找**反复出现**的问题。
2. **对用户的观察**：从对话里看出用户的工作习惯、反复提出的诉求、被浪费的时间。
   只写有证据的，没证据就不要编。
3. **消除体验摩擦**（优先级**高于**加新功能）：上面「用户体验评估」里反复出现的
   摩擦点，必须给出具体到环节的消除办法。过程繁琐就去砍步骤，评分低就去查
   是不是我的做法本身有问题——**不要写"我会更仔细"这种空话**。
4. **预判用户接下来要做什么**（新增，很重要）：从对话里推断用户**未来可能处理
   什么业务**、**当下已经卡在哪**。只写有证据的，每条必须带证据（用户原话/反复
   出现的模式）和把握程度（high/mid/low）。没证据就不要编 —— 编出来的预判比没有更糟。
5. **可增加的功能 / 新技术**：结合技术动态、论文评估和上面的预判，提出真正值得做的。
   ⚠️ **允许提「技术还没成熟、但理论上可行」的方案**（借鉴论文即可），只要对用户
   真有用。用户明确说过：哪怕这项技术还没出来，只要有用且理论可行就可以做。
   ⚠️ 但**必须说清落在哪**：改哪些文件（files，只能从上面的清单里选）、改哪个函数
   （anchor）。说不清 files/anchor 的建议不算有效 —— 那说明你自己也没想清怎么落地。
   ⚠️ 如果上面「真实运行指标」显示成功率偏低，**先提高可靠性，别急着加新功能**。
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
   ⚠️ 不要提议"重写整个模块"这种无法安全回滚的动作。
   ⚠️ 如果确实没有值得改的，actions 就给空数组 —— 不要为了凑数硬提。

## 输出格式（严格 JSON，不要任何额外文字）
{
  "summary": "一句话总结这次反思（30字内）",
  "weaknesses": [{"title":"短标题","detail":"具体问题+证据","severity":"high|mid|low"}],
  "user_insights": [{"title":"短标题","detail":"观察+证据","suggestion":"可以怎么改进"}],
  "forecast": [{"biz":"用户未来可能处理的业务","evidence":"依据（原话/重复出现的模式）","confidence":"high|mid|low","prepare":"可以提前准备什么"}],
  "needs": [{"pain":"当下遇到的问题","evidence":"证据","impact":"影响了什么"}],
  "features": [{"title":"功能名","why":"为什么值得做（必须说清）","pain":"对应哪个痛点或预判","how":"大概怎么做","effort":"S|M|L","files":["backend/xxx.py"],"anchor":"改哪个函数","risk":"low|mid|high","verify":"怎么验证有效","theory":"理论/论文依据，没有就留空"}],
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
        ("## 用户这次的额外关注点\n" + focus) if focus else "")


def reflect(trigger="manual", use_web=True, focus="", auto_execute=None):
    """跑一次完整反思（带运行状态包装）。

    auto_execute=None → 看设置里的 trust_mode（信任模式开着就自动执行）；
    auto_execute=True/False → 强制指定（前端手动触发时用）。

    包装的目的：内部实现有多条 return（LLM 报错、JSON 非法、正常完成），
    每条都要把 running 置回 False，否则前端会一直转圈。
    """
    _run_begin(trigger)
    try:
        out = _reflect_impl(trigger=trigger, use_web=use_web,
                            focus=focus, auto_execute=auto_execute)
    except Exception as e:                              # noqa: BLE001
        _run_end(err="%s: %s" % (type(e).__name__, e))
        raise
    _run_end(err="" if out.get("ok") else str(out.get("error") or "未知错误"),
             plan_id=str(((out.get("plan") or {}).get("id")) or ""))
    return out


def _reflect_impl(trigger="manual", use_web=True, focus="", auto_execute=None):
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
        set_stage("paper", "新增入库 %d 篇，向量召回中…" % (paper_meta.get("added") or 0))
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

    prompt = _build_reflect_prompt(sig, tech, journal, focus=focus,
                                   exp=exp_txt, paper_brief=paper_brief)
    # 真实文件清单：不给它，模型会编出 `avatar_warp.py` 这种不存在的路径
    try:
        prompt += _file_inventory()
    except Exception:
        logger.debug("silent-except", exc_info=True)
    set_stage("think", "把 %d 轮对话 + %d 条技术动态喂给模型…"
              % (len(sig), tech_total or 0))
    txt, err = _llm_text(prompt, max_tokens=4200, temperature=0.55)
    if err:
        return {"ok": False, "error": err}
    set_stage("plan", "模型已返回，整理动作清单…")

    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型返回的不是合法 JSON",
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

    out = {"ok": True, "plan": plan, "notice": plan["notice"]}
    # 信任模式：用户要求「不用批准，自动迭代，改完提示重启」。
    # 只在真的有计划动作时才执行，避免空跑。
    if auto_execute is None:
        auto_execute = bool(load_settings().get("trust_mode"))
    if auto_execute and actions:
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
        if a["status"] in ("pending", "rejected"):
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


# ---------------------------------------------------------------- 执行（带备份/校验/回滚）

def _norm_rel(rel):
    """规范化并校验路径；不合法返回空串。"""
    if not isinstance(rel, str):
        return ""
    s = rel.replace("\\", "/").strip().lstrip("./")
    low = s.lower()
    for bad in _FORBIDDEN_PARTS:
        if low.startswith(bad) or ("/" + bad) in ("/" + low):
            pass
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


def _apply_find_replace(rel, find, replace):
    """精确替换（必须恰好命中 1 处），与 selfdev_patch 同一套语义。

    特例：find 为空且目标文件不存在 → 视为**新建文件**，直接写入 replace。
    （"建一个回归脚本"这类动作必须支持，否则永远执行不了。）
    """
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
    ctx = []
    for rel in files[:_MAX_FILES_PER_ACTION]:
        body = _read_text(rel)
        if len(body) > 14000:
            body = body[:14000] + "\n…（后略，文件过长，请只针对你确定存在的片段给 find）"
        ctx.append("### 文件：%s\n```\n%s\n```" % (rel, body))
    for rel in new_files[:_MAX_FILES_PER_ACTION]:
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
3. 改动要**最小化**，不要顺手重构无关代码，不要删掉现有功能。
4. 如果发现无法安全实施（比如需求与代码不符），就输出 {"skip":"原因"}。

## 输出格式
{"patches":[{"file":"backend/xxx.py","find":"原文片段","replace":"替换后片段"}]}
""" % (action.get("title"), action.get("detail"),
       action.get("note") or "（无）", "\n\n".join(ctx))

    txt, err = _llm_text(prompt, max_tokens=3600, temperature=0.2)
    if err:
        return None, err
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return None, "模型没给出合法 JSON"
    if data.get("skip"):
        return None, "模型判断无法安全实施：%s" % str(data.get("skip"))[:200]
    patches = data.get("patches") or []
    if not isinstance(patches, list) or not patches:
        return None, "模型没有给出任何 patch"
    return patches, ""


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


def execute_approved(pid, on_event=None, should_cancel=None, _depth=0):
    """执行这次反思里所有 status=approved 的动作。带备份 + 校验 + 失败回滚。

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
        _lk, _lwhy = _loop_guard(a.get("files"))
        if not _lk:
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
        if len(set(rels)) > _MAX_FILES_PER_ACTION:
            a["status"] = "failed"
            a["error"] = "一次动作涉及文件过多（上限 %d）" % _MAX_FILES_PER_ACTION
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
                if not _rr.get("ok"):
                    rbad = "行为回归失败：%s" % (_rr.get("summary") or "")
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

        a["status"] = "done"
        a["error"] = ""
        results.append({"id": a["id"], "ok": True, "files": list(dict.fromkeys(done_rels)),
                        "backup": tag2})
        _log_act(plan, a, True, "改了：" + "、".join(dict.fromkeys(done_rels)))
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


def _similar_title(a, b):
    """判断两条建议是不是在说同一件事（用于 actions/features 去重）。

    实测：模型会把同一个需求**同时**写进 actions 和 features（比如
    「验证类回复强制结论优先模板」和「验证类请求的结论优先回复模板」），
    转成动作后就是两条动作改同一处 —— 既浪费名额，又可能互相覆盖。
    """
    def _n(s):
        return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", str(s or ""))
    a, b = _n(a), _n(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True

    def _g(s):
        return set(s[i:i + 2] for i in range(max(0, len(s) - 1)))
    ga, gb = _g(a), _g(b)
    if not ga or not gb:
        return False
    return len(ga & gb) / float(len(ga | gb)) >= 0.45


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
        files = []
        for x in (f.get("files") or []):
            if not isinstance(x, str):
                continue
            n = _norm_rel(x)
            if n and n in exist and n not in files:
                files.append(n)
        why = str(f.get("why") or "").strip()
        how = str(f.get("how") or "").strip()
        anchor = str(f.get("anchor") or "").strip()
        theory = str(f.get("theory") or "").strip()
        verify = str(f.get("verify") or "").strip()
        pain = str(f.get("pain") or "").strip()
        risk = str(f.get("risk") or "mid").lower()
        if risk not in ("low", "mid", "high"):
            risk = "mid"
        parts = []
        if pain:
            parts.append("对应痛点：%s" % pain)
        if why:
            parts.append("为什么值得做：%s" % why)
        if how:
            parts.append("怎么做：%s" % how)
        if anchor:
            parts.append("改哪里：%s" % anchor)
        if theory:
            parts.append("理论/论文依据：%s" % theory)
        if verify:
            parts.append("怎么验证：%s" % verify)
        if not files:
            parts.append("⚠️ 没给出可改的真实文件，执行时只能靠模型自己找位置。")
        out.append({
            "id": "f%d" % (start + len(out) + 1),
            "title": title[:120],
            "detail": "\n".join(parts)[:900],
            "risk": risk,
            "files": files[:_MAX_FILES_PER_ACTION],
            "origin": "feature",
            "why": why[:400],
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
    pend = [a for a in (plan.get("actions") or []) if a.get("status") == "pending"]
    # 用户要求「全部自动做」。设置文件里可能残留旧默认值 6（老版本留下的），
    # 实测它会把排在后面的功能动作整批切掉（4 条功能只进 1 条）。所以本轮
    # 动作数不超过硬上限 10 时就不截断；超过才按配额截。
    _HARD_CAP = 10
    if len(pend) <= _HARD_CAP:
        cap = max(cap, len(pend))
    else:
        cap = min(cap, _HARD_CAP)
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


def get_plan(pid):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    return {"ok": True, "plan": p}
