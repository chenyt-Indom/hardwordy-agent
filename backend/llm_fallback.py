# -*- coding: utf-8 -*-
"""云端 LLM 不可用时，自动降级到本地 Ollama，让对话**不中断**。

为什么需要（2026-11-15 用户实测踩到）：
    用户连续两轮对话被 DeepSeek `402 余额不足` 打断。根因在 main.py 主循环：

        except Exception as e:               # noqa: BLE001
            q.put(("error", f"{type(e).__name__}: {e}"))

    任何异常都直接变成一条 error 推给前端，**没有任何降级**。用户只能手动重发，
    而重发还是同一个 402 —— 表现就是「一直断」。

    而本机 Ollama 一直在线（实测：qwen2.5vl:3b 首字 5.8s、正常作答 24 字；
    装有 qwen3-vl:8b / qwen2.5vl:3b / deepseek-r1:1.5b）。云端挂了本地明明能顶，
    却让用户看着报错，这是纯粹的浪费。

设计约束（都是踩过坑才写下的）：
  1. `plan()` **绝不允许抛异常** —— 它是在 `except` 块里被调用的，再抛一次就
     连"降级"这条路都没了，整轮彻底报废。
  2. 降级是**保命通道**，不追求质量：不带工具循环、不做多轮 agent，
     只把消息喂进去、把文字流出来。能给出回答 > 什么都没有。
  3. 必须处理「只给 thinking 不给 content」的模型（deepseek-r1:1.5b 实测就是这样，
     返回 0 字正文）。这类模型的正文要退回用 reasoning 兜底。
  4. 必须有总超时：降级本身如果卡 10 分钟，比直接报错更糟。
  5. 本地模型不支持工具调用 → 降级时要把 tool 相关消息压平，否则某些模型会报错。
"""

from __future__ import annotations

import json
import logging
import time

from . import ollama_client

logger = logging.getLogger(__name__)

# 这些错误码说明「云端这条路当前走不通」，值得试本地。
# 不含 "empty"（模型没返回内容）—— 那是偶发，重试比降级合适。
FALLBACK_CODES = frozenset({
    "balance",   # 402 余额不足  ← 用户实际踩到的
    "auth",      # 401 Key 无效
    "timeout",   # 连接/读取超时 ← 用户踩到多次
    "network",   # 连不上
    "server",    # 5xx
    "rate",      # 429 限流
})

# 本地模型优先级：先挑「质量够 + 支持视觉」的，再退纯文本快的。
# 实测数据：qwen2.5vl:3b 首字 5.8s、正常作答；deepseek-r1:1.5b 正文为空（推理模型）。
_PREFER = [
    "qwen2.5vl:3b",
    "qwen3-vl:8b",
    "deepseek-r1:1.5b",
    "qwen2.5:3b",
    "qwen3:8b",
]

# 明确不能用来聊天的（embedding 模型）。
_NOT_CHAT = ("embed", "nomic-embed", "bge-", "mxbai-embed")

# 降级单轮总时长上限（秒）。超过就放弃，让原来的错误照旧报出去。
_TOTAL_TIMEOUT = 180.0

_REASON_CN = {
    "balance": "云端余额不足",
    "auth": "云端 API Key 失效",
    "timeout": "云端连接超时",
    "network": "云端网络不通",
    "server": "云端服务端故障",
    "rate": "云端限流",
}


def explain(code: str) -> str:
    """把错误码翻成一句人话（给用户看的）。"""
    return _REASON_CN.get(str(code or ""), "云端不可用")


def _installed() -> list:
    """本地已安装的聊天模型名列表。取不到就返回空（不抛）。"""
    try:
        models = ollama_client.OllamaClient().list_models() or []
    except Exception:
        logger.debug("fallback: list_models 失败", exc_info=True)
        return []
    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "").strip()
        if not name:
            continue
        low = name.lower()
        if any(bad in low for bad in _NOT_CHAT):
            continue
        out.append(name)
    return out


def pick_model(has_images: bool = False) -> str:
    """挑一个本地模型。

    has_images=True 时**必须**选支持视觉的（纯文本模型收到图片会直接报错）。
    返回 "" 表示本地没有可用模型。
    """
    have = _installed()
    if not have:
        return ""
    have_set = set(have)
    # 1) 按优先级找已安装的
    for want in _PREFER:
        if want in have_set:
            if has_images and not _looks_vision(want):
                continue
            return want
    # 2) 优先级列表都不在 → 退而求其次：有图就挑名字像视觉的，没图就挑第一个
    if has_images:
        for n in have:
            if _looks_vision(n):
                return n
        return ""            # 只有纯文本模型 → 带图不能降级
    return have[0]


