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

import hashlib
import json
import os
import threading
import time
import uuid

from . import config, sessions
import logging
logger = logging.getLogger(__name__)

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


# ★ 2026-11-15 用户要求：「反思自检评估以及抽空代码自检这些内容彻底成为这个
#   项目的一项功能，不要放在自动化里面，而是放在迭代想法里面」。
#
#   所以原先住在这里的四条「内置任务」（_BUILTIN_DEFS / _builtin_job /
#   _ensure_builtins）**整体搬到了 backend/selfmaint.py**：
#     · 它们的定义、节律、启停、上次结果、历史，都在 selfmaint 里，
#       状态存 data/self_maint.json，与用户的 schedules.json 彻底分开；
#     · 入口在「🌱 AI 迭代想法」抽屉的「自我维护」区块；
#     · 执行体仍是本文件的 _run_selfiter / _run_selfaudit / _run_selfexp /
#       _run_selfidle（会写会话 + 写 schedule_logs 的 md，历史能翻到），
#       由 selfmaint 延迟 import 本模块后调用，不重复实现一份；
#     · 本文件只剩一件事：load_jobs() 把旧文件里遗留的 kind 任务**迁移一次**
#       过去，然后从定时任务列表里移除（用户要「只留我自己建的任务」）。
#   保留 _LEGACY_KINDS 只为迁移时识别旧数据，不参与任何新建/自愈逻辑。
_LEGACY_KINDS = ("selfiter", "selfaudit", "selfexp", "selfidle")


def load_jobs_raw() -> list:
    """只读 schedules.json，**不做任何自愈 / 迁移**（给 selfmaint 迁移用）。"""
    try:
        with open(_jobs_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def load_jobs() -> list:
    """读用户的定时任务。

    ★ 2026-11-15 用户要求：「反思自检评估以及抽空代码自检彻底成为这个项目的
      一项功能，不要放在自动化里面，而是放在迭代想法里面」。
      所以这里**不再自愈补回内置任务**——它们已经搬到 backend/selfmaint.py，
      定义写在代码里，与 schedules.json 无关。
      这里只做一件事：如果旧文件里还留着 kind 任务，**迁移一次**到 selfmaint
      并从本文件移除（用户要「定时任务列表里只留我自己建的任务」）。
      迁移是幂等的：搬完就找不到 kind 任务，之后再调是空操作。
    """
    jobs = load_jobs_raw()
    try:
        from . import selfmaint as _sm
        if any(str(j.get("kind") or "") in _sm.KINDS for j in jobs):
            _sm.migrate_from_schedules()
            jobs = load_jobs_raw()
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return jobs


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
        logger.debug("silent-except", exc_info=True)
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
        logger.debug("silent-except", exc_info=True)

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
                        # 只要 num_ctx，别带模型探测（Ollama 没开时那要等 4 秒）
                        room = max(1024, int(_perf.get(with_detect=False)["num_ctx"]) - pt - 512)
                    params = dict(params)
                    params["max_tokens"] = min(
                        24576, max(int(params.get("max_tokens") or 4096) * 2, room))
                except Exception:
                    logger.debug("silent-except", exc_info=True)
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


def _run_selfiter(job: dict) -> dict:
    """内置任务：每日自我反思（kind="selfiter"）。

    2026-11-15：用户要「每天定时自动跑一次」反思。
    让模型自己在对话里"记得去调反思工具"太不可靠（它经常忘），
    所以做成内置分支，到点必定执行；产出的计划仍然**要用户批准**才动手。
    """
    from . import self_iterate as _si
    try:
        r = _si.reflect(trigger="daily", use_web=True,
                        focus=str(job.get("focus") or ""))
    except Exception as e:                              # noqa: BLE001
        r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if not r.get("ok"):
        raise RuntimeError("自我反思失败：%s" % r.get("error"))

    p = r.get("plan") or {}
    answer = ("【每日自我反思】%s\n\n"
              "- 自我反思：%d 条\n- 对你的观察：%d 条\n- 可加功能：%d 条\n"
              "- 技术动态：%d 条\n- 等你批准的动作：%d 条\n\n"
              "打开顶栏「🌱 迭代想法」看详情。想改的代码要你点批准才会动手。"
              % (p.get("summary") or "", len(p.get("weaknesses") or []),
                 len(p.get("user_insights") or []), len(p.get("features") or []),
                 p.get("tech_total") or 0, len(p.get("actions") or [])))

    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:                               # noqa: BLE001
            sid = ""
    try:
        old = sessions.get_messages(sid) or []
        stamp = _now()
        old.append({"role": "user", "ts": stamp,
                    "content": "【%s】" % (job.get("name") or "自我维护·自我反思")})
        old.append({"role": "assistant", "ts": stamp, "content": answer})
        sessions.save_messages(sid, old)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    try:
        pth = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"),
                                                      time.strftime("%Y%m%d_%H%M%S")))
        with open(pth, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 类型：每日自我反思\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(), answer))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"answer": answer, "model": "selfiter", "channel": "builtin", "session": sid}


