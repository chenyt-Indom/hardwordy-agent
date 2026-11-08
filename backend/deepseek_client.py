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
from typing import Iterator

import requests

BASE = "https://api.deepseek.com"
# (连接超时, 读取超时)：推理模型出首字可能要十几秒，读取给足 5 分钟
TIMEOUT = (10, 300)

# 前端下拉用的模型清单
MODELS = [
    {
        "id": "deepseek-chat",
        "name": "DeepSeek Chat",
        "desc": "通用对话，响应快，便宜",
        "support_image": False,
    },
    {
        "id": "deepseek-reasoner",
        "name": "DeepSeek Reasoner",
        "desc": "带思维链推理，复杂问题更强",
        "support_image": False,
    },
]

# Key 打码：sk- 后面跟 8 位以上字母数字
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def scrub(text: object) -> str:
    """把文本里可能出现的 API Key 打码。所有对外输出前必须过一遍。"""
    return _SK_RE.sub("sk-***", str(text))


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败。message 已脱敏。"""


def chat_stream(messages: list[dict], model: str, api_key: str,
                temperature: float = 0.7, max_tokens: int = 4096,
                reasoning_effort: str | None = None) -> Iterator[tuple[str, str]]:
    """流式对话。

    参数
    ----
    messages : OpenAI 格式的消息列表 [{"role": "user", "content": "..."}]
    model    : deepseek-chat | deepseek-reasoner
    api_key  : 用户在前端输入的 Key（仅本次请求使用）
    reasoning_effort : reasoner 专用，low/medium/high，None 用服务端默认

    产出
    ----
    (kind, text) 元组：
      kind = "content"  → 正文增量
      kind = "thinking" → 思维链增量（仅 reasoner）
    """
    if not api_key:
        raise DeepSeekError("缺少 API Key")
    if not model:
        model = "deepseek-chat"

    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if model == "deepseek-reasoner" and reasoning_effort:
        body["reasoning_effort"] = reasoning_effort

    try:
        resp = requests.post(
            f"{BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=body,
            stream=True,
            timeout=TIMEOUT,
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
        r = requests.get(
            f"{BASE}/user/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=(8, 20),
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
