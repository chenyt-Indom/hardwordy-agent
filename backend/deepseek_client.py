# -*- coding: utf-8 -*-
"""DeepSeek API 客户端（纯对话 + 流式）

安全约定（重要，改这个文件前先读）
--------------------------------
1. API Key 只在**请求作用域内**使用：不写 config.json、不写日志文件、
   不放进任何全局变量。函数返回后即释放。
2. 所有对外抛出的错误信息都经 scrub() 脱敏：`sk-xxxx` 一律打码成 `sk-***`。
   实测踩过坑——requests 的异常消息里会带完整 URL/header 片段，直接抛给前端
   等于把 Key 打在了界面上。
3. 不使用 requests 的自动重试（Retry），避免 Key 被重复发送到非预期地址。

为什么单独开一个文件而不是塞进 ollama_client.py
--------------------------------------------
Ollama 走的是本地 HTTP + 自定义 NDJSON，DeepSeek 走的是 OpenAI 兼容协议 +
SSE（data: {...}\\n\\n），两者的错误码、限流、思维链字段完全不同。混在一起
会让 ollama_client 里那套"num_ctx 必须全局一致否则重载模型"的约束失效。
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Iterator

import requests

BASE = "https://api.deepseek.com"
# (连接超时, 读取超时)：推理模型出首字可能要十几秒，读取给足 5 分钟
TIMEOUT = (10, 300)

# ⚠️⚠️ 必须彻底绕开系统代理（2026-11-08 实测踩坑，两轮才定位）
#
# 现象：同一个 Key 直连返回正常的 401 JSON，但经服务调用返回 "550 空响应"。
# 原因有两层：
#   ① requests 默认 trust_env=True，会读 HTTP_PROXY / HTTPS_PROXY 环境变量；
#   ② **Windows 的 IE/系统代理设置（注册表）也会被 requests 读取** ——
#      光 unset 环境变量没用，服务进程仍然走代理。
# 只传 proxies={"http": None} 在部分版本不足以覆盖系统级代理。
# 结论：Session + trust_env=False 才是彻底隔离（连 .netrc 也不读）。
_sessions = threading.local()


def _session() -> "requests.Session":
    """每个线程复用一个「忽略一切环境代理」的 Session。"""
    s = getattr(_sessions, "s", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False          # ← 关键：不读 env / 注册表 / .netrc
        s.proxies = {"http": None, "https": None}
        _sessions.s = s
    return s


NO_PROXY_MAP = {"http": None, "https": None}

# ⚠️⚠️ DNS 过滤：跳过会返回 550 的 CDN 节点（2026-11-08 实测）
#
# 本机把 api.deepseek.com 解析到 8 个 IP，逐个用 65KB 请求体测：
#   113.240.66.218  → 401 ✅     124.225.27.128 → 550 ❌
#   121.204.231.156 → 401 ✅     124.72.129.71  → 401 ✅
#   171.105.220.186 → 401 ✅     171.108.215.12 → 401 ✅
#   171.108.216.18  → 401 ✅     175.12.122.167 → 401 ✅
# 也就是说：**只有个别节点是腾讯 EdgeOne 的坏节点**，它会对大 POST 返回 550。
# 在 socket 层把坏 IP 从解析结果里剔掉，其余 7 个节点对 65KB 都正常。
# 实测 39 个全量工具（65KB）连发 10 次：9/10 成功；配合 3 次重试 → 99.9%。
#
# ⚠️ 只对 api.deepseek.com 生效，不影响 Ollama（127.0.0.1）和其他域名。
_DS_BAD_IPS = {"124.225.27.128"}

_ORIG_GETADDRINFO = socket.getaddrinfo


def _ds_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    infos = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    if host == "api.deepseek.com":
        good = [i for i in infos if i[4][0] not in _DS_BAD_IPS]
        if good:
            return good
    return infos


if not getattr(socket, "_ds_patched", False):
    socket.getaddrinfo = _ds_getaddrinfo
    socket._ds_patched = True

# 前端下拉用的模型清单
# ⚠️ 2026-11-08 修正：DeepSeek API 的模型名**早就换了**。
# 旧文档里的 deepseek-chat / deepseek-reasoner 现在已不在文档中，
# 现行的是（见 https://api-docs.deepseek.com/quick_start/pricing ）：
#   deepseek-flash    → 由 DeepSeek-V4.1-Flash-1017 服务（默认不思考）
#   deepseek-v4-pro   → V4 Pro，能力更强
# 推理能力不再靠"换个模型"，而是请求体参数：
#   "thinking": {"type": "enabled"}   开关思考
#   "reasoning_effort": "low|medium|high"
# 旧名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 仍被接受，
# 但对应模型已退役，请求会被路由到 V4.1-Flash-1017 并按 Flash 计费。
MODELS = [
    {
        "id": "deepseek-flash",
        "name": "DeepSeek V4.1 Flash",
        # ⚠️ 2026-11-08 改：原来默认不思考 → 前端**看不到任何思考过程**，
        #   用户以为"DeepSeek 不支持流式思考"。其实 V4.1 支持，只是没打开。
        #   现在默认开启（effort=medium），思考会以 reasoning_content 流式推给前端。
        #   想省钱/要更快，可以把这里改回 False（或选别的模型）。
        "desc": "V4.1 Flash，默认开启思考（可见思考过程），响应快、便宜",
        "thinking": True,
        "effort": "medium",
    },
    {
        "id": "deepseek-flash-think",
        "name": "DeepSeek V4.1 Flash（思考）",
        "desc": "同一模型开 thinking + reasoning_effort=medium，复杂问题更稳",
        "thinking": True,
        "effort": "medium",
        "wire_model": "deepseek-flash",      # 实际发给 API 的名字
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "desc": "V4 Pro，最强，适合复杂推理与长文",
        "thinking": True,
        "effort": "high",
    },
]

# Key 打码：sk- 后面跟 8 位以上字母数字
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def scrub(text: object) -> str:
    """把文本里可能出现的 API Key 打码。所有对外输出前必须过一遍。"""
    return _SK_RE.sub("sk-***", str(text))


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败。message 已脱敏。"""


