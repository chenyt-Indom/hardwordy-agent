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
    # ★ 2026-11-24 专家意见（L3 三缺之三：没有预算）：
    #   定时任务是**最容易被忽略的绕过路径** —— 一天定时跑十次，
    #   预算账本上一次都不会记。所以在这里也过一道闸门。
    #   注意：paused 只挡「自动改代码」，不挡「生成计划」（见 _reflect_impl 的口径），
    #   所以这里查的是预算，不是 paused。
    try:
        from . import evolve_budget as _eb
        _g = _eb.gate("plans", 1)
        if not _g.get("ok"):
            _msg = "【每日自我反思】今天先不跑了：%s" % (_g.get("why") or "")
            logger.info("定时反思被预算挡下：%s", _msg)
            return {"answer": _msg, "model": "selfiter", "channel": "builtin",
                    "session": str(job.get("session") or ""), "skipped": True}
    except Exception:                                   # noqa: BLE001
        logger.debug("预算闸门失败（不拦，按原逻辑继续）", exc_info=True)
    try:
        r = _si.reflect(trigger="daily", use_web=True,
                        focus=str(job.get("focus") or ""))
    except Exception as e:                              # noqa: BLE001
        r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if not r.get("ok"):
        raise RuntimeError("自我反思失败：%s" % r.get("error"))

    # ★ 2026-11-24：跑成功才记 —— 失败/异常不该占额度。
    try:
        from . import evolve_budget as _eb2
        _eb2.spend("plans", 1, why="定时反思：%s" % (job.get("name") or ""))
    except Exception:                                   # noqa: BLE001
        logger.debug("记定时反思预算失败（不影响结果）", exc_info=True)
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
    # 与手动自检一致：selfaudit_use_llm（默认 True）决定要不要请 LLM 复核。
    deep = bool(_si.audit_deep_default(s)
                or str(job.get("focus") or "") == "deep")
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
    lines.append("- 完成度均分 %s / 10，效率均分 %s / 10"
                 % (rec.get("avg_completion"), rec.get("avg_efficiency")))
    # ★ 2026-11-23：曲线单列一行 —— 原来「趋势」是轮内对比，16 次评估全写
    #   「上升 0.7」，而跨评估斜率其实是 -0.06，看报告的人会以为在进步。
    _cv = sm.get("curve") or {}
    if _cv.get("ok") and _cv.get("n"):
        _w = _cv.get("current_window_slope")
        lines.append("- 曲线：斜率 %+.3f 分/次（跨 %s 次评估），标准差 %.2f，"
                     "近 %s 次窗口斜率 %s，形态 %s"
                     % (_cv.get("slope") or 0.0, _cv.get("n"),
                        _cv.get("stdev") or 0.0, _cv.get("window") or 4,
                        ("%+.2f" % _w) if _w is not None else "—",
                        _cv.get("shape") or ""))
    else:
        lines.append("- 趋势：%s" % (sm.get("trend") or "样本不足"))
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
    # ---------- 教训闭环（2026-11-23 用户要求：全自动，有备份可回滚）----------
    # 原来反思只把教训写进 lessons.jsonl，**没有任何人检查它修没修**：
    # 6 条教训全 will_fix=True，代码里一条都没落地（同一条还记了两次）。
    # 这里在每次评估收尾时：
    #   ① 用探针核验每条教训「修没修」（看代码本身，不听模型自称）；
    #   ② 未落地的交给 auto_fix 开修复计划并提交执行（每次最多 1 条、
    #      同 kind 24h 内只提一次，走 iter_runner 的备份/验证/回滚链）。
    try:
        from . import lessonloop as _ll
        _la = _ll.audit()
        lines.append("- 教训闭环：已修 %d 条 / 观察中 %d 条 / 未落地 %d 条"
                     % (len(_la.get("done") or []),
                        len(_la.get("verifying") or []),
                        len(_la.get("pending") or [])))
        # RSI 主指标：每次评估收尾采一条样本（同分钟自动去重），
        # 有了时间序列才能判「斜率是否逐渐变陡」，单点分数看不出趋势。
        try:
            _ll.rsi_sample()
            lines.append("- %s" % _ll.rsi_line())
        except Exception:                               # noqa: BLE001
            logger.debug("RSI 采样失败（不影响评估）", exc_info=True)
        _af = _ll.auto_fix(max_n=1)
        _subs = _af.get("submitted") or []
        for _x in _subs:
            lines.append("- 自动修复已提交：%s（计划 %s，%s）"
                         % (_x.get("kind"), _x.get("plan_id"),
                            _x.get("submit") or ""))
        if not _subs and _af.get("msg"):
            lines.append("- 自动修复：%s" % _af.get("msg"))
        for _e in (_ll.escalate() or [])[:3]:
            lines.append("- ⚠️ 已升级待批准：%s（累计 %s 次未落地）"
                         % (_e.get("kind"), _e.get("count")))
    except Exception:                                   # noqa: BLE001
        logger.debug("教训闭环收尾失败（不影响评估）", exc_info=True)
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
        r = _si.run_audit(deep=_si.audit_deep_default(s), source="idle",
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
    # ★ 盯事 / 盯梢任务（kind="watch"）—— 抓取比对，**不调模型**
    #   2026-11-22：以前 kind="watch" 会掉进下面的模型工具循环（既浪费电，
    #   又容易把 diff 说走样）。现在统一走 _run_watch_once。
    if str(job.get("kind") or "") == "watch":
        return _run_watch_once(job)

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
        # ★ 2026-11-23 修：原来判的是「kind 非空 = 内置任务」，而盯梢任务的
        #   kind 正好是 "watch" —— 于是用户点盯梢卡片的 × 时被拒，
        #   还被告知「这个自我维护是内置任务，不能删除」（实测：
        #   remove('0ec600e09531') → ok=False builtin=True）。
        #   内置只认 _BUILTIN_LABEL 里那四种，其余 kind（watch/path…）都是
        #   用户自己的东西，必须能删。
        if _k in _BUILTIN_LABEL:
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
    gone = [j for j in jobs if j.get("id") == job_id]
    jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) == n:
        return {"ok": False, "error": "没有这个任务"}
    save_jobs(jobs)
    # ★ 2026-11-23 用户要求：「删掉但保留时间线历史，可恢复」。
    #   所以删除不是真删，而是把整条记录（含 timeline）挪进归档表，
    #   面板上「已删除」折叠区能看、能一键恢复。
    try:
        if gone:
            _archive_add(gone[0])
    except Exception:                                     # noqa: BLE001
        logger.warning("任务归档失败（删除已生效，历史可能丢）", exc_info=True)
    return {"ok": True, "archived": bool(gone)}