def _run_selfaudit(job: dict) -> dict:
    """内置任务：每日代码自检（kind="selfaudit"）。

    2026-11-15 用户要求：「智能体还要能主动且时不时检查自己的代码并且排除
    自己代码中不合理且存在 bug 的地方」，并且「每天定时一次」。

    和 _run_selfiter 一样做成内置分支：让模型自己"记得去自检"太不可靠。
    信任模式开着时，发现问题直接修（用户明确要求免批准）。
    """
    from . import self_iterate as _si
    s = _si.load_settings()
    auto = bool(s.get("trust_mode"))
    deep = bool(s.get("deep_audit") or str(job.get("focus") or "") == "deep")
    try:
        r = _si.run_audit(deep=deep, source="daily", create_plan=True, auto_fix=auto)
    except Exception as e:                              # noqa: BLE001
        raise RuntimeError("代码自检失败：%s: %s" % (type(e).__name__, e))
    if not r.get("ok"):
        raise RuntimeError("代码自检失败：%s" % r.get("error"))

    a = r.get("audit") or {}
    cnt = a.get("counts") or {}
    lines = ["【每日代码自检】%s" % (a.get("ts") or ""), ""]
    lines.append("- 扫了 %s 个 py + %s 个 js，用时 %s 秒"
                 % (a.get("files"), a.get("js_files"), a.get("seconds")))
    lines.append("- 发现 %s 处：high %s / mid %s / info %s"
                 % (a.get("total"), cnt.get("high"), cnt.get("mid"), cnt.get("info")))
    acts = (r.get("plan") or {}).get("actions") or []
    lines.append("- 整理成 %d 条待办" % len(acts))
    auto_r = r.get("auto") or {}
    if auto_r:
        lines.append("- 信任模式：自动执行 %s/%s 条"
                     % (auto_r.get("done"), auto_r.get("total")))
        if auto_r.get("need_restart"):
            lines.append("- ⚠️ 改了代码，需要点「立即重启」才生效")
    elif acts and not auto:
        lines.append("- 等你批准（信任模式没开）")
    answer = "\n".join(lines)

    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:                               # noqa: BLE001
            sid = ""
    try:
        old = sessions.get_messages(sid) or []
        stamp = _now()
        old.append({"role": "user", "ts": stamp,
                    "content": "【%s】" % (job.get("name") or "自我维护·代码自检")})
        old.append({"role": "assistant", "ts": stamp, "content": answer})
        sessions.save_messages(sid, old)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    try:
        pth = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"),
                                                      time.strftime("%Y%m%d_%H%M%S")))
        with open(pth, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 类型：每日代码自检\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(), answer))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"answer": answer, "model": "selfaudit", "channel": "builtin", "session": sid}