def _looks_vision(name: str) -> bool:
    """按名字猜这个模型支不支持看图（qwen2.5vl / qwen3-vl / llava / minicpm-v…）。"""
    low = str(name or "").lower()
    keys = ("vl", "vision", "llava", "minicpm-v", "moondream", "pixtral", "gemma3")
    return any(k in low for k in keys)


def plan(exc: BaseException, has_images: bool = False) -> dict:
    """判断这个异常值不值得降级。**保证不抛异常。**

    返回 {"ok": True, "model": ..., "code": ..., "note": ...} 或 {"ok": False, "why": ...}
    """
    try:
        code = str(getattr(exc, "code", "") or "")
        if code not in FALLBACK_CODES:
            return {"ok": False, "why": "错误类型不需要降级（%s）" % (code or "unknown")}
        model = pick_model(has_images=has_images)
        if not model:
            return {"ok": False,
                    "why": ("本地没有支持看图的模型" if has_images
                            else "本地没有可用的聊天模型")}
        note = "☁️→💻 %s，已自动切到本地模型 %s 继续回答（质量可能下降）" % (
            explain(code), model)
        return {"ok": True, "model": model, "code": code, "note": note}
    except Exception as e:                       # noqa: BLE001 —— 这里绝不能抛
        logger.warning("fallback.plan 异常（按不降级处理）：%s", e, exc_info=True)
        return {"ok": False, "why": "降级判断本身出错"}


def _flatten(messages: list) -> list:
    """把消息压成「本地模型能安心吃」的形式。

    · 去掉 tool / tool_calls 字段（本地模型不一定支持，留着容易报 400）
    · content 非字符串（多模态 list）→ 拼成纯文本
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        if role not in ("system", "user", "assistant"):
            # tool / function 这类结果，降级通道里当作用户补充信息
            role = "user"
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for seg in c:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text") or ""))
                else:
                    parts.append(str(seg))
            c = "\n".join(p for p in parts if p)
        out.append({"role": role, "content": str(c or "")})
    return out


def stream(messages: list, model: str, temperature: float = 0.7,
           max_tokens: int = 2048, images_base64: list | None = None,
           timeout: float = _TOTAL_TIMEOUT):
    """走本地模型流式生成。

    产出 (kind, text)：
      kind = "content"  → 正文增量
      kind = "thinking" → 思维链增量

    ⚠️ 会**先缓存 thinking**，等这一轮结束若正文为空，再把 thinking 当正文吐出来。
       原因：deepseek-r1:1.5b 这类推理模型实测 `think:False` 也把内容全写进
       thinking，content 一直是空 —— 不兜底的话用户看到的是「降级了但一个字没有」。
    """
    if not model:
        raise RuntimeError("没有指定本地模型")
    msgs = _flatten(messages)
    client = ollama_client.OllamaClient()
    resp = client.chat(msgs, model=model, stream=True,
                       images_base64=images_base64,
                       params={"temperature": float(temperature),
                               "max_tokens": int(max_tokens)})

    deadline = time.time() + float(timeout)
    got_content = False
    think_buf = []

    for line in resp.iter_lines():
        if time.time() > deadline:
            logger.warning("fallback: 超过 %.0fs 总时长，主动收尾", timeout)
            break
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("error"):
            raise RuntimeError(str(obj["error"])[:200])
        m = obj.get("message") or {}
        if m.get("thinking"):
            think_buf.append(m["thinking"])
            yield ("thinking", m["thinking"])
        if m.get("content"):
            got_content = True
            yield ("content", m["content"])
        if obj.get("done"):
            break

    # 正文一个字都没有，但思维链有内容 → 把它当正文吐出去（r1 那种模型）
    if not got_content:
        txt = "".join(think_buf).strip()
        if txt:
            logger.info("fallback: 模型 %s 只给了 thinking，已当作正文输出", model)
            yield ("content", txt)
        else:
            yield ("content", "（本地模型 %s 没有返回内容）" % model)


def health() -> dict:
    """给前端/诊断用：本地降级通道当前可用吗。"""
    try:
        online = bool((ollama_client.OllamaClient().health() or {}).get("online"))
    except Exception:
        online = False
    models = _installed() if online else []
    return {"online": online, "models": models,
            "preferred": pick_model(), "codes": sorted(FALLBACK_CODES)}