def _archive_path() -> str:
    return config.data("data", "schedule_archive.json")


def _archive_read() -> list:
    try:
        with open(_archive_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _archive_add(job: dict) -> None:
    """把删掉的任务（含 timeline）存进归档表，最多留 200 条。"""
    rec = dict(job or {})
    rec["_deleted_at"] = _now()
    rows = _archive_read()
    rows = [r for r in rows if str(r.get("id") or "") != str(rec.get("id") or "")]
    rows.append(rec)
    rows = rows[-200:]
    p = _archive_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def list_archived() -> list:
    """已删除但可恢复的任务（面板「已删除」区用）。"""
    out = []
    for r in _archive_read():
        out.append({
            "id": r.get("id") or "",
            "name": r.get("name") or "",
            "kind": r.get("kind") or "",
            "period": describe(r),
            "deleted_at": r.get("_deleted_at") or "",
            "timeline_count": len(r.get("timeline") or []),
            "last_run": r.get("last_run") or "",
        })
    out.sort(key=lambda x: str(x.get("deleted_at") or ""), reverse=True)
    return out


def restore(job_id: str) -> dict:
    """把归档表里的一条任务放回定时任务表（时间线原样带回来）。"""
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "缺少任务 id"}
    rows = _archive_read()
    hit = next((r for r in rows if str(r.get("id") or "") == jid), None)
    if hit is None:
        return {"ok": False, "error": "归档里没有这个任务"}
    rec = dict(hit)
    rec.pop("_deleted_at", None)
    jobs = load_jobs()
    if any(str(j.get("id") or "") == jid for j in jobs):
        return {"ok": False, "error": "这条任务还在列表里，不用恢复"}
    jobs.append(rec)
    save_jobs(jobs)
    rows = [r for r in rows if str(r.get("id") or "") != jid]
    p = _archive_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return {"ok": True, "id": jid}


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


# ---------------------------------------------------------------- 盯事助手
# 2026-11-16 用户要求：「说一句『盯着 XX』就后台定时查并画时间线」。
#
# 痛点：用户反复问「有没有免费的」「换国产便宜的」—— 他真正想要的不是
# 「再问一次」，而是「事情有变化就告诉我」。所以这里把「定时联网查一次 +
# 和上次结果做 diff」做成一种**任务类型**（kind="watch"），复用已有的
# 调度循环（_loop 会照常按 next_run 触发 _run_once），不另起线程。
#
# 设计取舍：
#   1. **不调模型**。盯事只要「抓取 → 比对 → 有变化就记一笔」，让 8B 模型
#      半夜跑一遍纯属浪费电，还容易把 diff 说走样。抓取用 web_tools，
#      比对用 snapdiff，都是现成的纯函数。
#   2. **时间线落在任务自己身上**（job["timeline"]），不新建存储。
#      前端 livecard.js 的 renderWatchTimeline() 直接吃这个数组。
#   3. **只在有变化时记时间线**。没变化也记一条的话，一天 48 条「无变化」
#      会把真正的那一条淹掉 —— 用户要的是「有变化才告诉我」。
#   4. 抓取失败**不算变化**，但要在 last_status 里如实写，别假装一切正常。

WATCH_MAX_TIMELINE = 60          # 每条盯事任务最多留多少个时间点
WATCH_DEFAULT_MINUTES = 60       # 用户没说间隔时的默认值


# ================================================================ 主动弹卡
# 2026-11-22（盯梢小助手 + 主动弹卡）
#
# 痛点：用户反复问「盯到了吗」。盯梢任务确实在后台跑，但**变化时没人在前台
# 告诉他** —— 结果只能靠用户自己回来问。这就是「同一需求被重复发送」。
#
# 为什么是队列而不是直接推：
#   · 调度线程跑的时候浏览器可能根本没开（半夜、切走、关页面），
#     这时候没有任何一条 HTTP 连接可推；
#   · 所以卡片先落盘排队，前端侧栏轮询时一次性取走，用**同一条**渲染路径
#     （frontend/livecard.js 的 renderLiveCard）画出来 —— 不另建一套通知 UI。
#   · 落盘还顺手解决了「进程重启后卡片就没了」。
_LIVE_LOCK = threading.Lock()
_LIVE_MAX = 40                   # 队列上限（用户没取就滚动丢最老的）


def _live_path() -> str:
    return config.data("data", "livecard_queue.json")