def vision_chat(prompt: str, image_b64: str, model: str, api_key: str,
                max_tokens: int = 900, timeout: tuple = (15, 180)) -> str:
    """**看图**：把图片按 OpenAI 多模态格式发给模型，返回文字回答。

    2026-11-08 实测：DeepSeek（deepseek-chat / deepseek-flash）**接受 image_url**
    并能正确描述截图内容（问"顶栏有没有控件重叠"，回答与截图一致）。
    所以不需要绕道本地视觉模型 —— 直接把图给它看，反馈质量更好。
    """
    if not api_key:
        raise DeepSeekError("缺少 API Key")
    # ⚠️ 前端传的是**内部 id**（deepseek-flash），官方 API 只认 wire_model（deepseek-chat）。
    #   不映射的话这里会 400 model_not_found，然后静默回退本地视觉模型 —— 踩过。
    _conf = next((m for m in MODELS if m["id"] == model), None)
    wire = (_conf or {}).get("wire_model") or model or "deepseek-chat"
    body = {
        "model": wire,
        "max_tokens": max_tokens,
        # ⚠️ 看图**必须关思考**：实测开着 thinking 时，模型把内容全写进
        #   reasoning_content，content 为空 → 我们这边判定"模型没有返回内容"
        #   然后退回本地小模型（白瞎了 DS 的视力）。
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt or "描述这张图片"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + image_b64}},
        ]}],
    }
    try:
        r = _session().post(f"{BASE}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {api_key}",
                                     "Content-Type": "application/json"},
                            timeout=timeout, proxies=NO_PROXY_MAP)
    except requests.RequestException as e:
        raise DeepSeekError("看图请求失败：%s" % scrub(e)) from None
    if r.status_code != 200:
        raise DeepSeekError("看图返回 %s：%s" % (r.status_code, scrub((r.text or "")[:200])))
    try:
        d = r.json()
    except Exception:
        raise DeepSeekError("看图返回的不是 JSON") from None
    msg = ((d.get("choices") or [{}])[0]).get("message") or {}
    txt = str(msg.get("content") or "").strip()
    if not txt:                                   # 有的实现只给 reasoning_content
        txt = str(msg.get("reasoning_content") or "").strip()
    if not txt:
        raise DeepSeekError("模型没有返回内容")
    return txt


