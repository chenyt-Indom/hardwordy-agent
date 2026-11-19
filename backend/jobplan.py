# -*- coding: utf-8 -*-
"""一句话订好整件事：目标 → 多步任务 → 逐步跑完 → 成品打成一个包。

为什么要有（2026-11-21）
------------------------
用户的真实用法是「查天气 → 查路线 → 写行程 → 做 PPT」四件事分开交代：
每件事都要重新开一次口，中间的衔接全靠人来做调度。这是摩擦点
「同一需求被重复发送」里最贵的一种 —— 不是重复说同一句话，而是**把一件事
拆成四次任务发**。这里补上那个调度：用户说一句话，我们拆步、顺序跑完、
把成品打成一个 zip 给下载链接。

谁做什么（刻意分开，谁都不许越界）
--------------------------------
  · 本模块：拆步（plan_steps）、任务落盘与执行（run_job）、打包（pack_zip）。
    **不重新实现任何工具** —— 干活一律 `tools.job_step()` → `tools.dispatch()`。
  · backend/tools.py：`job_step()` 只负责「按 tool 名分发到已有函数并回填结果」。
  · backend/main.py：`plan_and_run()` + 两个 HTTP 接口（前端轮询进度 / 下 zip）。
  · frontend/jobplan.js：侧栏进度条 + 跑完给下载按钮。

拆步靠 LLM，但**绝不只有 LLM**
-----------------------------
本地模型没开、云端没配 key、模型返回的不是 JSON —— 这三种都会发生（离线是本
产品的常态）。所以 `plan_steps` 带一份**规则兜底**：认得出「天气 / 路线 / 文档 /
PPT」就把对应步骤排进去。兜底不是"降级凑数"：它保证用户这句话一定跑得出东西，
而不是弹一句「拆解失败请重试」。

状态口径（重要，别把 status 当成败）
-----------------------------------
  · step['status']：pending / running / done / error —— 只表示**这一步跑过了没有**。
  · step['ok']：这一步的工具自己有没有报错（例如没配高德 key 查不了天气）。
为什么必须分开：天气查失败**不该拦住**后面的行程文档和 PPT（那才是主产出），
但用户又必须看得见「天气这步没数据」。所以跑完了就是 done，好坏另记 ok；
任务整体再给一个 status：全 done → done，有步骤没跑成 → partial。
"""

import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import zipfile

from . import config

logger = logging.getLogger(__name__)

# 步骤 / 任务状态
PENDING, RUNNING, DONE, ERROR = "pending", "running", "done", "error"

# 这一步只跑这四个工具 —— 编排器**不是**万能工具台：范围之外的活交给对话链路，
# 让模型自己决定用哪个工具；硬塞进来只会让拆解变得不可控。
STEP_TOOLS = {
    "get_weather": "查天气",
    "map_plan": "查路线",
    "make_docx": "写文档",
    "make_pptx": "做 PPT",
}

# 模型 / 前端可能给各种叫法，统一收敛成工具名（大小写、中英文、简称都认）
TOOL_CANON = {
    "get_weather": "get_weather", "weather": "get_weather", "天气": "get_weather",
    "查天气": "get_weather", "getweather": "get_weather",
    "map_plan": "map_plan", "map": "map_plan", "route": "map_plan",
    "路线": "map_plan", "查路线": "map_plan", "行程": "map_plan",
    "make_docx": "make_docx", "docx": "make_docx", "word": "make_docx",
    "文档": "make_docx", "写文档": "make_docx", "report": "make_docx",
    "make_pptx": "make_pptx", "pptx": "make_pptx", "ppt": "make_pptx",
    "演示文稿": "make_pptx", "幻灯片": "make_pptx", "slides": "make_pptx",
}

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def canon_tool(name) -> str:
    """把各种叫法收敛成工具名；认不出来返回空串（调用方据此报错，不猜）。"""
    k = str(name or "").strip().lower()
    if not k:
        return ""
    return TOOL_CANON.get(k, k if k in STEP_TOOLS else "")