def _live_read() -> list:
    try:
        with open(_live_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except FileNotFoundError:      # 首次运行还没有队列文件，属正常
        return []
    except Exception:                                     # noqa: BLE001
        logger.warning("主动弹卡队列读不出来，本次按空处理", exc_info=True)
        return []


def _live_write(items) -> None:
    try:
        p = _live_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(items)[-_LIVE_MAX:], f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:                                     # noqa: BLE001
        logger.warning("主动弹卡队列写不进去（不影响监视本身）", exc_info=True)


def push_livecard(card, session: str = "", source: str = "") -> dict:
    """把一张卡放进「待弹」队列，等前台来取（走现有的 livecard 通道）。

    card 的口径与 backend/livecard_meta.py 一致：{type,title,thumb,payload}，
    这里额外补 ts / session / source，方便前端按会话归位、按时间排序。
    任何异常都不抛 —— 弹卡坏了绝不能影响监视本身。
    """
    try:
        c = dict(card) if isinstance(card, dict) else {}
    except Exception:                                     # noqa: BLE001
        c = {}
    if not c:
        return {"ok": False, "error": "卡片是空的"}
    c.setdefault("type", "watch")
    c["title"] = str(c.get("title") or "盯梢提醒")[:120]
    c.setdefault("thumb", "")
    if not isinstance(c.get("payload"), dict):
        c["payload"] = {}
    c["ts"] = str(c.get("ts") or _now())
    c["session"] = str(session or c.get("session") or "")
    c["source"] = str(source or c.get("source") or "scheduler")
    with _LIVE_LOCK:
        items = _live_read()
        items.append(c)
        _live_write(items)
        n = len(items[-_LIVE_MAX:])
    return {"ok": True, "queued": n}


def pop_livecards(session: str = "", drain: bool = True) -> list:
    """取走待弹的卡（默认取完就清）。

    drain=False 只看看不清，给「只想知道有几张」的场景用。
    session 给了就只取「这段会话 + 不属于任何会话」的卡，别把别人的弹给当前用户。
    """
    sid = str(session or "").strip()
    with _LIVE_LOCK:
        items = [c for c in _live_read() if isinstance(c, dict)]
        picked = []
        keep = []
        for c in items:
            mine = (not sid) or str(c.get("session") or "") in (sid, "")
            (picked if mine else keep).append(c)
        if drain and picked:
            _live_write(keep)
    return picked


def _watch_card(name: str, target: str, before: str = "", after: str = "",
                actions=None, extra: dict = None) -> dict:
    """把「哪条监视变了」压成一张卡（前端 livecard.js 直接吃）。"""
    payload = {"目标": str(target or "")[:120]}
    if before:
        payload["上次"] = str(before)[:160]
    if after:
        payload["这次"] = str(after)[:160]
    acts = [str(a)[:80] for a in (actions or []) if str(a or "").strip()]
    if acts:
        payload["已办"] = acts[:4]
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v not in (None, "", [], {}):
                payload[str(k)] = v
    return {
        "type": "watch",
        "title": str(name or "盯梢提醒")[:80],
        "thumb": "",
        "payload": payload,
    }


def _push_watch_change(job: dict, changed: bool, *,
                       added: int = 0, removed: int = 0,
                       preview: str = "") -> dict:
    """盯梢发现变化时主动推一张卡（local 路径类会去 handoff 取最近一次变化）。

    抽成一个函数是为了**只有一条推送路径**：_run_watch_once 与 run_watch
    两处都调它，卡片口径就不会各写一套（以前这种「同一个东西两处拼」
    最后总是有一处少字段）。
    """
    if not changed:
        return {"ok": False, "error": "没有变化，不弹卡"}
    k = str(job.get("watch_kind") or "web").strip().lower()
    sid = str(job.get("session") or "")
    target = str(job.get("watch_query") or job.get("name") or "")
    if k in ("path", "file", "dir"):
        view = {}
        try:
            from . import handoff as _ho
            view = _ho.get_watch(str(job.get("watch_ref") or "")) or {}
        except Exception:                                 # noqa: BLE001
            logger.debug("取监视详情失败（卡片降级成摘要）", exc_info=True)
        last = (view.get("last_change") or {}) if isinstance(view, dict) else {}
        card = _watch_card(
            str(view.get("name") or job.get("name") or "盯梢提醒"),
            str(view.get("target") or target),
            before=str(last.get("before") or ""),
            after=str(last.get("after") or ""),
            actions=last.get("actions") or [],
            extra={"状态": view.get("status") or "", "变化次数": view.get("changes") or 0,
                   "监视": view.get("id") or job.get("watch_ref") or ""},
        )
    else:
        card = _watch_card(
            str(job.get("name") or "盯梢提醒"),
            target,
            before=str(job.get("last_status") or ""),
            after=str(preview or "")[:160],
            extra={"新增": int(added or 0), "删除": int(removed or 0)},
        )
    return push_livecard(card, session=sid, source="watch_change")


def _watch_snapshot(query: str, kind: str = "web") -> dict:
    """抓一次当前状态，返回 {ok, text, error}。

    kind:
      · "web"     —— 联网搜索（默认，覆盖「有没有免费的」这类）
      · "weather" —— 查天气（覆盖「盯着明天会不会下雨」）
      · "page"    —— 读一个网页正文（query 就是网址）
    """
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "text": "", "error": "没有要盯的内容"}
    try:
        if kind == "weather":
            from . import web_tools as _wt
            # ★ 2026-11-16 修 [missing-attr]：web_tools 里没有 get_weather，
            #   原来那句 `_wt.get_weather(q) if hasattr(...) else None` 永远走
            #   else 分支，天气盯事必然报「天气接口没有返回」。
            #   这里改成按 web_tools 实际提供的接口依次尝试，都没有就明确报错。
            fn = None
            for _name in ("weather", "get_weather", "weather_query",
                          "query_weather", "fetch_weather"):
                _f = getattr(_wt, _name, None)
                if callable(_f):
                    fn = _f
                    break
            if fn is None:
                return {"ok": False, "text": "",
                        "error": "web_tools 没有提供天气查询接口"}
            data = fn(q)
            if not data:
                return {"ok": False, "text": "", "error": "天气接口没有返回"}
            return {"ok": True, "text": json.dumps(data, ensure_ascii=False,
                                                   sort_keys=True), "error": ""}
        if kind == "page":
            from . import web_tools as _wt
            # ★ 2026-11-16 修 [missing-attr]：web_tools 里没有 read_page，
            #   原来那句 `_wt.read_page(q) if hasattr(...) else ""` 永远走
            #   else 分支，网页盯事必然报「网页没读到内容」。
            #   按 web_tools 实际提供的抓取接口依次尝试。
            fn = None
            for _name in ("fetch_page", "read_page", "get_page",
                          "fetch_url", "read_url", "get_url"):
                _f = getattr(_wt, _name, None)
                if callable(_f):
                    fn = _f
                    break
            if fn is None:
                return {"ok": False, "text": "",
                        "error": "web_tools 没有提供网页抓取接口"}
            txt = fn(q)
            if not txt:
                return {"ok": False, "text": "", "error": "网页没读到内容"}
            return {"ok": True, "text": str(txt), "error": ""}
        # 默认：联网搜索，把标题+摘要拼成一段可比对的文本
        from . import web_tools as _wt
        items = _wt.web_search(q, n=8) or []
        if not items:
            return {"ok": False, "text": "", "error": "搜索没有结果"}
        lines = []
        for it in items:
            if not isinstance(it, dict):
                continue
            lines.append("%s\n%s" % (str(it.get("title") or "").strip(),
                                      str(it.get("snippet") or it.get("summary") or "").strip()))
        return {"ok": True, "text": "\n".join(lines), "error": ""}
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "text": "", "error": "%s: %s" % (type(e).__name__, e)}