def _run_selfexp(job: dict) -> dict:
    """内置任务：定时体验评估（kind="selfexp"）。

    2026-11-15 用户要求：「主动在与用户交流中评估出任务的完成效果，
    如果评分不好或过程繁琐就要思考怎么解决」，并且「时不时」做。

    做成内置分支的理由同 selfiter/selfaudit：让模型自己记得去评估太不可靠。
    """
    from . import self_iterate as _si
    s = _si.load_settings()
    # 自动触发时尊重「关掉定时评估」的设置；用户在迭代想法里手动点
    # 「立即运行」时（job.force）照样跑一次 —— 手动操作不该被定时开关挡住。
    if not s.get("evaluate_periodic", True) and not job.get("force"):
        return {"answer": "【体验评估】已在设置里关闭定时评估，本次跳过。",
                "model": "selfexp", "channel": "builtin",
                "session": str(job.get("session") or "")}
    try:
        r = _si.evaluate_and_improve(limit=12, use_llm=True, trigger="periodic",
                                     auto_improve=bool(s.get("trust_mode")))
    except Exception as e:                              # noqa: BLE001
        raise RuntimeError("体验评估失败：%s: %s" % (type(e).__name__, e))
    if r.get("skipped"):
        return {"answer": "【体验评估】%s" % (r.get("reason") or "最近没有新对话可评，跳过。"),
                "model": "selfexp", "channel": "builtin",
                "session": str(job.get("session") or "")}
    if not r.get("ok"):
        raise RuntimeError("体验评估失败：%s" % r.get("error"))

    rec = r.get("record") or {}
    sm = r.get("summary") or {}
    lines = ["【定时体验评估】%s" % (rec.get("ts") or ""), ""]
    lines.append("- 评了最近 %s 轮" % rec.get("n"))
    lines.append("- 完成度均分 %s / 10，效率均分 %s / 10，趋势：%s"
                 % (rec.get("avg_completion"), rec.get("avg_efficiency"),
                    sm.get("trend") or "样本不足"))
    if rec.get("overall"):
        lines.append("- 整体判断：%s" % rec["overall"])
    fr = sm.get("frictions") or []
    if fr:
        lines.append("- 摩擦点（按出现次数）：")
        for f in fr[:5]:
            lines.append("  · %s ×%s — %s" % (f.get("tag"), f.get("count"),
                                              f.get("suggestion") or f.get("detail") or ""))
    if r.get("need_improve"):
        lines.append("- ⚠️ 判定为「需要改进」：%s" % r.get("reason"))
        imp = r.get("improve") or {}
        acts = (imp.get("plan") or {}).get("actions") or []
        if acts:
            lines.append("- 已整理 %d 条改进动作" % len(acts))
        auto_r = imp.get("auto") or {}
        if auto_r:
            lines.append("- 信任模式：自动执行 %s/%s 条"
                         % (auto_r.get("done"), auto_r.get("total")))
            if auto_r.get("need_restart"):
                lines.append("- ⚠️ 改了代码，需要点「立即重启」才生效")
        elif acts:
            lines.append("- 等你批准（信任模式没开）")
        elif not imp.get("ok"):
            lines.append("- 反思没跑成：%s" % imp.get("error"))
    else:
        lines.append("- 判定为「不需要专门改进」")
    answer = "\n".join(lines)

    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:                               # noqa: BLE001
            sid = ""
    try:
        old = sessions.get_messages(sid) or []
        stamp = _now()
        old.append({"role": "user", "ts": stamp,
                    "content": "【%s】" % (job.get("name") or "自我维护·体验评估")})
        old.append({"role": "assistant", "ts": stamp, "content": answer})
        sessions.save_messages(sid, old)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    try:
        pth = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"),
                                                      time.strftime("%Y%m%d_%H%M%S")))
        with open(pth, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 类型：定时体验评估\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(), answer))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"answer": answer, "model": "selfexp", "channel": "builtin", "session": sid}


