# -*- coding: utf-8 -*-
"""本机保管云端 API Key —— 只为让**无人值守的定时任务**也能用云端模型。

为什么需要它（2026-11-09 用户报：「定时任务显示无法连接 ollama，我需要无论
用哪个模型都能完成定时任务」）
------------------------------------------------------------------
原来 Key 只存在浏览器 localStorage 里，服务端从不落盘（安全考虑）。
但定时任务是**半夜自动跑**的：那时候没有浏览器，没人把 Key 送过来，
于是 scheduler 只能固定用本机 Ollama —— 用户没装 / 没启动 Ollama，
任务就必然失败（就是那句「无法连接 ollama」）。

现在的取 Key 顺序
----------------
1. 环境变量 `DEEPSEEK_API_KEY`（服务器 / systemd EnvironmentFile，最优先）；
2. 本机文件 `data/secrets.json`（用户在前端点「保存并校验」时写入）；
3. 都没有 → 返回空字符串（调用方据此回退本地模型或给出清晰提示）。

安全边界（改之前先读）
--------------------
· 只保存用户**主动点保存**的那个 Key，不自动收集任何东西；
· 文件权限尽量收到 600（POSIX 有效；Windows 上 chmod 是空操作，属已知限制）；
· 对外接口**只报 has_key 布尔**，绝不回显 Key 本身；
· 任何日志都走 scrub()，不打印明文。
"""
from __future__ import annotations

import json
import os
import stat

from . import config

_FILE = "secrets.json"


def _path() -> str:
    return config.data("data", _FILE)


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    p = _path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:                                  # POSIX 上收紧权限；Windows 忽略
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def get_key() -> str:
    """环境变量优先，其次本机保管的文件。"""
    k = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if k:
        return k
    return str(_read().get("deepseek_api_key") or "").strip()


def set_key(key: str) -> dict:
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "error": "Key 不能为空"}
    d = _read()
    d["deepseek_api_key"] = key
    d["updated"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    _write(d)
    return {"ok": True, "has_key": True, "path": _path()}


# ---------------- 图像通道（2026-11-15 用户要求「所有 key 统一保管」）----------------
# 背景：图像 Key 原来只存在浏览器 localStorage，后端从不落盘。
# 后果是**每个功能都要单独配一次**：文生图要带 header、图像微改要带 header、
# 数字人后台闭嘴要带 header、定时任务（半夜无浏览器）根本拿不到 → 全部失败。
# 现在统一：用户在 🎨 面板存一次，前端同步落盘到这里；任何通道取不到 Key 时自动兜底。
# 安全边界不变：只存用户主动点保存的那把、不回显明文、日志走 scrub()。
def get_image_key() -> str:
    """图像通道的 Key：环境变量优先，其次本机保管。"""
    k = str(os.environ.get("IMAGE_API_KEY") or "").strip()
    if k:
        return k
    return str(_read().get("image_api_key") or "").strip()


def set_image_key(key: str) -> dict:
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "error": "Key 不能为空"}
    d = _read()
    d["image_api_key"] = key
    d["image_updated"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    _write(d)
    return {"ok": True, "has_key": True, "path": _path()}


def remember_key(key: str) -> bool:
    """前端带来的 DeepSeek Key：后端还没存过就顺手记下来（只写一次）。

    2026-11-15 用户要求「Key 只需配一次，以后新功能直接用这个就行」。
    这样不管用户是在聊天框、🎨 面板还是哪个新功能里填的，
    第一次用过后后端就有了 —— 定时任务、后台线程、以后新加的功能都能直接取，
    不需要每个功能各配一次。
    """
    key = str(key or "").strip()
    if not key or get_key():          # 已有（含环境变量）就不覆盖
        return False
    try:
        set_key(key)
        return True
    except Exception:
        return False


def remember_image_key(key: str) -> bool:
    """前端带来的图像 Key：后端还没存过就顺手记下来（只写一次）。"""
    key = str(key or "").strip()
    if not key or get_image_key():
        return False
    try:
        set_image_key(key)
        return True
    except Exception:
        return False


def clear_image_key() -> dict:
    d = _read()
    d.pop("image_api_key", None)
    d.pop("image_updated", None)
    _write(d)
    return {"ok": True, "has_key": False}


def clear() -> dict:
    d = _read()
    d.pop("deepseek_api_key", None)
    _write(d)
    return {"ok": True, "has_key": False}


def status() -> dict:
    """只报布尔与来源，不回显 Key。"""
    env = bool(str(os.environ.get("DEEPSEEK_API_KEY") or "").strip())
    file_key = str(_read().get("deepseek_api_key") or "").strip()
    return {"ok": True,
            "has_key": bool(env or file_key),
            "source": "env" if env else ("file" if file_key else ""),
            "updated": str(_read().get("updated") or "")}