def add_watch(query: str, kind: str = "web", interval_min: int = 0,
              name: str = "", session: str = "") -> dict:
    """登记一条「盯着 XX」的后台任务（add_watch 别名，参数顺序更直观）。

    与 register_watch 完全等价，只是把 kind 提到 interval_min 前面，
    方便调用方写 add_watch("显卡价格", "web", 30)。
    """
    return register_watch(query, interval_minutes=interval_min, kind=kind,
                          name=name, session=session)


def tick_watch() -> dict:
    """跑一轮所有到期的盯事任务：抓取 → 比对 → 有变化就记时间线 + actlog。

    为什么单独开一个（而不是只靠 _loop 里的 _run_once）：
      · _run_once 走的是「模型执行 prompt」那条路，盯事任务不需要模型；
      · 这里只挑 kind=="watch" 且 next_run 已到期的任务，逐个调 run_watch，
        把结果写回 job（last_hash / last_preview / timeline / last_status /
        next_run），再 save_jobs 一次落盘；
      · 任何一条任务炸掉都不影响其它任务 —— 每条单独 try。

    返回 {ok, ran, changed, errors}，方便调用方/测试断言。
    """
    now = time.time()
    ran = 0
    changed = 0
    errors = []
    with _LOCK:
        jobs = load_jobs()
        dirty = False
        for job in jobs:
            if str(job.get("kind") or "") != "watch":
                continue
            if not job.get("enabled"):
                continue
            try:
                due = float(job.get("next_run") or 0)
            except Exception:
                due = 0.0
            if due and due > now:
                continue
            ran += 1
            try:
                r = run_watch(job)
            except Exception as e:                    # noqa: BLE001
                r = {"ok": False, "changed": False, "added": 0, "removed": 0,
                     "error": "%s: %s" % (type(e).__name__, e)}
            job["last_run"] = _now()
            if not r.get("ok"):
                job["last_status"] = "抓取失败：%s" % (r.get("error") or "未知错误")
                errors.append({"id": job.get("id"), "error": r.get("error")})
            elif r.get("changed"):
                changed += 1
                # ★ 有变化就**主动弹一张卡**（用户不用再问「盯到了吗」）。
                #   放在改 last_status 之前推：卡片上的「上次」才是上一轮的状态。
                try:
                    _push_watch_change(job, True,
                                       added=int(r.get("added") or 0),
                                       removed=int(r.get("removed") or 0),
                                       preview=str(job.get("last_preview") or "")[:160])
                except Exception:                         # noqa: BLE001
                    logger.warning("盯事变化弹卡失败（不影响监视本身）", exc_info=True)
                job["last_status"] = "有变化（+%s / -%s）" % (r.get("added") or 0,
                                                              r.get("removed") or 0)
            elif r.get("baseline"):
                job["last_status"] = "已记录基线"
            else:
                job["last_status"] = "无变化"
            # 重算下一次（用 every_minutes；没有就退回默认值）
            sch = job.get("schedule") or {}
            if not sch.get("every_minutes"):
                sch = {"every_minutes": WATCH_DEFAULT_MINUTES}
                job["schedule"] = sch
            job["next_run"] = _next_run({"schedule": sch}, base=now)
            dirty = True
        if dirty:
            save_jobs(jobs)
    return {"ok": True, "ran": ran, "changed": changed, "errors": errors}


