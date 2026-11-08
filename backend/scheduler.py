# -*- coding: utf-8 -*-
"""定时 / 长期自动化任务。

用户要求：「我还需要智能体能够实现长期自动化或定时化任务」。

设计取舍（重要，改之前先读）
--------------------------
1. **只在本地跑**。定時任务没有人在场，而 DeepSeek 的 Key 只存在浏览器
   localStorage 里（服务端从来不存）—— 半夜三点跑任务时没人给你送 Key。
   所以这里固定用**本机 Ollama 模型**。等哪天做了"服务端保管 Key"再说云端。

2. **无人值守 → 不做交互**。`ask`（问用户）直接返回空、`confirm`（危险操作）
   直接批准。理由：定时任务弹一个没人点的框 = 永久卡死，比"没问"更糟。
   风险由**用户创建任务时**承担：任务内容是他自己写的，模型只会去执行它。

3. **结果落到会话里**，不是只写日志。用户第二天打开应用，在对应会话里就能看到
   "【定时任务】昨晚跑了什么"。日志另存一份便于排查。

4. **不引入 cron 依赖**：只支持两种最简单的周期 ——
   `every_minutes`（每 N 分钟）和 `at_time`（每天 HH:MM）。
   够覆盖"每天早上九点总结"这类需求，也不会把调度复杂度带进来。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from . import config, sessions

_LOCK = threading.Lock()
_THREAD = None
_STATE = {"running": "", "last_tick": 0.0}


def _jobs_path() -> str:
    return config.data("data", "schedules.json")


def _logs_dir() -> str:
    d = config.data("data", "schedule_logs")
    os.makedirs(d, exist_ok=True)
    return d


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_jobs() -> list:
    try:
        with open(_jobs_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_jobs(jobs: list) -> None:
    p = _jobs_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 时间
def _parse_hhmm(s: str):
    try:
        h, m = str(s).replace("：", ":").split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def _next_run(job: dict, base: float = None) -> float:
    """算下一次该跑的时间戳（0 = 不排期）。"""
    base = time.time() if base is None else base
    sch = job.get("schedule") or {}
    if sch.get("every_minutes"):
        try:
            mins = max(1, int(sch["every_minutes"]))
        except Exception:
            return 0
        return base + mins * 60
    if sch.get("at_time"):
        hm = _parse_hhmm(sch["at_time"])
        if not hm:
            return 0
        t = time.localtime(base)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                              hm[0], hm[1], 0, 0, 0, -1))
        if target <= base:
            target += 86400
        return target
    return 0


def describe(job: dict) -> str:
    sch = job.get("schedule") or {}
    if sch.get("every_minutes"):
        return "每 %s 分钟" % sch["every_minutes"]
    if sch.get("at_time"):
        return "每天 %s" % sch["at_time"]
    return "未设置周期"


# ---------------------------------------------------------------- 执行
def _run_once(job: dict) -> dict:
    """真的跑一次：本地模型 + 工具循环，结果写回会话。"""
    from . import tools as T
    from . import main as M          # 延迟导入，避免循环依赖

    cfg = config.load_config() or {}
    model = str(job.get("model") or cfg.get("default_model") or "")
    prompt = str(job.get("prompt") or "").strip()
    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:
            sid = ""

    # 模型没下载就退回已装的
    try:
        have = M._installed_models()
        if have and model not in have:
            model = next((n for n in have if "qwen3-vl" in n), "") or have[0]
    except Exception:
        pass

    schemas = T.make_schemas(
        web_enabled=bool(cfg.get("web_enabled")),
        kb_enabled=bool(cfg.get("rag_enabled")),
        code_exec=bool(cfg.get("code_exec_enabled")),
        office_gen=True, ask_mode="quick")
    ctx = {"images": [], "last_image": None, "mask": None, "shown_images": [],
           "ask_mode": "quick", "session": sid, "user_text": prompt,
           "image_key": "", "ds_key": "",
           # 无人值守：危险操作自动批准、提问直接跳过（见文件头第 2 条）
           "confirm": lambda payload: True,
           "ask": lambda payload: []}

    msgs = [{"role": "user", "content": prompt}]
    answer = ""
    for _ in range(12):
        resp = M.client.chat(msgs, model=model, stream=False,
                             tools=schemas, params=dict(cfg))
        data = resp.json() if hasattr(resp, "json") else resp
        msg = data.get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = str(msg.get("content") or "")
            break
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})
        for c in calls:
            fn = (c or {}).get("function") or {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            try:
                out = T.dispatch(name, args or {}, [], ctx)
            except Exception as e:                    # noqa: BLE001
                out = "工具出错：%s: %s" % (type(e).__name__, e)
            msgs.append({"role": "tool", "content": str(out)[:8000]})
    if not answer:
        answer = "（这次没有产出正文，可能是轮次用尽或模型被截断）"

    # 写回会话（用户下次打开就能看到）
    try:
        old = sessions.get_messages(sid) or []
        stamp = _now()
        old.append({"role": "user", "ts": stamp,
                    "content": "【定时任务·%s】%s" % (job.get("name") or "未命名", prompt)})
        old.append({"role": "assistant", "ts": stamp, "content": answer})
        sessions.save_messages(sid, old)
    except Exception:
        pass

    # 日志留档
    try:
        p = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"), time.strftime("%Y%m%d_%H%M%S")))
        with open(p, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 模型：%s\n- 周期：%s\n\n## 任务\n\n%s\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(), model, describe(job), prompt, answer))
    except Exception:
        pass
    return {"answer": answer, "model": model, "session": sid}


def run_now(job_id: str) -> dict:
    """立即跑一次（前端"立即运行"按钮 / 测试用）。"""
    jobs = load_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    _STATE["running"] = job_id
    try:
        r = _run_once(job)
        job["last_run"] = _now()
        job["last_status"] = "ok"
        job["last_preview"] = (r.get("answer") or "")[:200]
        job["next_run"] = _next_run(job)
        save_jobs(jobs)
        return {"ok": True, **r}
    except Exception as e:                            # noqa: BLE001
        job["last_run"] = _now()
        job["last_status"] = "error: %s" % e
        save_jobs(jobs)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        _STATE["running"] = ""


# ---------------------------------------------------------------- 增删改查
def add(name: str, prompt: str, *, every_minutes: int = 0, at_time: str = "",
        session: str = "", model: str = "") -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "任务内容不能为空"}
    sch = {}
    if every_minutes:
        sch["every_minutes"] = int(every_minutes)
    elif at_time:
        if not _parse_hhmm(at_time):
            return {"ok": False, "error": "时间格式要像 09:00"}
        sch["at_time"] = at_time
    else:
        return {"ok": False, "error": "要么给 every_minutes（每几分钟），要么给 at_time（每天几点）"}
    job = {"id": uuid.uuid4().hex[:8],
           "name": (name or prompt[:20]).strip()[:40],
           "prompt": prompt, "schedule": sch,
           "session": session, "model": model,
           "enabled": True, "created": _now(),
           "last_run": "", "last_status": "", "last_preview": "",
           "next_run": _next_run({"schedule": sch})}
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)
    return {"ok": True, "job": job}


def update(job_id: str, **kw) -> dict:
    jobs = load_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    for k in ("name", "prompt", "enabled", "session", "model"):
        if k in kw and kw[k] is not None:
            job[k] = kw[k]
    if "enabled" in kw and kw["enabled"]:
        job["next_run"] = _next_run(job)
    save_jobs(jobs)
    return {"ok": True, "job": job}


def remove(job_id: str) -> dict:
    jobs = load_jobs()
    n = len(jobs)
    jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) == n:
        return {"ok": False, "error": "没有这个任务"}
    save_jobs(jobs)
    return {"ok": True}


def list_jobs() -> list:
    out = []
    for j in load_jobs():
        j = dict(j)
        nr = float(j.get("next_run") or 0)
        j["next_human"] = (time.strftime("%m-%d %H:%M", time.localtime(nr)) if nr else "-")
        j["period"] = describe(j)
        out.append(j)
    return out


# ---------------------------------------------------------------- 后台循环
def _loop() -> None:
    while True:
        try:
            jobs = load_jobs()
            due = []
            now = time.time()
            for j in jobs:
                if not j.get("enabled"):
                    continue
                nr = float(j.get("next_run") or 0)
                if nr and nr <= now:
                    due.append(j)
            for j in due:
                _STATE["running"] = j["id"]
                try:
                    _run_once(j)
                    j["last_status"] = "ok"
                except Exception as e:                # noqa: BLE001
                    j["last_status"] = "error: %s" % e
                finally:
                    j["last_run"] = _now()
                    j["next_run"] = _next_run(j)
                    _STATE["running"] = ""
                    save_jobs(jobs)
            _STATE["last_tick"] = now
        except Exception:
            pass
        time.sleep(20)


def start() -> dict:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return {"ok": True, "already": True}
    _THREAD = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _THREAD.start()
    return {"ok": True, "started": True}


def status() -> dict:
    jobs = load_jobs()
    return {"ok": True,
            "running": _STATE["running"],
            "last_tick": _STATE["last_tick"],
            "count": len(jobs),
            "enabled": len([j for j in jobs if j.get("enabled")]),
            "thread_alive": bool(_THREAD and _THREAD.is_alive())}