def _run_selfidle(job: dict) -> dict:
    """内置任务：抽空自检（kind="selfidle"）。

    用户原话：「自检这东西能不能也是和自动迭代一样，自从抽空检测」。
    默认每 20 分钟触发一次，但**绝大多数时候什么都不做** —— 只做一件事：
    问一句「现在是不是空档」，不是就直接返回，不写会话、不写日志文件
    （否则聊天区会被成片的「已跳过」刷满）。

    空档的判定在 self_iterate.idle_gate()：开关、用户安静时长、
    距上次自检的间隔、有没有反思/迭代在跑，四条全过才真跑。
    """
    from . import self_iterate as _si
    s = _si.load_settings()
    gate = _si.idle_gate(s)
    # 手动点「立即运行」时越过空闲闸 —— 用户就是现在要它跑，
    # 再拿「你是不是在忙」把他挡回去没有意义。
    if job.get("force"):
        gate = {"ok": True, "reason": "手动触发"}
    if not gate.get("ok"):
        return {"answer": "", "model": "selfidle", "channel": "builtin",
                "session": "", "skipped": True,
                "reason": gate.get("reason") or "不是空档"}

    auto = bool(s.get("trust_mode"))
    try:
        r = _si.run_audit(deep=bool(s.get("deep_audit")), source="idle",
                          create_plan=True, auto_fix=auto)
    except Exception as e:                              # noqa: BLE001
        raise RuntimeError("抽空自检失败：%s: %s" % (type(e).__name__, e))
    if not r.get("ok"):
        raise RuntimeError("抽空自检失败：%s" % r.get("error"))

    a = r.get("audit") or {}
    cnt = a.get("counts") or {}
    total = int(a.get("total") or 0)
    acts = (r.get("plan") or {}).get("actions") or []
    auto_r = r.get("auto") or {}

    # 干干净净又没改代码 → 不打扰用户（记录在 audits.json，抽屉里能查到）
    # 手动运行时即使「没问题」也要给个明确结果，不能静默跳过
    if total <= 0 and not auto_r and not job.get("force"):
        return {"answer": "", "model": "selfidle", "channel": "builtin",
                "session": "", "skipped": True,
                "reason": "空档自检完成，没有发现问题（%s 秒）" % a.get("seconds")}

    lines = ["【抽空自检】%s" % (a.get("ts") or ""), ""]
    lines.append("- 趁空档扫了 %s 个 py + %s 个 js，用时 %s 秒"
                 % (a.get("files"), a.get("js_files"), a.get("seconds")))
    lines.append("- 发现 %s 处：high %s / mid %s / low %s / info %s"
                 % (total, cnt.get("high"), cnt.get("mid"),
                    cnt.get("low"), cnt.get("info")))
    lines.append("- 整理成 %d 条待办" % len(acts))
    if auto_r:
        lines.append("- 信任模式：自动执行 %s/%s 条"
                     % (auto_r.get("done"), auto_r.get("total")))
        if auto_r.get("need_restart"):
            lines.append("- ⚠️ 改了代码，需要点「立即重启」才生效")
    elif acts and not auto:
        lines.append("- 等你批准（信任模式没开）")
    answer = "\n".join(lines)

    sid = str(job.get("session") or "")
    if not sid:
        try:
            sid = sessions.ensure_default().get("id") or ""
        except Exception:                               # noqa: BLE001
            sid = ""
    try:
        old = sessions.get_messages(sid) or []
        stamp = _now()
        old.append({"role": "user", "ts": stamp,
                    "content": "【%s】" % (job.get("name") or "自我维护·抽空自检")})
        old.append({"role": "assistant", "ts": stamp, "content": answer})
        sessions.save_messages(sid, old)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    try:
        pth = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"),
                                                      time.strftime("%Y%m%d_%H%M%S")))
        with open(pth, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 类型：抽空自检\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(), answer))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return {"answer": answer, "model": "selfidle", "channel": "builtin", "session": sid}