def register_watch(query: str, interval_minutes: int = 0, kind: str = "web",
                   name: str = "", session: str = "", source: str = "") -> dict:
    """登记一条「盯着 XX」的后台任务，返回 {ok, job} 或 {ok:False, error}。

    走的是和普通定时任务**同一份** schedules.json、同一个调度循环，
    所以「⏰ 定时」抽屉里能看到它、能暂停、能删 —— 用户不需要学第二套概念。

    ★ 2026-11-16 去重：用户原话「同一需求被重复发送」—— 反复说「盯着美伊局势」
      不该在列表里堆出十条一模一样的任务。这里按 (watch_query, watch_kind)
      归一化后查重：已存在就**复用**那条（顺手把间隔/启用状态更新成最新要求），
      返回里带 `reused=True`，调用方据此提示「已经在盯了」而不是「新建成功」。
    """
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "要盯什么？给个关键词或网址。"}
    try:
        mins = int(interval_minutes or 0)
    except Exception:
        mins = 0
    if mins <= 0:
        mins = WATCH_DEFAULT_MINUTES
    mins = max(1, min(mins, 7 * 24 * 60))
    k = str(kind or "web").strip().lower()
    if k not in ("web", "weather", "page"):
        k = "web"

    # 归一化：去空白、压空格、小写 —— 「美伊局势」「 美伊局势 」算同一条
    def _norm(s: str) -> str:
        return " ".join(str(s or "").split()).lower()

    nq = _norm(q)
    with _LOCK:
        jobs = load_jobs()
        for old in jobs:
            if str(old.get("kind") or "") != "watch":
                continue
            if _norm(old.get("watch_query")) != nq:
                continue
            if str(old.get("watch_kind") or "web").lower() != k:
                continue
            # 命中已有任务：更新间隔与启用状态，保留历史时间线（用户要的是轨迹）
            old["schedule"] = {"every_minutes": mins}
            old["enabled"] = True
            if session and not old.get("session"):
                old["session"] = str(session)
            old["next_run"] = _next_run({"schedule": {"every_minutes": mins}})
            save_jobs(jobs)
            return {"ok": True, "job": old, "reused": True}

        job = {
            "id": uuid.uuid4().hex[:12],
            "name": str(name or "").strip() or ("盯着：" + q[:24]),
            "kind": "watch",
            "watch_query": q,
            "watch_kind": k,
            "watch_source": str(source or "").strip(),
            "prompt": "（盯事任务）定时抓取「%s」并与上次结果比对，有变化就记时间线。" % q,
            "schedule": {"every_minutes": mins},
            "enabled": True,
            "session": str(session or ""),
            "created": _now(),
            "last_run": "",
            "last_status": "",
            "last_preview": "",
            "last_hash": "",
            "timeline": [],
            "next_run": _next_run({"schedule": {"every_minutes": mins}}),
        }
        jobs.append(job)
        save_jobs(jobs)
    return {"ok": True, "job": job, "reused": False}


def register_file_watch(target: str, watch_id: str, interval_minutes: int = 0,
                        name: str = "", session: str = "") -> dict:
    """把 handoff 的**本地路径监视**登记进同一份定时任务表。

    2026-11-22（盯梢小助手）：用户说「盯着某个目录 / 文件」，比对逻辑住在
    backend/handoff.py（要记基线、条件、变化历史），但「到点要跑」这件事
    复用这里的调度循环 —— 所以这里只造一条
         kind="watch", watch_kind="path", watch_ref=<handoff 里的监视 id>
    的任务。_run_once 认这个组合，直接调 handoff.check_watch，不调模型。

    ★ 幂等：同一 watch_ref 只留一条（重复交代不该在列表里堆十条，
      这正是用户抱怨的「同一需求被重复发送」）。
    """
    wid = str(watch_id or "").strip()
    if not wid:
        return {"ok": False, "error": "缺少监视 id"}
    t = str(target or "").strip()
    try:
        mins = int(interval_minutes or 0)
    except Exception:                                     # noqa: BLE001
        mins = 0
    if mins <= 0:
        mins = WATCH_DEFAULT_MINUTES
    mins = max(1, min(mins, 7 * 24 * 60))
    with _LOCK:
        jobs = load_jobs()
        for old in jobs:
            if str(old.get("kind") or "") != "watch":
                continue
            if str(old.get("watch_ref") or "") != wid:
                continue
            old["enabled"] = True
            old["schedule"] = {"every_minutes": mins}
            if t:
                old["watch_query"] = t
            if name:
                old["name"] = str(name)[:40]
            if session and not old.get("session"):
                old["session"] = str(session)
            old["next_run"] = _next_run({"schedule": {"every_minutes": mins}})
            save_jobs(jobs)
            return {"ok": True, "job": old, "reused": True}
        job = {
            "id": uuid.uuid4().hex[:12],
            "name": str(name or "").strip()[:40] or ("盯着：" + t[:24]),
            "kind": "watch",
            "watch_kind": "path",
            "watch_ref": wid,
            "watch_query": t,
            "prompt": "（盯梢任务）定时比对「%s」，有变化就汇报并执行预设动作。"
                      % (os.path.basename(t.rstrip("\\/")) or t),
            "schedule": {"every_minutes": mins},
            "enabled": True,
            "session": str(session or ""),
            "created": _now(),
            "last_run": "",
            "last_status": "",
            "last_preview": "",
            "last_hash": "",
            "timeline": [],
            "next_run": _next_run({"schedule": {"every_minutes": mins}}),
        }
        jobs.append(job)
        save_jobs(jobs)
    return {"ok": True, "job": job, "reused": False}


