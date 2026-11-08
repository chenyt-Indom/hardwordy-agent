# -*- coding: utf-8 -*-
"""给智能体装一双“眼睛”：截图 + 让视觉模型描述（2026-11-08）。

为什么需要
----------
用户要求：「调前端 / 生成 PPT / 文本排版时，要能**看见成果**，再自己微调」。
但现在的代码模型（DeepSeek V4.1）是**纯文本**的，它看不到页面长什么样 ——
只能凭想象改 CSS，改完也不知道对不对。

方案：**分工**。
  · 会写代码的模型（DS）负责改；
  · 本机的视觉模型（qwen3-vl 8B）负责"看" —— 它对图片有原生支持（Ollama images 字段）；
  · 把"看到了什么"以**文字**回传给代码模型，它就能据此微调。

为什么不直接把图塞给 DS
----------------------
DeepSeek 的 chat API 不接受 image 输入。硬塞会被拒或静默忽略 —— 那比不看更糟
（模型会以为自己看到了）。所以统一走"本地 VL 转述 → 文本反馈"这条路，
任何代码模型都能用。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time

from . import config

# 视觉模型：默认用已装的 qwen3-vl 8B（比 30B 快，够看布局）
_VL_PREFER = ("qwen3-vl:8b", "qwen3-vl:30b", "qwen2.5vl:3b")


def _pick_vl_model() -> str:
    cfg = config.load_config() or {}
    want = str(cfg.get("vision_model") or "").strip()
    if want:
        return want
    from . import main as M
    try:
        have = M._installed_models()
    except Exception:
        have = []
    for n in _VL_PREFER:
        if n in have:
            return n
    for n in have:
        if "vl" in n.lower() or "vision" in n.lower():
            return n
    return ""


def _out_dir() -> str:
    d = config.data("data", "shots")
    os.makedirs(d, exist_ok=True)
    return d


def screenshot(url: str = "http://127.0.0.1:8000", width: int = 1280,
               height: int = 900, wait_ms: int = 7000) -> dict:
    """用无头 Edge 截一张图（本机已装 Edge，不需要额外依赖）。"""
    edge = (shutil.which("msedge")
            or r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not os.path.isfile(edge):
        return {"ok": False, "error": "找不到 msedge（Edge 没装？）"}
    out = os.path.join(_out_dir(), "shot_%s.png" % time.strftime("%H%M%S"))
    prof = os.path.join(_out_dir(), "prof_%s" % time.strftime("%H%M%S"))
    try:
        subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--hide-scrollbars", "--user-data-dir=%s" % prof,
             "--window-size=%d,%d" % (int(width), int(height)),
             "--virtual-time-budget=%d" % int(wait_ms),
             "--screenshot=%s" % out, url],
            capture_output=True, timeout=90)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if not os.path.isfile(out) or os.path.getsize(out) < 2000:
        return {"ok": False, "error": "截图失败（文件没生成或太小）"}
    return {"ok": True, "path": out, "bytes": os.path.getsize(out),
            "size": "%dx%d" % (int(width), int(height))}


def describe(image_path: str, question: str = "", ds_key: str = "",
             model: str = "") -> dict:
    """看图并回答。**优先走 DeepSeek（多模态）**，失败再退本地 VL。"""
    if not image_path or not os.path.isfile(image_path):
        return {"ok": False, "error": "图片不存在：%s" % image_path}

    # ① 首选：DeepSeek 直接看图（2026-11-08 实测它接受 image_url，且看得准）
    if ds_key:
        try:
            from . import deepseek_client as D
            b64 = base64.b64encode(open(image_path, "rb").read()).decode("ascii")
            q = (question or "").strip() or (
                "客观描述这张界面截图的布局：①有没有元素重叠、错位、被裁切？"
                "②有没有横向挤压或大面积空白？③文字有没有看不清或被遮挡？"
                "④用 3~6 条列出你看到的问题（没有就说没问题）。")
            txt = D.vision_chat(q, b64, model or "deepseek-flash", ds_key)
            return {"ok": True, "model": "DeepSeek " + (model or "deepseek-flash"),
                    "text": txt, "via": "deepseek"}
        except Exception as e:                        # noqa: BLE001
            _fallback_reason = "%s: %s" % (type(e).__name__, e)
    else:
        _fallback_reason = "没带 DeepSeek Key"

    model = _pick_vl_model()
    if not model:
        return {"ok": False,
                "error": "DeepSeek 看图失败（%s），本机也没有视觉模型" % _fallback_reason}
    from . import main as M
    try:
        b64 = base64.b64encode(open(image_path, "rb").read()).decode("ascii")
    except OSError as e:
        return {"ok": False, "error": "读图失败：%s" % e}
    q = (question or "").strip() or (
        "客观描述这张界面截图的布局：①有没有元素重叠、错位、被裁切？"
        "②有没有横向挤压或大面积空白？③文字有没有看不清或被遮挡？"
        "④用 3~6 条列出你看到的问题（没有就说没问题）。")
    try:
        resp = M.client.chat(
            [{"role": "user", "content": q}],
            model=model, stream=False, images_base64=[b64],
            params={"temperature": 0.1, "max_tokens": 800, "think": False})
        data = resp.json() if hasattr(resp, "json") else resp
        msg = data.get("message") or {}
        text = str(msg.get("content") or "").strip()
        if not text:
            text = str(msg.get("thinking") or "").strip()
        if not text:
            return {"ok": False, "error": "视觉模型没有输出内容", "model": model}
        return {"ok": True, "model": model, "text": text,
                "via": "local", "note": "DeepSeek 优先但失败：%s" % _fallback_reason}
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
