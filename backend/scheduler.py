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
def _ollama_alive(cfg: dict) -> bool:
    """2 秒内探一下本机 Ollama 在不在线。

    为什么要先探：直接调 chat 会抛 requests 的连接异常，错误信息对用户毫无意义
    （就是那句「无法连接 ollama」）。先探一下，才能决定"换云端跑"还是"给出人话提示"。
    """
    import requests
    url = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        return requests.get(url + "/api/tags", timeout=2).status_code == 200
    except Exception:
        return False


def _is_cloud_name(model: str) -> bool:
    m = str(model or "").strip().lower()
    return m.startswith("deepseek") or m.startswith("cloud:")


def _cloud_key() -> str:
    try:
        from . import keyvault
        return keyvault.get_key()
    except Exception:
        return ""


def _pref_path() -> str:
    return config.data("data", "chat_model.json")


def get_chat_model() -> str:
    """前端「对话最后选用的模型」（把 localStorage 的 chat_provider 镜像一份到本机）。

    为什么要镜像：定时任务半夜跑，浏览器不在场，后端拿不到前端的下拉框。
    """
    try:
        with open(_pref_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return str((d or {}).get("model") or "")
    except Exception:
        return ""


def set_chat_model(model: str) -> dict:
    """记下对话最后选用的模型（定时任务没写模型时就用它）。"""
    m = str(model or "").strip()
    try:
        p = _pref_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"model": m, "updated": _now()}, f, ensure_ascii=False)
        return {"ok": True, "model": m}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _pick_channel(job: dict, cfg: dict):
    """决定这次走哪条通道：返回 (channel, model, note)。

    规则（用户要求「无论用什么模型都能完成定时任务」）：
      1. 任务里写了模型 → 尊重它：`local:xxx` 走本机，`deepseek-xxx` 走云端；
      2. 没写 → 本机 Ollama 在线就用默认本地模型；不在线而有云端 Key 就用云端；
      3. 选中的通道缺条件（云端没 Key / 本机没 Ollama）→ **自动换另一条**；
      4. 两条都不行 → 抛人话错误，写进任务状态里（不再是干巴巴的"无法连接"）。
    """
    want = str(job.get("model") or "").strip()
    # ★ 2026-11-10 用户要求：「对话最后一次选定用什么模型，就用什么模型完成自动化」。
    #   任务没写模型时，跟随前端镜像过来的「最后选用模型」；仍是 auto/空才走老的自动判断。
    if not want or want.lower() == "auto":
        want = get_chat_model()
        if want.lower() == "auto":
            want = ""
    default_model = str(cfg.get("default_model") or "qwen3-vl:30b")
    key = _cloud_key()
    local_ok = _ollama_alive(cfg)
    note = ""

    if want == "local":
        # 前端选了「本机默认模型」
        channel, model = "local", default_model
    elif want.startswith("local:"):
        channel, model = "local", want[6:].strip() or default_model
    elif _is_cloud_name(want):
        channel = "cloud"
        model = want.split(":", 1)[1] if want.lower().startswith("cloud:") else want
        if model in ("", "auto"):
            model = "deepseek-flash"
    elif local_ok:
        channel, model = "local", (want or default_model)
    elif key:
        channel, model = "cloud", "deepseek-flash"
        note = "本机 Ollama 不在线，已自动改用云端模型"
    else:
        raise RuntimeError(
            "两条通道都不可用：本机 Ollama 连不上（%s），也没有保存 DeepSeek Key。"
            "请在顶栏 🔑 里填一次 Key（会同时存到本机，定时任务也能用），"
            "或者先把 Ollama 启动起来。"
            % str(cfg.get("ollama_url") or "http://127.0.0.1:11434"))

    # 通道缺条件 → 自动换另一条
    if channel == "cloud" and not key:
        if local_ok:
            channel, model = "local", default_model
            note = "没有云端 Key，已改用本机模型"
        else:
            raise RuntimeError(
                "这个任务指定了云端模型 %s，但本机没有保存 DeepSeek Key，"
                "且 Ollama 也不在线。请先在顶栏 🔑 里填一次 Key。" % model)
    if channel == "local" and not local_ok:
        if key:
            channel, model = "cloud", "deepseek-flash"
            note = "本机 Ollama 不在线，已自动改用云端模型"
        else:
            raise RuntimeError(
                "本机 Ollama 连不上（%s），也没有保存 DeepSeek Key。"
                "要么启动 Ollama，要么在顶栏 🔑 填一次 Key。"
                % str(cfg.get("ollama_url") or "http://127.0.0.1:11434"))
    return channel, model, note