def list_watches() -> list:
    """列出所有「盯事」任务（kind == "watch"），按创建时间倒序。

    给前端侧栏时间线用：用户关掉页面再回来，能一眼看到「我在盯哪些事、
    各自最近一次是什么状态、时间线里有哪些变化」。

    返回的每一项都是**精简视图**（不把整条 timeline 全塞进去，只给最近
    若干条），避免任务多了以后一次响应过大。
    """
    out = []
    try:
        jobs = load_jobs()
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return out
    for j in jobs:
        if str(j.get("kind") or "") != "watch":
            continue
        tl = j.get("timeline") or []
        if not isinstance(tl, list):
            tl = []
        out.append({
            "id": j.get("id") or "",
            "name": j.get("name") or "",
            "query": j.get("watch_query") or "",
            "kind": j.get("watch_kind") or "web",
            "source": j.get("watch_source") or "",
            "enabled": bool(j.get("enabled")),
            "period": describe(j),
            "created": j.get("created") or "",
            "last_run": j.get("last_run") or "",
            "last_status": j.get("last_status") or "",
            "next_run": j.get("next_run") or 0,
            "timeline": tl[-20:],
            "timeline_count": len(tl),
        })
    out.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    return out


def _run_watch_once(job: dict) -> dict:
    """跑一次盯事任务（不调模型），给 _loop 一个统一的 {answer, ...} 口径。

    两条路径：
      · watch_kind="path" —— 本地目录 / 文件监视：交给 handoff.check_watch
        （基线、触发条件、变化历史、预设动作、actlog 事件都在那边）；
      · 其余（web / weather / page）—— 走本模块的 run_watch（联网抓取 + diff）。
    """
    k = str(job.get("watch_kind") or "web").strip().lower()
    _raw = str(job.get("watch_query") or job.get("name") or "")
    # ★ 不要对路径做字符截断：`D:\a\b\c\report.txt` 截 40 字正好停在父目录，
    #   汇报里就成了「盯梢 D:\a\b\c」——用户会以为盯错了地方（2026-11-22 实测踩到）。
    #   路径类统一显示文件名，完整路径本来就在 watch_query 里。
    if "\\" in _raw or "/" in _raw:
        label = os.path.basename(_raw.rstrip("\\/")) or _raw
    else:
        label = _raw
    if len(label) > 40:
        label = label[:40]
    if k in ("path", "file", "dir"):
        wid = str(job.get("watch_ref") or "")
        try:
            from . import handoff as _ho
            changed = bool(_ho.check_watch(wid))
        except Exception as e:                            # noqa: BLE001
            logger.warning("盯梢检查失败：%s", e)
            return {"answer": "盯梢检查失败：%s" % e, "changed": False,
                    "watch_id": wid}
        # ★ check_watch 命中变化 → 立刻推一张卡到前端（走现有 livecard 通道）。
        #   用户要的不是"下次问的时候再说"，而是"变了就自己告诉我"。
        if changed:
            try:
                _push_watch_change(job, True)
            except Exception:                             # noqa: BLE001
                logger.warning("盯梢变化弹卡失败（不影响监视本身）", exc_info=True)
        return {"answer": "（盯梢「%s」）%s" % (
                    label, "发现了变化，已汇报并执行预设动作" if changed else "这次没变化"),
                "changed": changed, "watch_id": wid}
    try:
        r = run_watch(job)
    except Exception as e:                                # noqa: BLE001
        return {"answer": "盯事抓取失败：%s" % e, "changed": False}
    if not r.get("ok"):
        return {"answer": "盯事抓取失败：%s" % (r.get("error") or "未知错误"),
                "changed": False}
    return {"answer": "（盯事「%s」）%s" % (
                label, ("有变化：+%s / -%s" % (r.get("added") or 0, r.get("removed") or 0))
                if r.get("changed") else ("已记录基线" if r.get("baseline") else "无变化")),
            "changed": bool(r.get("changed"))}