def chat_stream(messages: list[dict], model: str, api_key: str,
                temperature: float = 0.7, max_tokens: int = 8192,
                thinking: bool | None = None,
                reasoning_effort: str | None = None) -> Iterator[tuple[str, str]]:
    """流式对话。

    参数
    ----
    messages : OpenAI 格式的消息列表 [{"role": "user", "content": "..."}]
    model    : MODELS 里的 id（deepseek-flash / deepseek-flash-think / deepseek-v4-pro）
               —— 前端下拉传的就是这个；实际发给 API 的名字由 wire_model 决定
    api_key  : 用户在前端输入的 Key（仅本次请求使用）
    thinking : 是否开思考。None = 按 MODELS 里该模型的默认值
    reasoning_effort : low / medium / high，None = 按模型默认值

    产出
    ----
    (kind, text) 元组：
      kind = "content"  → 正文增量
      kind = "thinking" → 思维链增量（开了 thinking 才有）
    """
    if not api_key:
        raise DeepSeekError("缺少 API Key")
    if not model:
        model = "deepseek-flash"

    # 查这个 id 的配置：wire_model（实际发给 API 的名字）、thinking、effort
    conf = next((m for m in MODELS if m["id"] == model), None)
    wire_model = (conf or {}).get("wire_model") or model
    if thinking is None:
        thinking = bool((conf or {}).get("thinking", False))
    if reasoning_effort is None:
        reasoning_effort = (conf or {}).get("effort")

    body: dict = {
        "model": wire_model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # ⚠️ 2026-11 的新协议：思考是请求参数，不是模型名
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking and reasoning_effort:
        body["reasoning_effort"] = reasoning_effort

    try:
        resp = _session().post(
            f"{BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=body,
            stream=True,
            timeout=TIMEOUT,
            proxies=NO_PROXY_MAP,
        )
    except requests.RequestException as e:
        # 注意：异常消息里可能带 URL（不含 Key，但万一有 header 回显也要打码）
        raise DeepSeekError(f"连接 DeepSeek 失败：{scrub(e)}") from None

    # 非 200：把响应体前 300 字符带出去（已脱敏），前端能看懂是余额不足还是 Key 错
    if resp.status_code != 200:
        try:
            detail = resp.text[:300]
        except Exception:
            detail = ""
        hint = ""
        if resp.status_code == 401:
            hint = "（Key 无效或已撤销，请到 platform.deepseek.com 重新创建）"
        elif resp.status_code == 402:
            hint = "（余额不足，请到 platform.deepseek.com/top_up 充值）"
        elif resp.status_code == 429:
            hint = "（请求太频繁或并发超限，稍后再试）"
        elif resp.status_code >= 500:
            hint = "（DeepSeek 服务端故障，稍后重试）"
        raise DeepSeekError(
            f"DeepSeek 返回 {resp.status_code}{hint}：{scrub(detail)}")

    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                # SSE 偶发半截包，跳过而不是整轮失败
                continue

            choices = obj.get("choices") or []
            if not choices:
                # 可能是最后一条 usage 统计，忽略
                continue
            delta = choices[0].get("delta") or {}

            # 思维链（reasoner 专有字段）
            think = delta.get("reasoning_content")
            if think:
                yield ("thinking", think)

            content = delta.get("content")
            if content:
                yield ("content", content)

            finish = choices[0].get("finish_reason")
            if finish:
                # content_filter / length 这类要告诉用户，别静默截断
                if finish == "length":
                    yield ("content", "\n\n（输出达到 max_tokens 上限被截断）")
                break
    finally:
        try:
            resp.close()
        except Exception:
            pass


def chat_stream_with_tools(messages: list[dict], model: str, api_key: str,
                           tools: list[dict], tool_executor,
                           max_rounds: int = 120,
                           temperature: float = 0.7, max_tokens: int = 8192,
                           top_p: float | None = None,
                           thinking: bool | None = None,
                           reasoning_effort: str | None = None,
                           system_prompt: str | None = None,
                           guard=None):
    """**带工具调用**的流式对话（2026-11-08 新增）。

    为什么需要它
    ------------
    DeepSeek 这条通道原来是纯对话，用户反馈「接入 API 的模型无法调度其他工具」。
    官方 API 本身**支持 function calling**（OpenAI 兼容协议），只是我们没接。
    这里补上完整的工具循环：

        发请求(带 tools) → 模型返回 tool_calls → 本地执行 → 结果回填 → 再发请求
        直到模型不再调工具（或达到 max_rounds）。

    参数
    ----
    tools : OpenAI 格式的工具 schema 列表（tools.make_schemas() 的产物）
    tool_executor : callable(name: str, args: dict) -> str
                    由调用方注入（一般是 tools.dispatch 的包装），
                    这样本模块不依赖 tools.py，方便单测。
    max_rounds : 最多几轮工具调用（防死循环）。默认 120（服务器版改成"不轻易中断"）。
    guard : callable(used: set[str], final_text: str) -> str | None，可选。
            **「光嘴上说说」的兜底**：模型准备收官时调一次，返回非空字符串
            就把这段话当成用户消息再推给它，要求真正动手（只兜一次）。
            主通道（/api/chat）早就有这套兜底，DeepSeek 通道原来没有 ——
            用户实测「让它做 Excel，它把表格写在正文里就算完事」。

    产出（元组，和 chat_stream 相比多了几种 kind）
    ----
    ("content",    text)           正文增量
    ("thinking",   text)           思维链增量
    ("tool_start", name)           开始执行某个工具（前端据此弹工具卡片）
    ("tool_result", (name, ok, text))  工具执行完毕（ok=False 表示抛异常）
    ("note",       text)           过程提示（如「还没看到文件产出，我让它真做出来」）
    """
    if not api_key:
        raise DeepSeekError("缺少 API Key")
    if not model:
        model = "deepseek-flash"

    conf = next((m for m in MODELS if m["id"] == model), None)
    wire_model = (conf or {}).get("wire_model") or model
    if thinking is None:
        thinking = bool((conf or {}).get("thinking", False))
    if reasoning_effort is None:
        reasoning_effort = (conf or {}).get("effort")

    # 组装初始消息（系统提示统一放最前）
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend(messages)

    used: set[str] = set()      # 本会话真正调用过的工具（guard 要用）
    guard_used = False          # 兜底只放一次，避免和模型死磕
    cont_used = 0               # 被 max_tokens 截断后自动续写的次数（最多 2 次）

    for rnd in range(max_rounds):
        body: dict = {
            "model": wire_model,
            "messages": msgs,
            "stream": True,
            "temperature": temperature,
            # ★ 2026-11-08：top_p 之前**完全没传** —— 前端那条采样滑杆对云端是死的。
            #   现在显式带上；None 表示"由模型默认"，不塞空值（有的网关会报错）。
            **({"top_p": top_p} if top_p is not None else {}),
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if thinking and reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        # ⚠️ 550 重试（2026-11-08 实测定位）：
        #   本机访问 api.deepseek.com 会随机落到腾讯 EdgeOne CDN，它对**大 POST**
        #   概率性返回 550（Content-Length: 0，Server: TencentEdgeOne）。
        #   换一条连接常常就落到 DeepSeek 真实服务器（openresty）上。
        #   配合 main.py 里把工具集压到 ~18KB，3 次重试实测成功率 99%+。
        resp = None
        last_exc = None
        for _attempt in range(3):
            try:
                resp = _session().post(
                    f"{BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    # 用 data= 显式 UTF-8：json= 默认 ensure_ascii=True，
                    # 中文工具描述会被转义，请求体膨胀 1.6 倍（65KB→105KB）
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    stream=True, timeout=TIMEOUT, proxies=NO_PROXY_MAP,
                )
            except requests.RequestException as e:
                last_exc = e
                if _attempt < 2:
                    time.sleep(0.4)
                    continue
                raise DeepSeekError(f"连接 DeepSeek 失败：{scrub(e)}") from None
            if resp.status_code == 550 and _attempt < 2:
                try:
                    resp.close()
                except Exception:
                    pass
                time.sleep(0.4)
                continue
            break
        if resp is None:
            raise DeepSeekError(f"连接 DeepSeek 失败：{scrub(last_exc)}")

        if resp.status_code != 200:
            try:
                detail = resp.text[:300]
            except Exception:
                detail = ""
            hint = ""
            if resp.status_code == 401:
                hint = "（Key 无效或已撤销）"
            elif resp.status_code == 402:
                hint = "（余额不足）"
            elif resp.status_code == 429:
                hint = "（请求太频繁）"
            raise DeepSeekError(
                f"DeepSeek 返回 {resp.status_code}{hint}：{scrub(detail)}")

        content_buf: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish = None

        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = obj.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}

                if delta.get("reasoning_content"):
                    yield ("thinking", delta["reasoning_content"])

                if delta.get("content"):
                    content_buf.append(delta["content"])
                    yield ("content", delta["content"])

                # 流式下 tool_calls 是**分片**的：name 只在第一片出现，
                # arguments 会分很多片，必须按 index 累加拼接。
                for tc in (delta.get("tool_calls") or []):
                    idx = int(tc.get("index") or 0)
                    slot = tool_calls.setdefault(
                        idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                    break
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # ★ 被 max_tokens 截断（finish_reason=length）→ **自动续写**
        #   2026-11-08 用户报「思考过程有了，但输出被限制了」：
        #   开了 thinking 之后推理 token 也算进 max_tokens，正文很容易被截在半句。
        #   Ollama 那条通道早就有续写逻辑（见 tools._do_write_code），云端这条没有。
        if (not tool_calls and finish == "length" and content_buf
                and cont_used < 6 and rnd + 1 < max_rounds):
            cont_used += 1
            msgs.append({"role": "assistant", "content": "".join(content_buf)})
            msgs.append({"role": "user", "content": (
                "（系统提示）你上一条输出被长度上限截断了。"
                "**接着往下写**，从断开的地方继续，不要重复已经写过的内容，"
                "也不要重新开头。")})
            yield ("note", "输出被长度上限截断，正在自动续写…")
            continue

        # 这一轮没有工具调用 → 本该对话结束
        if not tool_calls:
            # ★ 「光嘴上说说」兜底（2026-11-08）：
            #   用户要文件/图片，模型却把内容写在正文里就收工 —— 代码层退回一轮。
            #   只在**确实写了正文**（说明它自认为答完了）且没兜过时才触发。
            if guard is not None and not guard_used and content_buf:
                try:
                    tip = guard(used, "".join(content_buf))
                except Exception:                 # noqa: BLE001
                    tip = None
                if tip:
                    guard_used = True
                    msgs.append({"role": "assistant",
                                 "content": "".join(content_buf) or "（无正文）"})
                    msgs.append({"role": "user", "content": tip})
                    yield ("note", "还没看到成品产出，我让它真正做出来。")
                    continue
            return

        # 把 assistant 的 tool_calls 消息补进历史（协议要求）
        assistant_calls = []
        for idx in sorted(tool_calls):
            tc = tool_calls[idx]
            assistant_calls.append({
                "id": tc["id"] or f"call_{rnd}_{idx}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["args"] or "{}",
                },
            })
        msgs.append({
            "role": "assistant",
            "content": "".join(content_buf) if content_buf else None,
            "tool_calls": assistant_calls,
        })

        # 逐个执行，结果以 role=tool 回填
        for tc in assistant_calls:
            name = tc["function"]["name"]
            used.add(name)
            yield ("tool_start", name)
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            try:
                result = tool_executor(name, args)
                ok = True
            except Exception as e:                # noqa: BLE001
                result = f"工具执行失败：{type(e).__name__}: {e}"
                ok = False
            text = str(result)
            yield ("tool_result", (name, ok, text[:400]))
            # ⚠️ 回填给模型的长度要卡住：某些工具（read_file / 网页正文）
            # 一次能吐几万字，直接塞回去会超上下文
            msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": text[:12000],
            })

    # 轮次用尽：**先要一段收尾交代**，别让用户对着半截话。
    # 2026-11-09 用户报「任务跑到一半就停、模型也不知道刚刚改了什么」：
    # 原来这里只吐一句「已达上限」，用户既不知道改到哪一步，也看不到改过哪些文件。
    try:
        msgs.append({"role": "user", "content": (
            "（系统提示）工具轮次已用尽，现在**不能再调用任何工具**。"
            "请只用文字交代：① 已经改了/做了什么（逐个列出文件路径）；"
            "② 还差什么没做完；③ 下一步建议。直接说现状，不要写「我将要…」。")})
        _body = {
            "model": wire_model,
            "messages": msgs,
            "stream": True,
            "temperature": temperature,
            **({"top_p": top_p} if top_p is not None else {}),
            "max_tokens": max(512, min(int(max_tokens or 1024), 2048)),
            "thinking": {"type": "disabled"},
        }
        _r = _session().post(
            f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            data=json.dumps(_body, ensure_ascii=False).encode("utf-8"),
            stream=True, timeout=TIMEOUT, proxies=NO_PROXY_MAP,
        )
        if _r.status_code == 200:
            try:
                for _raw in _r.iter_lines(decode_unicode=True):
                    if not _raw:
                        continue
                    _line = _raw.strip()
                    if not _line.startswith("data:"):
                        continue
                    _pl = _line[5:].strip()
                    if not _pl or _pl == "[DONE]":
                        continue
                    try:
                        _o = json.loads(_pl)
                    except json.JSONDecodeError:
                        continue
                    for _ch in (_o.get("choices") or []):
                        _d = _ch.get("delta") or {}
                        if _d.get("content"):
                            yield ("content", _d["content"])
                        if _ch.get("finish_reason"):
                            break
            finally:
                try:
                    _r.close()
                except Exception:
                    pass
    except Exception:                     # noqa: BLE001
        pass                              # 收尾失败也不能影响已完成的产物

    yield ("content", f"\n\n（已达工具调用轮次上限 {max_rounds}，停止继续调用）")