# ---------------------------------------------------------------- 落盘
def _dir() -> str:
    """任务目录：<可写数据根>/data/jobs。"""
    d = config.data("data", "jobs")      # config.data 顺带确保 <数据根>/data 在
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                    # noqa: BLE001
        logger.warning("建任务目录失败：%s", d, exc_info=True)
    return d


def new_job_id() -> str:
    """job_20261121_183000_a1b2c3 —— 只用字母数字下划线，能直接当文件名。"""
    return "job_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), secrets.token_hex(3))


def _safe_id(job_id) -> str:
    return _ID_RE.sub("", str(job_id or ""))[:64]


def _path(job_id) -> str:
    return os.path.join(_dir(), _safe_id(job_id) + ".json")


def save_job(job) -> dict:
    """把 job 整体写盘（先写临时文件再替换，别让前端读到半个 JSON）。"""
    jid = _safe_id((job or {}).get("id"))
    if not jid:
        raise ValueError("job 缺少 id")
    job["id"] = jid
    job["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = _path(jid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return job


def load_job(job_id) -> dict:
    """读一个任务；没有 / 坏了都返回 {}（前端据此提示「没有这个任务」）。"""
    p = _path(job_id)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            job = json.load(f)
        return job if isinstance(job, dict) else {}
    except Exception:                                    # noqa: BLE001
        logger.warning("任务文件读不出来：%s", p, exc_info=True)
        return {}


def list_jobs(limit: int = 10) -> list:
    """最近的任务（新的在前）—— 给「上次那个还没下」留个入口。"""
    d = _dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for n in names:
        j = load_job(n[:-5])
        if j:
            out.append(public_job(j))
    out.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    return out[:max(1, int(limit or 10))]


def public_job(job) -> dict:
    """给前端的精简视图：每步只留**够画进度条**的字段。

    result 可能是一整篇文档正文（几十 KB），轮询每秒拉一次会很蠢 —— 所以截断；
    完整内容在 zip 里，前端要看原文就去下载。
    """
    steps = []
    for st in (job or {}).get("steps") or []:
        steps.append({
            "id": st.get("id"),
            "tool": st.get("tool"),
            "title": st.get("title"),
            "status": st.get("status") or PENDING,
            "ok": st.get("ok"),
            "error": str(st.get("error") or "")[:300],
            "secs": st.get("secs") or 0,
            "files": list(st.get("files") or []),
            "brief": str(st.get("result") or "")[:600],
        })
    total = len(steps)
    finished = len([s for s in steps if s["status"] in (DONE, ERROR)])
    return {
        "id": job.get("id"),
        "goal": job.get("goal"),
        "created": job.get("created"),
        "updated": job.get("updated"),
        "status": job.get("status") or PENDING,
        "ok": job.get("ok"),
        "steps": steps,
        "total": total,
        "finished": finished,
        "percent": int(round(100.0 * finished / total)) if total else 0,
        "zip_ready": bool(job.get("zip_path") and os.path.isfile(str(job.get("zip_path")))),
        # 成品包的本地绝对路径。前端**不直接用**（走 /api/job/zip 下载，别把本机
        # 路径当 URL），但脚本 / 自检要能就地解压验证，所以照实给出来。
        "zip_path": str(job.get("zip_path") or ""),
        "zip_files": list(job.get("zip_files") or []),
        "warnings": list(job.get("warnings") or []),
    }


# ---------------------------------------------------------------- 拆步
_PLAN_PROMPT = """你是任务编排器。把用户这一句需求拆成**按顺序执行**的几步。

只能用下面四个工具（tool 字段照抄，不许造新的）：
  get_weather  查某地未来几天天气   {"city": "汕头", "days": 3}
  map_plan     查路线 / 周边 / 行程点   {"city": "汕头", "days": 2}
  make_docx    写一份 Word 文档     {"title": "汕头两日游行程", "filename": "汕头两日游行程.docx",
                                    "blocks": [{"type": "heading", "level": 1, "text": "标题"},
                                               {"type": "para", "text": "正文…"}]}
  make_pptx    做一份 PPT           {"title": "汕头两日游", "slides": [{"title": "第一天",
                                    "bullets": ["要点一", "要点二"]}]}

每一步的字段：tool / args / depends_on / title
  depends_on 是**上游步骤的序号数组**（例如 [1, 2]），表示这步要等前几步跑成才做。
  真的要依赖才写；写文档这种「上游没数据也能靠常识写」的步骤就别依赖，
  否则上游一失败，用户最想要的成品会被连着跳过。没有依赖就写 []。

规则：
1. 3~5 步；顺序符合依赖（先查数据，再写成品）；
2. 只排真正需要的步骤 —— 用户只要文档就别硬加 PPT；
3. blocks / slides 不许留空，内容写短一点没关系，后面会自动补；
4. **只输出 JSON 数组**，不要解释、不要 ``` 围栏。

用户需求："""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_ARR = re.compile(r"\[.*\]", re.S)


def _ask_text(prompt: str, llm=None, timeout: float = 60.0) -> str:
    """向模型要一段文本。**任何异常 / 超时都吞掉**，返回空串。

    ⚠️ 必须带超时：拆步是任务的入口，模型卡住时用户看到的是「点了没反应」。
    超时后走规则兜底（兜底保证一定有步骤可跑），比干等强。
    """
    fn = llm if callable(llm) else _default_llm()
    if not callable(fn):
        return ""
    box = {}

    def _run():
        try:
            box["text"] = fn(prompt)
        except Exception as exc:                          # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_run, name="jobplan-llm", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning("拆步调用超过 %.0f 秒，改用规则兜底", timeout)
        return ""
    if box.get("err") is not None:
        logger.warning("拆步调用失败：%s", box["err"])
        return ""
    v = box.get("text")
    if isinstance(v, (list, dict)):
        # 注入方（上层复用 / 自动化验收）直接给结构化步骤时别再要求它转字符串
        return json.dumps(v, ensure_ascii=False)
    return str(v or "")


def _default_llm():
    """默认的模型调用：本地 Ollama 通道（拿不到就返回 None → 走规则兜底）。"""
    try:
        cfg = config.load_config() or {}
    except Exception:                                    # noqa: BLE001
        return None
    model = str(cfg.get("default_model") or "").strip()
    if not model or model.lower().startswith("deepseek"):
        # 云端模型不走 Ollama 通道（与 main.py 里同款判断）。没配 key 时调用必然失败，
        # 与其白等一次网络超时，不如直接交给规则兜底。
        return None

    def _call(prompt: str) -> str:
        from . import ollama_client
        params = {"temperature": 0.2, "max_tokens": 900,
                  "num_ctx": int(cfg.get("num_ctx") or 8192)}
        resp = ollama_client.OllamaClient().chat(
            [{"role": "user", "content": prompt}], model=model,
            stream=False, params=params)
        data = resp.json() if hasattr(resp, "json") else resp
        return str(((data or {}).get("message") or {}).get("content") or "")

    return _call


def _parse_steps(text: str) -> list:
    """从模型输出里掏出步骤数组。**不抛异常**；掏不出返回 []。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    m = _FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    arr = _ARR.search(raw)
    data = None
    for cand in (raw, arr.group(0) if arr else ""):
        if not cand:
            continue
        try:
            data = json.loads(cand)
            break
        except Exception:                                # noqa: BLE001
            continue
    if isinstance(data, dict):
        data = data.get("steps") or data.get("plan") or []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        st = _norm_step(item, len(out) + 1)
        if st:
            out.append(st)
    return out


def _norm_step(item, idx: int) -> dict:
    """把一条（模型给的 / 前端给的）步骤收敛成内部结构；认不出工具返回 None。"""
    if not isinstance(item, dict):
        return None
    tool = canon_tool(item.get("tool") or item.get("name") or item.get("type"))
    if not tool:
        return None
    args = item.get("args")
    if args is None:
        args = item.get("arguments")
    if args is None:
        args = item.get("params")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:                                # noqa: BLE001
            args = {}
    if not isinstance(args, dict):
        args = {}
    title = str(item.get("title") or item.get("desc") or "").strip()
    # depends_on：这一步必须等哪几步跑成（步骤序号）。**只在调用方/模型显式给了
    # 的时候才记**，默认空 —— 默认成"依赖上一步"会让一次失败把后面的文档/PPT
    # 全连带跳过（天气查不到不该拦住成品，那才是用户真正要的东西）。
    deps = []
    raw_dep = item.get("depends_on")
    if raw_dep is None:
        raw_dep = item.get("after")
    if raw_dep is not None and not isinstance(raw_dep, (list, tuple)):
        raw_dep = [raw_dep]
    for d in (raw_dep or []):
        try:
            v = int(str(d).strip())
        except Exception:                                # noqa: BLE001
            continue
        if 0 < v < idx and v not in deps:                # 只认上游，防自己依赖自己
            deps.append(v)
    return {
        "id": int(idx),
        "tool": tool,
        "title": (title or STEP_TOOLS[tool])[:60],
        "args": args,
        "depends_on": deps,
        "status": PENDING,
        "ok": None,
        "result": "",
        "error": "",
        "files": [],
        "secs": 0.0,
    }


def _guess_city(goal: str) -> str:
    """从一句话里认出一个地名：先查内置地名表（准），再用「XX两日游」这种句式兜。"""
    t = str(goal or "")
    try:
        from . import builtin_places as _bp
        names = list((getattr(_bp, "BUILTIN_PLACES", {}) or {}).keys())
    except Exception:                                    # noqa: BLE001
        names = []
    # 长名字优先，免得「上海」抢在「上海崇明」前面只剩一半
    for n in sorted([str(x) for x in names if x], key=len, reverse=True):
        if n and n in t:
            return n
    m = re.search(r"([\u4e00-\u9fa5]{2,4})\s*(?:市)?\s*(?:两|二|三|四|五|\d)\s*日", t)
    return m.group(1) if m else ""


def _rule_steps(goal: str) -> list:
    """规则兜底：**模型不可用时也一定给出可跑的步骤**。

    只把用户已经说出来的事排好序，不添加用户没提的东西 —— 唯一例外是
    目的地类需求（「两日游」）默认给一份行程文档，因为「订好整件事」的成品
    本来就该是一份能发出去的东西。
    """
    t = str(goal or "")
    city = _guess_city(t)
    trip = bool(re.search(r"游|行程|旅行|旅游|出差|玩|逛|路线|自驾", t))
    wants_weather = bool(re.search(r"天气|气温|下雨|温度|冷不冷|热不热", t)) or (trip and bool(city))
    # 目的地类需求默认要一份路线/行程点数据 —— 写行程文档时最缺的就是它
    wants_map = bool(re.search(r"路线|怎么走|多远|周边|景点|行程|自驾|高铁|机票", t)) \
        or (trip and bool(city))
    wants_docx = bool(re.search(r"文档|报告|word|docx|方案|攻略|行程单|写一份|写份", t)) \
        or trip or not re.search(r"ppt|幻灯片|演示", t, re.I)
    wants_pptx = bool(re.search(r"ppt|幻灯片|演示|汇报|宣讲", t, re.I))
    steps = []
    if wants_weather and city:
        steps.append({"tool": "get_weather", "title": "查 %s 天气" % city,
                      "args": {"city": city, "days": 3}})
    if wants_map and city:
        steps.append({"tool": "map_plan", "title": "%s 路线与行程点" % city,
                      "args": {"city": city, "days": 2}})
    if wants_docx:
        steps.append({"tool": "make_docx", "title": "写一份成品文档", "args": {}})
    if wants_pptx:
        steps.append({"tool": "make_pptx", "title": "做一份汇报 PPT", "args": {}})
    if not steps:
        # 什么意图都没认出来：至少给一份文档，把这句话本身办成一件有形的东西
        steps.append({"tool": "make_docx", "title": "写一份成品文档", "args": {}})
    out = []
    for item in steps[:5]:
        st = _norm_step(item, len(out) + 1)
        if st:
            out.append(st)
    return out


def plan_steps(goal: str, llm=None, steps=None) -> list:
    """把一句话拆成步骤。返回的每一步都已规范化（tool/args/status 齐备）。

    优先级：调用方直接给的 steps > 模型拆解 > 规则兜底。
    **永不抛异常**：拆解失败一律落到兜底，用户至少能拿到东西。
    """
    g = str(goal or "").strip()
    if steps:
        given = [_norm_step(x, i) for i, x in enumerate(list(steps), 1)]
        given = [x for x in given if x]
        if given:
            return given
    if g:
        text = _ask_text(_PLAN_PROMPT + g, llm=llm)
        parsed = _parse_steps(text)
        if parsed:
            return parsed
    return _rule_steps(g)


# ---------------------------------------------------------------- 内容补齐
def _facts(job) -> str:
    """把已跑完的步骤结果攒成一段「已知事实」，给写文档 / 做 PPT 用。"""
    bits = []
    for st in (job or {}).get("steps") or []:
        if st.get("status") == DONE and st.get("result"):
            bits.append("【%s】\n%s" % (st.get("title") or st.get("tool"),
                                       str(st.get("result"))[:1500]))
    return "\n\n".join(bits)


def _blocks_ok(v) -> bool:
    return isinstance(v, list) and any(isinstance(b, dict) and
                                      str(b.get("text") or "").strip() for b in v)


def _slides_ok(v) -> bool:
    return isinstance(v, list) and any(isinstance(s, dict) and
                                      (str(s.get("title") or "").strip()
                                       or s.get("bullets")) for s in v)


def _notice(job) -> str:
    """兜底正文用的抬头：写清「这是编排器自己凑的」，别让用户以为是模型写的。"""
    return ("以下内容由任务编排器按目标与已查到的数据自动汇总"
            "（本次没有可用的写作模型，格式朴素但内容真实）。")


def _blocks_auto(job, title: str) -> list:
    """没有模型时也要产出**结构正确**的 blocks（docx_maker 只认这个结构）。"""
    blocks = [{"type": "heading", "level": 1, "text": title},
              {"type": "para", "text": "目标：%s" % (job.get("goal") or "")}]
    facts = _facts(job)
    if facts:
        blocks.append({"type": "heading", "level": 2, "text": "一、已查到的数据"})
        for st in job.get("steps") or []:
            if st.get("status") != DONE or not st.get("result"):
                continue
            blocks.append({"type": "heading", "level": 3,
                           "text": str(st.get("title") or st.get("tool"))})
            for line in str(st["result"]).splitlines():
                line = line.strip()
                if line:
                    blocks.append({"type": "para", "text": line[:400]})
    blocks.append({"type": "heading", "level": 2, "text": "二、接下来怎么做"})
    for line in ("把上面查到的数据逐条落到日程上（每天几点去哪、住哪、怎么走）；",
                 "行前确认交通与住宿的预订状态，留一份紧急联系人和订单号；",
                 "把这份文档直接发给同行的人，需要汇报时再用同名 PPT。"):
        blocks.append({"type": "para", "text": line})
    if len(blocks) <= 3:
        blocks.append({"type": "para", "text": _notice(job)})
    return blocks


def _slides_auto(job, title: str) -> list:
    """没有模型时的 PPT 结构：封面 + 数据页 + 结论页，**每页都有真内容**。"""
    slides = [{"title": title, "bullets": [str(job.get("goal") or "")[:60],
                                           "自动汇总 · %s" % time.strftime("%Y-%m-%d")]}]
    for st in job.get("steps") or []:
        if st.get("status") != DONE:
            continue
        lines = [x.strip() for x in str(st.get("result") or "").splitlines() if x.strip()]
        if not lines:
            continue
        slides.append({"title": str(st.get("title") or st.get("tool")),
                       "bullets": [x[:90] for x in lines[:6]]})
    slides.append({"title": "下一步",
                   "bullets": ["按上面的数据把日程定下来",
                               "确认交通住宿预订",
                               "需要汇报时用同一份内容改排版"]})
    return slides


_WRITE_PROMPT = """你在把「已经查到的数据」写成一份可以直接发出去的成品。

用户目标：%s

已知数据：
%s

要求：只用上面给出的数据，不要编造没有的数字和地名；中文表达，分点、具体。
只输出 JSON，不要解释、不要 ``` 围栏：
%s
"""

_DOCX_SHAPE = ('{"blocks": [{"type": "heading", "level": 1, "text": "标题"},'
               ' {"type": "para", "text": "正文段落"}, ...]}')
_PPTX_SHAPE = '{"slides": [{"title": "页标题", "bullets": ["要点1", "要点2"]}, ...]}'


def _fill_content(job, step, llm=None) -> None:
    """给「写文档 / 做 PPT」这类步骤补上正文（模型优先，凑不出来就用真实数据拼）。

    ⚠️ 这里**只补内容，不生成文件** —— 生成文件一律交给 tools.job_step →
    make_docx / make_pptx（模板、封面、图片基底都在那边，重写必然分叉）。
    """
    tool = step.get("tool")
    if tool not in ("make_docx", "make_pptx"):
        return
    args = step.setdefault("args", {})
    if not isinstance(args, dict):
        args = step["args"] = {}
    title = str(args.get("title") or "").strip() or str(job.get("goal") or "")[:40] \
        or "成品"
    if tool == "make_docx":
        if _blocks_ok(args.get("blocks")):
            args.setdefault("title", title)
            return
        got = {}
        text = _ask_text(_WRITE_PROMPT % (job.get("goal") or "", _facts(job) or "（无）",
                                          _DOCX_SHAPE), llm=llm, timeout=90.0)
        if text:
            try:
                got = json.loads(_ARR.search(text).group(0) if _ARR.search(text)
                                 else text)
            except Exception:                            # noqa: BLE001
                got = {}
        blocks = got.get("blocks") if isinstance(got, dict) else None
        args["blocks"] = blocks if _blocks_ok(blocks) else _blocks_auto(job, title)
        args.setdefault("title", title)
        args.setdefault("filename", title + ".docx")
    else:
        if _slides_ok(args.get("slides")):
            args.setdefault("title", title)
            return
        got = {}
        text = _ask_text(_WRITE_PROMPT % (job.get("goal") or "", _facts(job) or "（无）",
                                          _PPTX_SHAPE), llm=llm, timeout=90.0)
        if text:
            try:
                got = json.loads(_ARR.search(text).group(0) if _ARR.search(text)
                                 else text)
            except Exception:                            # noqa: BLE001
                got = {}
        slides = got.get("slides") if isinstance(got, dict) else None
        args["slides"] = slides if _slides_ok(slides) else _slides_auto(job, title)
        args.setdefault("title", title)
        args.setdefault("filename", title + ".pptx")


# ---------------------------------------------------------------- 执行
def _stage(job, stage: str, detail: str = "") -> None:
    """把进度同时写进 actlog 的阶段流水（时间线卡片能直接读，不必回查 job 文件）。"""
    try:
        from . import actlog
        actlog.record_stage(str((job or {}).get("id") or ""), stage, detail)
    except Exception:                                    # noqa: BLE001
        logger.debug("写任务阶段日志失败", exc_info=True)


def create_job(goal: str, steps=None, llm=None) -> dict:
    """建一个任务并落盘（**不执行**）。返回的 job 已含 steps，可直接跑。"""
    g = str(goal or "").strip()
    if not g:
        raise ValueError("目标不能为空")
    job = {
        "id": new_job_id(),
        "goal": g,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": PENDING,
        "ok": None,
        "steps": plan_steps(g, llm=llm, steps=steps),
        "zip_path": "",
        "zip_files": [],
        "warnings": [],
    }
    return save_job(job)


def run_job(job_or_id, llm=None, ui_events=None, pack: bool = True) -> dict:
    """按顺序把任务跑完，然后打成一个 zip。返回跑完的 job（已落盘）。

    顺序执行本身在 `tools.run_plan()` 里（那边负责依赖检查 + 每步写 actlog 进度 +
    逐步落盘），这里只做三件事：起手记一条阶段、把「补内容」当回调递进去、
    收尾算状态与打包。**别再在本地另写一份步骤循环** —— 两份循环必然走岔。
    """
    job = load_job(job_or_id) if isinstance(job_or_id, str) else dict(job_or_id or {})
    if not job.get("id"):
        raise ValueError("没有这个任务")
    from . import tools as _tools
    job["steps"] = list(job.get("steps") or [])
    job["status"] = RUNNING
    job["ok"] = None
    save_job(job)
    _stage(job, "executing", "开始执行：%d 步" % len(job["steps"]))

    def _prepare(st):
        # 只补内容（blocks / slides），生成文件一律留给 tools.job_step → dispatch
        _fill_content(job, st, llm=llm)

    ran = _tools.run_plan(job["steps"], job=job, ui_events=ui_events,
                          prepare=_prepare)
    job["steps"] = ran["steps"]
    job["status"] = "partial" if ran["failed"] else DONE
    job["ok"] = bool(ran["ok"])
    job["warnings"] = list(ran["warnings"])
    if pack:
        try:
            pack_zip(job)
        except Exception as exc:                          # noqa: BLE001
            job["warnings"].append("打包失败：%s: %s" % (type(exc).__name__, exc))
            logger.warning("任务 %s 打包失败", job.get("id"), exc_info=True)
    save_job(job)
    _stage(job, "done" if job.get("ok") else "failed",
           "任务收尾：%s" % ("成功" if job.get("ok") else "部分完成"))
    return job


# ---------------------------------------------------------------- 打包
def _job_markdown(job) -> str:
    lines = ["# %s" % (job.get("goal") or "任务"), "",
             "- 任务号：%s" % job.get("id"),
             "- 创建：%s" % job.get("created"),
             "- 状态：%s" % job.get("status"), "", "## 步骤", ""]
    for st in job.get("steps") or []:
        lines.append("### %s. %s（%s）" % (st.get("id"), st.get("title"),
                                          st.get("tool")))
        lines.append("- 状态：%s%s" % (st.get("status"),
                                      "" if st.get("ok") is not False else "（工具有报错）"))
        if st.get("files"):
            lines.append("- 产出：%s" % "、".join(st["files"]))
        if st.get("error"):
            lines.append("- 错误：%s" % st["error"])
        body = str(st.get("result") or "").strip()
        if body:
            lines += ["", body[:1500], ""]
    if job.get("warnings"):
        lines += ["## 注意", ""] + ["- %s" % w for w in job["warnings"]]
    return "\n".join(lines) + "\n"


def pack_zip(job) -> str:
    """把各步产出的文件 + 一份任务说明打成一个 zip，路径写回 job['zip_path']。

    ⚠️ 用 doclib.file_path(rel) 解析绝对路径，**不要自己拼 library 目录**：
    文件可能在回收站 / 备份里，解析规则只有 doclib 知道。
    zip 里保留**原来的中文文件名**（用户下载下来就能直接发给人）。
    """
    from . import doclib
    d = _dir()
    jid = _safe_id(job.get("id"))
    if not jid:
        raise ValueError("job 缺少 id")
    zpath = os.path.join(d, jid + ".zip")
    names = []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for st in job.get("steps") or []:
            for rel in st.get("files") or []:
                try:
                    p = doclib.file_path(rel)
                except Exception:                        # noqa: BLE001
                    continue
                if os.path.isfile(p) and rel not in names:
                    z.write(p, arcname=str(rel))
                    names.append(str(rel))
        z.writestr("任务说明.md", _job_markdown(job))
    job["zip_path"] = zpath
    job["zip_files"] = names
    return zpath