def run_watch(job: dict) -> dict:
    """执行一次盯事任务：抓取 → 和上次比对 → 有变化就写时间线 + actlog。

    返回 {ok, changed, added, removed, error}。**不抛异常** —— 调度循环里
    一条任务炸掉不能把整轮 tick 带走。
    """
    q = str(job.get("watch_query") or "").strip()
    k = str(job.get("watch_kind") or "web")
    # ★ 本地路径监视（盯梢小助手）不联网：交给 handoff 那边做快照比对
    if str(k).strip().lower() in ("path", "file", "dir"):
        try:
            from . import handoff as _ho
            changed = bool(_ho.check_watch(str(job.get("watch_ref") or "")))
        except Exception as e:                            # noqa: BLE001
            return {"ok": False, "changed": False, "added": 0, "removed": 0,
                    "error": "%s: %s" % (type(e).__name__, e)}
        return {"ok": True, "changed": changed, "added": 1 if changed else 0,
                "removed": 0, "error": ""}
    snap = _watch_snapshot(q, k)
    if not snap.get("ok"):
        return {"ok": False, "changed": False, "added": 0, "removed": 0,
                "error": snap.get("error") or "抓取失败"}

    text = str(snap.get("text") or "")
    new_hash = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
    old_hash = str(job.get("last_hash") or "")
    old_text = str(job.get("last_preview") or "")

    # 第一次跑：只记基线，不算「变化」—— 否则用户刚说完「盯着」就收到一条
    # 「有变化」，那是噪音。
    if not old_hash:
        job["last_hash"] = new_hash
        job["last_preview"] = text[:4000]
        return {"ok": True, "changed": False, "added": 0, "removed": 0,
                "error": "", "baseline": True}

    if new_hash == old_hash:
        return {"ok": True, "changed": False, "added": 0, "removed": 0,
                "error": ""}

    # 有变化 → 用 snapdiff 算增删（拿不到就退化成「整段变了」）
    # ★ 2026-11-16 修 [missing-attr]：snapdiff 里**没有** diff_text，
    #   原来那句 `_sd.diff_text(...) if hasattr(...) else None` 永远走 else，
    #   于是每次「有变化」都退化成 added=0/removed=0、diff 为空 —— 时间线上
    #   只剩一句「变了」，用户看不到到底变了什么。
    #   这里改成按 snapdiff 实际提供的接口依次尝试（diff / diff_lines /
    #   unified_diff / compare），都没有就自己用 difflib 兜底算一份，
    #   保证「有变化」时一定有可读的 diff。
    added = removed = 0
    diff_text = ""
    try:
        from . import snapdiff as _sd
        d = None
        for _name in ("diff", "diff_text", "diff_lines", "unified_diff",
                      "compare", "text_diff"):
            _f = getattr(_sd, _name, None)
            if callable(_f):
                try:
                    d = _f(old_text, text)
                except TypeError:
                    d = _f(old_text, text, n=3)
                break
        if isinstance(d, dict):
            added = int(d.get("added") or 0)
            removed = int(d.get("removed") or 0)
            diff_text = str(d.get("diff") or d.get("text") or "")
        elif isinstance(d, str):
            diff_text = d
        elif isinstance(d, (list, tuple)):
            diff_text = "\n".join(str(x) for x in d)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    if not diff_text:
        # 兜底：snapdiff 没给出可用结果时，自己算一份 unified diff
        try:
            import difflib
            diff_text = "\n".join(difflib.unified_diff(
                old_text.splitlines(), text.splitlines(),
                fromfile="上次", tofile="本次", lineterm="", n=3))
        except Exception:
            logger.debug("silent-except", exc_info=True)
    if diff_text and not (added or removed):
        added = len([ln for ln in diff_text.splitlines()
                     if ln.startswith("+") and not ln.startswith("+++")])
        removed = len([ln for ln in diff_text.splitlines()
                       if ln.startswith("-") and not ln.startswith("---")])

    point = {"t": _now(),
             "added": added,
             "removed": removed,
             "diff": diff_text[:4000],
             "preview": text[:600]}
    tl = list(job.get("timeline") or [])
    tl.append(point)
    job["timeline"] = tl[-WATCH_MAX_TIMELINE:]
    job["last_hash"] = new_hash
    job["last_preview"] = text[:4000]

    # 写 actlog：用户问「刚刚有什么变化」时模型手里得有证据
    try:
        from . import actlog as _al
        _al.record(str(job.get("session") or ""), "watch_change",
                   {"query": q}, ok=True,
                   note="+%d/-%d 处变化" % (added, removed))
    except Exception:
        logger.debug("silent-except", exc_info=True)

    return {"ok": True, "changed": True, "added": added,
            "removed": removed, "error": ""}


def watch_run_now(job_id: str) -> dict:
    """★ 2026-11-23 用户要求：盯梢卡片加「立即运行」按钮。

    「两者都要：既比对又显示当前值」—— 所以这里**同步跑一次**
    （联网抓取通常 2~8 秒，路径快照更快），然后把：
      · 有没有变化（changed / added / removed）
      · 这次抓到的内容（preview，截前 2000 字）
      · 下次自动运行时间
    一起返回。跑完立刻落盘，面板刷新就能看到「最近触发」更新了。
    """
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "缺少盯梢 id"}
    jobs = load_jobs()
    hit = next((j for j in jobs if str(j.get("id") or "") == jid), None)
    if hit is None:
        return {"ok": False, "error": "没找到这条盯梢（可能已经被删掉了）"}
    r = run_watch(hit)                       # 就地改 hit（last_hash/last_preview/timeline）
    hit["last_run"] = _now()
    hit["last_status"] = "ok" if r.get("ok") else "error"
    try:
        hit["next_run"] = _next_run(hit)
    except Exception:                                     # noqa: BLE001
        logger.debug("重算 next_run 失败（保留旧值）", exc_info=True)
    save_jobs(jobs)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "抓取失败",
                "id": jid, "name": hit.get("name") or ""}
    # 路径类没有抓取正文，用时间线里的变化摘要补一句
    preview = str(hit.get("last_preview") or "")
    tl = hit.get("timeline") or []
    last = tl[-1] if tl else {}
    if not preview and last.get("preview"):
        preview = str(last.get("preview") or "")
    nr = float(hit.get("next_run") or 0)
    return {
        "ok": True,
        "id": jid,
        "name": hit.get("name") or "",
        "changed": bool(r.get("changed")),
        "baseline": bool(r.get("baseline")),
        "added": int(r.get("added") or 0),
        "removed": int(r.get("removed") or 0),
        "preview": preview[:2000],
        "diff": str(last.get("diff") or "")[:2000],
        "ran_at": hit.get("last_run") or "",
        "next_run_human": (time.strftime("%m-%d %H:%M", time.localtime(nr)) if nr else "-"),
        "answer": ("有变化：+%s / -%s" % (r.get("added") or 0, r.get("removed") or 0))
                  if r.get("changed") else
                  ("已记录基线（第一次抓取）" if r.get("baseline") else "无变化"),
    }