def _run_once(job: dict) -> dict:
    """真的跑一次：自动挑通道（本机 / 云端）+ 工具循环，结果写回会话。"""
    from . import tools as T

    # ★ 内置任务（每日自我反思）—— 不走 LLM 工具循环
    if str(job.get("kind") or "") == "selfiter":
        return _run_selfiter(job)
    # ★ 内置任务（每日代码自检）—— 同上
    if str(job.get("kind") or "") == "selfaudit":
        return _run_selfaudit(job)
    # ★ 内置任务（定时体验评估）—— 同上
    if str(job.get("kind") or "") == "selfexp":
        return _run_selfexp(job)
    # ★ 内置任务（抽空自检）—— 同上
    if str(job.get("kind") or "") == "selfidle":
        return _run_selfidle(job)

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
        logger.debug("silent-except", exc_info=True)

    # 日志留档
    try:
        p = os.path.join(_logs_dir(), "%s_%s.md" % (job.get("id"), time.strftime("%Y%m%d_%H%M%S")))
        with open(p, "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 时间：%s\n- 通道：%s\n- 模型：%s\n- 周期：%s\n\n## 任务\n\n%s\n\n## 结果\n\n%s\n"
                    % (job.get("name") or "", _now(),
                       "云端" if channel == "cloud" else "本机",
                       model, describe(job), prompt, answer))
    except Exception:
        logger.debug("silent-except", exc_info=True)
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
                logger.debug("silent-except", exc_info=True)
        _th.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True, "id": job_id,
                "msg": "已开始，通常 1~2 分钟；结果会写回会话并留档"}
    _STATE["running"] = job_id
    try:
        r = _run_once(job)
        job["last_run"] = _now()
        if r.get("skipped"):
            job["last_status"] = "skipped"
            job["last_preview"] = str(r.get("reason") or "")[:200]
        else:
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
        session: str = "", model: str = "", kind: str = "", focus: str = "") -> dict:
    """新建定时任务。

    kind：内置任务类型。目前只有 "selfiter"（每日自我反思）——
    这类任务不走 LLM 工具循环，直接调 self_iterate.reflect()，
    所以允许 prompt 为空。
    """
    prompt = (prompt or "").strip()
    if not prompt and not kind:
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
           "name": (name or prompt[:20] or kind).strip()[:40],
           "prompt": prompt, "schedule": sch,
           "session": session, "model": model,
           "kind": kind, "focus": focus,
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
    # ★ 2026-11-15 用户要求：「自检和反思这些不能删除，只能暂停」。
    #   这四类是智能体**自我维护**的骨架（自我反思 / 代码自检 / 体验评估 / 抽空自检），
    #   删掉之后就再也不会自动跑，用户也没有入口把它加回来。所以后端硬拦一层：
    #   内置任务一律只允许「暂停」（update enabled=False），不给删。
    #   前端也据此不渲染删除按钮，但后端必须自己守住 —— 前端可以被绕过。
    jobs = load_jobs()
    hit = next((j for j in jobs if j.get("id") == job_id), None)
    if hit is not None:
        _k = str(hit.get("kind") or "").strip()
        if _k:
            _nm = str(hit.get("name") or job_id)
            _lbl = _BUILTIN_LABEL.get(_k, "自我维护")
            _what = _nm if _nm == _lbl else "「%s」这个%s" % (_nm, _lbl)
            return {
                "ok": False,
                "builtin": True,
                "error": "%s是内置任务，不能删除，只能暂停。"
                         "想让它不再自动跑，点卡片上的「暂停」即可。" % _what,
            }
    n = len(jobs)
    jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) == n:
        return {"ok": False, "error": "没有这个任务"}
    save_jobs(jobs)
    return {"ok": True}


# 2026-11-15 用户要求：「自检和反思那些不要归类在自动化任务里，另外分一个类」。
# 这四类是智能体**自我维护**用的内置任务，和用户自己建的业务任务混在一个列表里
# 既看不清也容易误删，所以单独打标记，前端据此分两组渲染。
# 2026-11-15 追加：「自检和反思这些不能删除，只能暂停」——
# 内置的判定统一用「kind 非空」而不是写死这四种，以后新增内置类型不用再改判定；
# remove() 据此拒绝删除（只允许 update enabled=False）。
_BUILTIN_LABEL = {
    "selfiter": "自我反思",
    "selfaudit": "代码自检",
    "selfexp": "体验评估",
    "selfidle": "抽空自检",
}