def _run_local(M, cfg: dict, model: str, prompt: str, schemas: list, ctx: dict) -> str:
    """本机 Ollama 通道：原来的工具循环。"""
    # 模型没下载就退回已装的
    try:
        have = M._installed_models()
        if have and model not in have:
            model = next((n for n in have if "qwen3-vl" in n), "") or have[0]
    except Exception:
        pass

    # ★ 2026-11-10 修「定时任务只输出一半」。
    #   用户实测：国际新闻那条断在「#### 2. 油价飙升至五年高位：能源市场」，
    #   后半截整段没有。根因：这里 stream=False 一把梭、**完全不看 done_reason**，
    #   模型撞到 num_predict / 窗口上限时 content 本来就是半截，
    #   却直接当完整答案返回，既不续写也不提示。
    #   主对话（main.py 的 [continue] 分支）和云端通道早就有续写，只有这条漏了。
    msgs = [{"role": "user", "content": prompt}]
    parts: list = []
    cont_used = 0
    params = dict(cfg)
    for _ in range(12):
        resp = M.client.chat(msgs, model=model, stream=False,
                             tools=schemas, params=params)
        data = resp.json() if hasattr(resp, "json") else resp
        msg = data.get("message") or {}
        calls = msg.get("tool_calls") or []
        body = str(msg.get("content") or "")
        reason = str(data.get("done_reason") or "")

        if not calls:
            if body:
                parts.append(body)
            # 撞长度上限 → 从断开处续写（最多 3 次），否则用户永远只看得到半句
            if reason == "length" and cont_used < 3:
                cont_used += 1
                msgs.append({"role": "assistant", "content": body})
                msgs.append({"role": "user", "content": (
                    "（系统提示）你上一条回答**被长度上限截断了**，停在了半句话上。"
                    "现在**从断开的地方接着往下写**：不要重复已经写过的内容，"
                    "不要重新开头，不要解释、不要道歉，直接续上正文；"
                    "如果内容其实已经写完，就只把最后那句补完整。")})
                # 额度按**真实窗口**重算 —— 否则同样的额度会在同一个地方再断一次
                try:
                    pt = int(data.get("prompt_eval_count") or 0)
                    room = 8192
                    if pt:
                        from . import perf as _perf
                        room = max(1024, int(_perf.get()["num_ctx"]) - pt - 512)
                    params = dict(params)
                    params["max_tokens"] = min(
                        24576, max(int(params.get("max_tokens") or 4096) * 2, room))
                except Exception:
                    pass
                continue
            if reason == "length":
                parts.append("\n\n（⚠️ 这条输出达到长度上限被截断，"
                             "回一句「继续」可以接着写。）")
            break
        msgs.append({"role": "assistant", "content": body,
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
                out = T_dispatch(name, args or {}, [], ctx)
            except Exception as e:                    # noqa: BLE001
                out = "工具出错：%s: %s" % (type(e).__name__, e)
            msgs.append({"role": "tool", "content": str(out)[:8000]})
    return "".join(parts)


def _run_cloud(cfg: dict, model: str, key: str, prompt: str,
               schemas: list, ctx: dict) -> str:
    """云端通道（DeepSeek / OpenAI 兼容）：复用主对话那套带工具的流式循环。

    ★ 2026-11-09 新增 —— 用户要求「无论使用什么模型都可以完成定时任务」。
      原来这里写死 `M.client.chat(...)`（本机 Ollama），用户机器上 Ollama 没起
      或没下载模型，定时任务就必然失败。
    """
    from . import deepseek_client

    acc: list = []

    def executor(name: str, args: dict) -> str:
        return T_dispatch(name, args or {}, [], ctx)

    for kind, val in deepseek_client.chat_stream_with_tools(
            [{"role": "user", "content": prompt}], model, key, schemas, executor,
            temperature=min(1.3, max(0.0, float(cfg.get("temperature") or 0.6))),
            top_p=min(1.0, max(0.01, float(cfg.get("top_p") or 0.95))),
            max_tokens=max(4096, int(cfg.get("max_tokens") or 8192)),
            max_rounds=max(1, min(int(cfg.get("max_tool_rounds") or 30), 200))):
        if kind == "content":
            acc.append(val)
    return "".join(acc)


def T_dispatch(name, args, events, ctx):
    """延迟取 tools.dispatch（避免模块级循环导入）。"""
    from . import tools as _T
    return _T.dispatch(name, args, events, ctx)


def _run_once(job: dict) -> dict:
    """真的跑一次：自动挑通道（本机 / 云端）+ 工具循环，结果写回会话。"""
    from . import tools as T

    cfg = config.load_config() or {}
    prompt = str(job.get("prompt") or "").strip()
    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:
            sid = ""

    channel, model, note = _pick_channel(job, cfg)
    job["last_model"] = model
    job["last_channel"] = channel

    schemas = T.make_schemas(
        web_enabled=bool(cfg.get("web_enabled")),
        kb_enabled=bool(cfg.get("rag_enabled")),
        code_exec=bool(cfg.get("code_exec_enabled")),
        office_gen=True, ask_mode="quick")
    ctx = {"images": [], "last_image": None, "mask": None, "shown_images": [],
           "ask_mode": "quick", "session": sid, "user_text": prompt,
           "image_key": "",
           # 云端通道执行工具时可能要 Key（如云端视觉/图片），本机通道给空
           "ds_key": (_cloud_key() if channel == "cloud" else ""),
           # 无人值守：危险操作自动批准、提问直接跳过（见文件头第 2 条）
           "confirm": lambda payload: True,
           "ask": lambda payload: []}

    # ★ 两条通道互为备胎：一条跑拖了就自动换另一条 ——
    #   用户要求的是「无论用什么模型都能完成定时任务」，不是「选中了就只能靠它」。
    from . import main as M          # 延迟导入，避免循环依赖
    default_model = str(cfg.get("default_model") or "qwen3-vl:30b")
    try:
        if channel == "cloud":
            answer = _run_cloud(cfg, model, _cloud_key(), prompt, schemas, ctx)
        else:
            answer = _run_local(M, cfg, model, prompt, schemas, ctx)
    except Exception as _e:                           # noqa: BLE001
        if channel == "cloud" and _ollama_alive(cfg):
            channel, model = "local", default_model
            job["last_model"], job["last_channel"] = model, channel
            note = "云端调用失败（%s），已自动改用本机模型" % type(_e).__name__
            answer = _run_local(M, cfg, model, prompt, schemas, ctx)
        elif channel == "local" and _cloud_key():
            channel, model = "cloud", "deepseek-flash"
            job["last_model"], job["last_channel"] = model, channel
            note = "本机模型调用失败（%s），已自动改用云端模型" % type(_e).__name__
            answer = _run_cloud(cfg, model, _cloud_key(), prompt, schemas, ctx)
        else:
            raise

    if not answer:
        answer = "（这次没有产出正文，可能是轮次用尽或模型被截断）"
    if note:
        answer = "（%s）\n\n%s" % (note, answer)

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
            f.write("# %s\n\n- 时间：%s\n- 通道：%s\n- 模型：%s\n- 周期：%s\n\n## 任务\n\n%s\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(),
                       "云端" if channel == "cloud" else "本机",
                       model, describe(job), prompt, answer))
    except Exception:
        pass
    return {"answer": answer, "model": model, "channel": channel, "session": sid}


def run_now(job_id: str, background: bool = True) -> dict:
    """立即跑一次。

    2026-11-09：默认**后台跑**。原来同步执行，一次 60~120 秒（联网 + 工具 + 模型），
    浏览器/nginx 那边早就超时了，用户看到的就是"点了没反应"。
    现在立刻返回 {"started": true}，前端轮询 /api/schedule 看 last_run 变化。
    """
    jobs = load_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"ok": False, "error": "没有这个任务"}
    if _STATE.get("running") == job_id:
        return {"ok": True, "started": False, "busy": True,
                "msg": "这个任务正在跑，稍等"}
    if background:
        import threading as _th
        def _bg():
            try:
                run_now(job_id, background=False)
            except Exception:                        # noqa: BLE001
                pass
        _th.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True, "id": job_id,
                "msg": "已开始，通常 1~2 分钟；结果会写回会话并留档"}
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
        j["channel_hint"] = ("\u2601 " if j.get("last_channel") == "cloud"
                             else ("\U0001f4bb " if j.get("last_channel") == "local" else "")) \
                            + str(j.get("last_model") or "")
        out.append(j)
    return out


def latest_result(job_id: str, limit: int = 4000) -> str:
    """读某个任务**最近一次**运行的完整结果正文。

    2026-11-11：用户要求「外面无论单开多少对话，所有对话都能访问定时任务的
    完成情况和内容，是否查看由模型自主决断」——
    面板和 list_schedules 的摘要只给前 200 字（job["last_preview"]），
    正文留在这里，模型觉得需要时再取（list_schedules 传 with_result=true）。
    """
    try:
        d = _logs_dir()
        pref = "%s_" % job_id
        cand = sorted(x for x in os.listdir(d)
                      if x.startswith(pref) and x.endswith(".md"))
        if not cand:
            return ""
        with open(os.path.join(d, cand[-1]), "r", encoding="utf-8") as f:
            txt = f.read()
        k = txt.find("## 结果")
        if k >= 0:
            txt = txt[k + len("## 结果"):].lstrip()
        return txt[:limit]
    except Exception:
        return ""


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
                    _r = _run_once(j)
                    j["last_status"] = "ok"
                    j["last_preview"] = str(_r.get("answer") or "")[:200]
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