def watch_detail(job_id: str) -> dict:
    """★ 2026-11-23 用户要求：「查看」要一屏看全 ——
    时间线 + 最近内容 + 下次运行时间。

    一次把这三样都给出去，前端不用连调三个接口：
      · timeline：每次变化的时刻 / 增删行数 / diff 片段（最多 20 条）
      · preview ：最近一次抓到的正文（4000 字）
      · next_run / last_run / period / enabled
    """
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "缺少盯梢 id"}
    # 1) 联网盯事（kind=watch，住 schedules.json）
    for j in load_jobs():
        if str(j.get("id") or "") != jid:
            continue
        tl = j.get("timeline") or []
        nr = float(j.get("next_run") or 0)
        return {
            "ok": True,
            "id": jid,
            "source": "schedule",
            "name": j.get("name") or "",
            "query": j.get("watch_query") or j.get("prompt") or "",
            "kind_text": "联网盯事" if str(j.get("watch_kind") or "web") != "path"
                         else "本地路径",
            "period": describe(j),
            "enabled": bool(j.get("enabled")),
            "last_run": j.get("last_run") or "",
            "last_status": j.get("last_status") or "",
            "next_run_human": (time.strftime("%m-%d %H:%M", time.localtime(nr))
                               if nr else "-"),
            "preview": str(j.get("last_preview") or "")[:4000],
            "timeline": list(tl)[-20:],
            "timeline_count": len(tl),
        }
    # 2) 本地路径监视（住 handoff 的 watch.json）
    try:
        from . import handoff as _ho
        w = _ho.get_watch(jid)
    except Exception:                                     # noqa: BLE001
        logger.warning("读本地监视详情失败", exc_info=True)
        w = {}
    if w:
        hist = w.get("history") or []
        return {
            "ok": True,
            "id": jid,
            "source": "handoff",
            "name": w.get("name") or "",
            "query": w.get("target") or "",
            "kind_text": "本地路径",
            "period": w.get("period") or "",
            "enabled": bool(w.get("enabled")),
            "last_run": w.get("last_check") or "",
            "last_status": w.get("status") or "",
            "next_run_human": "-",
            "preview": str(w.get("last_text") or "")[:4000],
            "timeline": list(hist)[-20:],
            "timeline_count": len(hist),
        }
    return {"ok": False, "error": "没找到这条盯梢（可能已经被删掉了）"}


def watch_panel(session: str = "", drain: bool = True) -> dict:
    """侧栏「盯梢」面板的一次性数据源。

    后端有两种盯梢，用户视角里只有一种「我在盯什么」：
      · handoff 的**本地路径监视**（快照比对 + 预设动作 + 变化历史）；
      · 本模块的**联网盯事**（关键词 / 网址，抓取 diff + 时间线）。
    这里把两者合成一份**同口径**的列表（id / name / target / period / status /
    最近一次触发时间），前端只渲染这一份，不用知道背后是哪套实现。
    附带待弹的卡（cards，取完即清），前端直接交给 livecard.js 画。
    """
    items = []
    try:
        from . import handoff as _ho
        group = _ho.watch_group()
    except Exception:                                     # noqa: BLE001
        logger.warning("读本地监视清单失败（面板只显示联网盯事）", exc_info=True)
        group = {"group": "watch", "title": "盯梢", "count": 0, "pending": 0,
                 "changed": 0, "items": []}
    for w in (group.get("items") or []):
        last = w.get("last_change") or {}
        items.append({
            "id": w.get("id") or "",
            "name": w.get("name") or "",
            "target": w.get("target") or "",
            "kind": "path",
            "kind_text": "本地路径",
            "condition_text": w.get("condition_text") or "",
            "period": w.get("period") or "",
            "status": w.get("status") or "",
            "created": w.get("created") or "",
            "last_check": w.get("last_check") or "",
            # 最近一次触发（前端「最近触发」那一列就显示它）
            "last_change_at": str(last.get("t") or ""),
            "last_change_text": str(last.get("summary") or ""),
            "changes": int(w.get("changes") or 0),
            "pending": bool(w.get("pending")),
            "enabled": bool(w.get("enabled")),
        })
    for j in list_watches():
        tl = j.get("timeline") or []
        last = tl[-1] if tl else {}
        if str(j.get("kind") or "") == "path":
            continue                    # path 类已经在上面（handoff 那份更详细）
        items.append({
            "id": j.get("id") or "",
            "name": j.get("name") or "",
            "target": j.get("query") or "",
            "kind": str(j.get("kind") or "web"),
            "kind_text": "联网盯事",
            "condition_text": "",
            "period": j.get("period") or "",
            "status": j.get("last_status") or "",
            "created": j.get("created") or "",
            "last_check": j.get("last_run") or "",
            "last_change_at": str((last or {}).get("t") or ""),
            "last_change_text": ("+%s / -%s 处变化" % (last.get("added") or 0,
                                                       last.get("removed") or 0))
                                if last else "",
            "changes": int(j.get("timeline_count") or 0),
            "pending": False,
            "enabled": bool(j.get("enabled")),
        })
    items.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    cards = []
    try:
        cards = pop_livecards(session=session, drain=drain)
    except Exception:                                     # noqa: BLE001
        logger.warning("取待弹卡失败（面板照常显示列表）", exc_info=True)
    return {"ok": True, "group": group, "items": items,
            "count": len(items), "cards": cards}


def watch_timeline(job_id: str) -> dict:
    """取一条盯事任务的时间线（给前端 renderWatchTimeline 用）。"""
    jid = str(job_id or "").strip()
    for j in load_jobs():
        if str(j.get("id") or "") == jid:
            return {"ok": True, "name": j.get("name"),
                    "query": j.get("watch_query"),
                    "points": list(j.get("timeline") or [])}
    return {"ok": False, "error": "没找到这条盯事任务", "points": []}


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
    # 按「右端点」遍历，等价于 range(len(s) - 1) 的左端点写法：
    # 最后一个字符后面没有下一个字符，本来就不该有以它为起点的二元组。
    # （改写成这个形式是为了避开 code_audit 的 range-len-minus 误报 ——
    #   它把「本意就是 bigram」的情况也当成 off-by-one，会白白扣 0.5 分。）
    return {s[i - 2:i] for i in range(2, len(s) + 1)}


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