def list_models() -> list[dict]:
    """给前端下拉用的清单（不联网，静态定义）。"""
    return MODELS


def check_key(api_key: str) -> dict:
    """校验 Key 是否可用 + 查询余额（GET /user/balance）。

    返回 {"ok": bool, "balance": str, "currency": str, "msg": str}
    """
    if not api_key:
        return {"ok": False, "balance": "", "currency": "", "msg": "缺少 API Key"}
    try:
        r = _session().get(
            f"{BASE}/user/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=(8, 20), proxies=NO_PROXY_MAP,
        )
    except requests.RequestException as e:
        return {"ok": False, "balance": "", "currency": "", "msg": f"连接失败：{scrub(e)}"}

    if r.status_code == 401:
        return {"ok": False, "balance": "", "currency": "", "msg": "Key 无效或已撤销"}
    if r.status_code != 200:
        return {"ok": False, "balance": "", "currency": "",
                "msg": f"HTTP {r.status_code}：{scrub(r.text[:200])}"}

    try:
        d = r.json()
    except Exception:
        return {"ok": False, "balance": "", "currency": "", "msg": "响应解析失败"}

    infos = d.get("balance_infos") or []
    if infos:
        b = infos[0]
        return {
            "ok": bool(d.get("is_available")),
            "balance": str(b.get("total_balance", "")),
            "currency": str(b.get("currency", "")),
            "msg": "可用" if d.get("is_available") else "余额不足",
        }
    return {"ok": bool(d.get("is_available")), "balance": "", "currency": "",
            "msg": "可用" if d.get("is_available") else "余额不足"}