# 兼容旧名字（其它地方按 _KIND_LABELS 取标签），不再单独维护一份表
_KIND_LABELS = _BUILTIN_LABEL


def list_jobs() -> list:
    out = []
    for j in load_jobs():
        j = dict(j)
        nr = float(j.get("next_run") or 0)
        j["next_human"] = (time.strftime("%m-%d %H:%M", time.localtime(nr)) if nr else "-")
        j["period"] = describe(j)
        # builtin=True 的归到「自我维护」，其余是用户自己的「自动化任务」
        _k = str(j.get("kind") or "").strip()
        j["channel_hint"] = ("\u2601 " if j.get("last_channel") == "cloud"
                             else ("\U0001f4bb " if j.get("last_channel") == "local" else "")) \
                            + str(j.get("last_model") or "")
        # ★ 2026-11-15 用户要求：「自检和反思那些不要归类在自动化任务里，另外分一个类」。
        #   kind 非空 = 智能体内置任务（自我反思 / 代码自检 / 体验评估 / 抽空自检），
        #   kind 为空 = 用户自己建的定时任务。前端据此分两组渲染。
        j["builtin"] = bool(_k)
        j["kind_label"] = _BUILTIN_LABEL.get(_k, "")
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
        # 2026-11-13：limit<=0 表示**整篇返回**（用户要求「不要少字」）。
        # 原来无条件 txt[:limit]，传 0 会直接返回空串，长报告也会被切在半句上。
        try:
            n = int(limit or 0)
        except Exception:
            n = 0
        return txt if n <= 0 else txt[:n]
    except Exception:
        return ""


# ---------------------------------------------------------------- 后台循环
# 自我维护是否正在跑（防止上一轮还没跑完又被下一个 tick 触发）
_MAINT_BUSY = [False]


def _maint_tick() -> None:
    """自我维护的一次 tick：到点就跑。

    ★ 单独开线程跑，**不占调度循环** —— 一次反思要 60~100 秒，如果直接
      在 _loop 里跑，用户自己建的定时任务会被拖后一两分钟。
    """
    if _MAINT_BUSY[0]:
        return
    try:
        from . import selfmaint as _sm
        if not _sm.due_items():
            return
        _MAINT_BUSY[0] = True
        try:
            done = _sm.run_all_due()
            if done:
                _STATE["last_maint"] = done
                logger.warning("自我维护自动执行：%s",
                               ", ".join("%s(%s)" % (d.get("kind"),
                                                     "跳过" if d.get("skipped")
                                                     else ("ok" if d.get("ok")
                                                           else "失败"))
                                         for d in done))
        finally:
            _MAINT_BUSY[0] = False
    except Exception:
        _MAINT_BUSY[0] = False
        logger.debug("silent-except", exc_info=True)


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

                def _attempt(_j=j):
                    _r = _run_once(_j)
                    if _r.get("skipped"):
                        _j["last_status"] = "skipped"
                        _j["last_preview"] = str(_r.get("reason") or "")[:200]
                    else:
                        _j["last_status"] = "ok"
                        _j["last_preview"] = str(_r.get("answer") or "")[:200]

                try:
                    try:
                        _attempt()
                    except Exception as _e1:          # noqa: BLE001
                        # ★ 内置任务（反思/自检/评估）失败重试一次。
                        #   一次反思要几十秒，偶发的模型超时或网络抖动不该让
                        #   今天这一轮直接报废；重试仍失败才记 error。
                        #   用户自建的任务**不重试** —— 它们可能带副作用
                        #   （发消息、跑脚本），重跑一遍不是用户想要的。
                        if not str(j.get("kind") or ""):
                            raise
                        logger.warning("内置任务 %s 首次失败，重试一次：%s",
                                       j.get("kind"), _e1)
                        _attempt()
                except Exception as e:                # noqa: BLE001
                    j["last_status"] = "error: %s" % e
                finally:
                    j.pop("catchup", None)
                    j["last_run"] = _now()
                    j["next_run"] = _next_run(j)
                    _STATE["running"] = ""
                    save_jobs(jobs)
            _STATE["last_tick"] = now
            # 自我维护（反思 / 自检 / 体验评估 / 抽空自检）：到点就跑，
            # 开独立线程，不阻塞用户自己建的任务
            try:
                threading.Thread(target=_maint_tick, daemon=True,
                                 name="maint-tick").start()
            except Exception:
                logger.debug("silent-except", exc_info=True)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        time.sleep(20)


def _catchup_builtins() -> list:
    """应用关着时错过了内置任务的执行时间 → 启动后补跑一次，返回补跑的 kind。

    ★ 2026-11-15：光有「到点必定执行」不够 —— 关机一夜，第二天开机时
    next_run 还停在昨天，不补跑就要白等一整天（内置任务多是每天一次）。
    这里把**已过期**的内置任务 next_run 拉到「现在」，调度循环 20 秒内就会
    真跑一次；跑完会重算 next_run，所以只会补一次，不会反复触发。

    两条护栏：
      · 只补 enabled 的（用户暂停了就尊重，不自作主张）；
      · 过期不到 5 分钟的不算「错过」，交给 _loop 正常处理，不插队。
    """
    # ★ 2026-11-15：内置任务已迁到 backend/selfmaint.py，补跑交给它做。
    try:
        from . import selfmaint as _sm
        return _sm.catchup()
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []


def start() -> dict:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return {"ok": True, "already": True}
    # ★ 启动先跑一次 load_jobs：它会把 schedules.json 里遗留的内置任务
    #   迁到 selfmaint（幂等），并保证 selfmaint 的状态文件存在。
    try:
        load_jobs()
        from . import selfmaint as _sm
        _sm.status()                 # 触发一次自愈：状态文件丢了就重建默认
    except Exception:
        logger.debug("silent-except", exc_info=True)
    # ★ 再补跑关机期间错过的（只对内置任务）
    try:
        _c = _catchup_builtins()
        if _c:
            logger.warning("启动补跑内置任务：%s", ",".join(_c))
    except Exception:
        logger.debug("silent-except", exc_info=True)
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


# ---------------------------------------------------------------- 检索
# 2026-11-13 用户要求：「定时任务内容也要支持检索，最好向量化提高召回率」。
# 直接复用知识库那套后端（backend/embed.py），不另建一套索引：
#   · 任务数量本来就少（几个到几十个），一次 embed 全部文本足够快；
#   · 按「文本 md5」缓存向量，内容没变就不重算；
#   · 向量后端不可用（没装 embedding 模型）时自动退回纯关键词，
#     不会因为"没配模型"就让检索直接不可用。
_VEC_LOCK = threading.Lock()
_VEC_CACHE = {"sig": "", "items": {}}


def _vec_path() -> str:
    return config.data("data", "schedule_vecs.json")


def _load_vec_cache() -> dict:
    global _VEC_CACHE
    if _VEC_CACHE.get("items"):
        return _VEC_CACHE
    try:
        with open(_vec_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        if isinstance(d, dict):
            _VEC_CACHE = {"sig": str(d.get("sig") or ""),
                          "items": dict(d.get("items") or {})}
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return _VEC_CACHE


def _save_vec_cache() -> None:
    try:
        p = _vec_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_VEC_CACHE, f)
        os.replace(tmp, p)
    except Exception:
        logger.debug("silent-except", exc_info=True)


def _job_search_text(job: dict) -> str:
    """一条任务参与检索的文本：名称 + 任务内容 + 上次结果摘要。"""
    parts = [str(job.get("name") or ""), str(job.get("prompt") or "")]
    prev = str(job.get("last_preview") or "")
    if prev:
        parts.append(prev)
    return "\n".join(p for p in parts if p)


def _bigrams(s: str) -> set:
    """字符二元组。中文没有空格分词，用 bigram 当关键词信号最稳。"""
    s = "".join(ch for ch in str(s or "").lower() if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def search_jobs(query: str, top_k: int = 50) -> dict:
    """在定时任务里做混合检索，返回 {ok, items, backend}。

    backend 会告诉前端这次到底是"向量+关键词"还是"仅关键词"，
    这样界面上能如实标注，而不是假装一直在用向量。
    """
    query = (query or "").strip()
    jobs = load_jobs()
    if not jobs:
        return {"ok": True, "items": [], "backend": "none"}
    if not query:
        items = []
        for j in jobs:
            d = dict(j)
            d.update({"score": 1.0, "vec_score": 0.0, "kw_score": 0.0})
            d["builtin"] = bool(str(j.get("kind") or "").strip())
            d["kind_label"] = _KIND_LABELS.get(str(j.get("kind") or ""), "")
            items.append(d)
        return {"ok": True, "items": items, "backend": "all"}

    texts = [_job_search_text(j) for j in jobs]
    n = len(texts)

    # ① 关键词：查询的字符 bigram 有多大比例出现在任务文本里
    q_grams = _bigrams(query)
    kw = [0.0] * n
    if q_grams:
        for i, t in enumerate(texts):
            g = _bigrams(t)
            if g:
                kw[i] = len(q_grams & g) / float(len(q_grams))

    # ② 语义向量（带缓存）
    vec = None
    backend = "keyword"
    try:
        import numpy as np
        from . import embed as _embed
        try:
            sig = str(_embed.signature() or "")
        except Exception:
            sig = ""
        with _VEC_LOCK:
            cache = _load_vec_cache()
            if cache.get("sig") != sig:
                _VEC_CACHE.clear()
                _VEC_CACHE.update({"sig": sig, "items": {}})
            items = _VEC_CACHE.get("items") or {}
            need, keys = [], []
            for j, t in zip(jobs, texts):
                k = str(j.get("id") or "")
                h = hashlib.md5(t.encode("utf-8")).hexdigest()
                old = items.get(k) or {}
                if old.get("h") != h or not old.get("v"):
                    need.append(t)
                    keys.append((k, h))
            if need:
                got = _embed.embed_texts(need, is_query=False)
                if got is not None and len(got) == len(need):
                    for (k, h), v in zip(keys, got):
                        items[k] = {"h": h, "v": [float(x) for x in v]}
                    _save_vec_cache()
            qv = _embed.embed_texts([query], is_query=True)
            if qv is not None and len(qv) == 1:
                qvec = np.asarray(qv[0], dtype=np.float32)
                qn = float(np.linalg.norm(qvec)) or 1.0
                qvec = qvec / qn
                sims = []
                for j in jobs:
                    it = items.get(str(j.get("id") or "")) or {}
                    v = it.get("v")
                    if not v:
                        sims.append(0.0)
                        continue
                    arr = np.asarray(v, dtype=np.float32)
                    an = float(np.linalg.norm(arr)) or 1.0
                    sims.append(float(arr @ qvec / an))
                vec = sims
                backend = "vector+keyword"
    except Exception:
        vec = None
        backend = "keyword"

    out = []
    for i, j in enumerate(jobs):
        v = float(vec[i]) if vec else 0.0
        score = (0.65 * v + 0.35 * kw[i]) if vec else kw[i]
        d = dict(j)
        d["score"] = round(float(score), 4)
        d["vec_score"] = round(v, 4)
        d["kw_score"] = round(float(kw[i]), 4)
        # 检索结果同样要带分组信息，否则内置任务会被前端当成「我的任务」
        d["builtin"] = bool(str(j.get("kind") or "").strip())
        d["kind_label"] = _KIND_LABELS.get(str(j.get("kind") or ""), "")
        out.append(d)
    out.sort(key=lambda x: -x["score"])
    hit = [d for d in out if d["score"] > 0.02 or d["kw_score"] > 0]
    return {"ok": True, "items": (hit or out)[:max(1, int(top_k))],
            "backend": backend}
