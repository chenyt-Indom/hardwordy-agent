# -*- coding: utf-8 -*-
"""启动自检：把「所有能力是否正常」一次性查清楚，并给出修复提示。

为什么需要它
------------
用户的原话：「每次启动都得全部验证所有东西包括文件操作图片等内容都正常并报告状态，
确保无论隔多长时间都没问题」。

长期不用的软件最容易在这几处悄悄坏掉：
  · 模型目录被挪走/删掉（路径写死在 config 里，挪了就找不到）
  · pip 升级把某个依赖搞坏（diffusers / torch 版本不匹配）
  · Ollama 服务没起 / 模型被 `ollama rm` 了
  · 磁盘满导致写文件失败
  · 显卡驱动更新后 CUDA 不可用
这些都不会在启动时立刻报错，而是等用户用到了才炸 —— 体验极差。

设计原则
--------
1. **只做静态检测，不真跑模型**。跑一次 SDXL 要 80 秒，启动时不能这么干。
   查"依赖能不能导入 + 模型文件在不在 + 目录可不可写"就够判断了。
2. **每项都带 hint**：失败时告诉用户"怎么修"，不只是"失败了"。
3. **结果缓存**：`/api/selftest` 直接读缓存，前端不用等。
4. **启动时后台跑**，不阻塞服务起来。
"""
from __future__ import annotations

import importlib
import os
import shutil
import socket
import tempfile
import time
from typing import Any

from . import config


def _ok(detail: str = "", hint: str = "") -> dict:
    return {"ok": True, "detail": detail, "hint": hint}


def _bad(detail: str, hint: str) -> dict:
    return {"ok": False, "detail": detail, "hint": hint}


# ---------------------------------------------------------------- 各项检测

def check_ollama() -> dict:
    """Ollama 服务是否在线。"""
    import urllib.request
    url = (config.load_config() or {}).get("ollama_url") or "http://127.0.0.1:11434"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=5) as r:
            import json as _j
            d = _j.loads(r.read().decode("utf-8", "ignore"))
            return _ok("版本 " + str(d.get("version", "?")))
    except Exception as e:
        return _bad(f"连不上 {url}（{type(e).__name__}）",
                    "在 PowerShell 跑 `ollama serve`，或检查任务栏 Ollama 图标")


def check_models() -> dict:
    """默认模型 / 代码模型是否已下载。"""
    import urllib.request
    import json as _j
    cfg = config.load_config() or {}
    url = (cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=8) as r:
            have = {m.get("name", "") for m in
                    (_j.loads(r.read().decode("utf-8", "ignore")).get("models") or [])}
    except Exception:
        return _bad("查不到已装模型（Ollama 未运行）", "先启动 Ollama")
    need = [cfg.get("default_model"), cfg.get("code_model")]
    miss = [m for m in need if m and m not in have]
    if miss:
        return _bad("未下载：" + ", ".join(miss),
                    "点状态卡片下方的「⬇ 下载模型」，或 PowerShell 里 `ollama pull " + miss[0] + "`")
    return _ok("已装 " + str(len(have)) + " 个模型")


def check_torch() -> dict:
    """torch + CUDA。"""
    try:
        import torch
    except Exception as e:
        return _bad(f"导入失败（{type(e).__name__}）",
                    "pip install torch --index-url https://download.pytorch.org/whl/cu121")
    cuda = bool(getattr(torch, "cuda", None) and torch.cuda.is_available())
    ver = getattr(torch, "__version__", "?")
    if cuda:
        try:
            name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            return _ok(f"{ver} · CUDA · {name} ({mem:.1f} GB)")
        except Exception:
            return _ok(f"{ver} · CUDA 可用")
    return _bad(f"{ver} · CUDA 不可用（会退回 CPU，很慢）",
                "检查显卡驱动；或装 CUDA 版 torch")


def check_t2i() -> dict:
    """文生图：diffusers 能否导入 + SD 模型文件在不在。"""
    try:
        import diffusers  # noqa: F401
        import transformers  # noqa: F401
    except Exception as e:
        return _bad(f"缺依赖（{type(e).__name__}: {e}）",
                    "pip install diffusers transformers accelerate safetensors")
    try:
        from . import t2i
        d = t2i.local_model_dir()
    except Exception as e:
        return _bad(f"t2i 模块异常（{type(e).__name__}）", "检查 backend/t2i.py")
    if not d or not os.path.isdir(d):
        return _bad(f"模型目录不存在：{d}",
                    "config.json 里把 `sd_model_dir` 指到 SDXL base 目录（含 model_index.json）")
    if not os.path.exists(os.path.join(d, "model_index.json")):
        return _bad(f"目录里没有 model_index.json：{d}",
                    "这个目录不是完整的 diffusers 模型，重新下载或换目录")
    try:
        gb = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _, fs in os.walk(d) for f in fs) / 1024 ** 3
    except Exception:
        gb = 0
    return _ok(f"SDXL · {gb:.1f} GB · {d}")


def check_inpaint() -> dict:
    """图片微改：inpaint 模型在不在。"""
    cfg = config.load_config() or {}
    d = str(cfg.get("inpaint_model_dir") or "").strip()
    if not d:
        return _bad("没配 inpaint_model_dir",
                    "config.json 里把 `inpaint_model_dir` 指到 SDXL inpaint 目录")
    if not os.path.isdir(d):
        return _bad(f"目录不存在：{d}", "把 `inpaint_model_dir` 改成正确路径")
    if not os.path.exists(os.path.join(d, "model_index.json")):
        return _bad(f"目录里没有 model_index.json：{d}", "不是完整的 diffusers 模型")
    try:
        gb = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _, fs in os.walk(d) for f in fs) / 1024 ** 3
    except Exception:
        gb = 0
    return _ok(f"SDXL inpaint · {gb:.1f} GB")


def check_office() -> dict:
    """Excel / Word / PPT 三个库。"""
    miss = []
    for mod, pkg in (("openpyxl", "openpyxl"), ("docx", "python-docx"), ("pptx", "python-pptx")):
        try:
            importlib.import_module(mod)
        except Exception:
            miss.append(pkg)
    if miss:
        return _bad("缺：" + ", ".join(miss), "pip install " + " ".join(miss))
    return _ok("openpyxl / python-docx / python-pptx 均可导入")


def check_files() -> dict:
    """文件读写：在工作区里真写一个临时文件再删。"""
    try:
        from . import workspace
        root = workspace.root("") if hasattr(workspace, "root") else ""
        d = root or config.data_root()
        os.makedirs(d, exist_ok=True)
        fd, p = tempfile.mkstemp(prefix=".selftest_", suffix=".tmp", dir=d)
        try:
            os.write(fd, b"ok")
            os.close(fd)
            with open(p, "rb") as f:
                assert f.read() == b"ok"
        finally:
            try:
                os.unlink(p)
            except Exception:
                pass
        # 磁盘余量
        try:
            free = shutil.disk_usage(d).free / 1024 ** 3
            if free < 2:
                return _bad(f"磁盘只剩 {free:.1f} GB", "清理磁盘，模型和生成文件很占空间")
            return _ok(f"可读写 · 剩余 {free:.0f} GB · {d}")
        except Exception:
            return _ok(f"可读写 · {d}")
    except Exception as e:
        return _bad(f"写文件失败（{type(e).__name__}: {e}）", "检查目录权限 / 磁盘是否满")


def check_media_tools() -> dict:
    """图像处理附加能力：超分 / 抠图 / 合成（缺了不影响主流程，标 warning）。"""
    parts = []
    # Real-ESRGAN 权重
    esrgan = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "models", "esrgan")
    alt = r"E:\本地多模态助手-Docker\models\esrgan"
    if os.path.isdir(esrgan) or os.path.isdir(alt):
        parts.append("超分✅")
    else:
        parts.append("超分❌")
    try:
        importlib.import_module("rembg")
        parts.append("抠图✅")
    except Exception:
        parts.append("抠图❌")
    try:
        importlib.import_module("PIL")
        parts.append("Pillow✅")
    except Exception:
        parts.append("Pillow❌")
    ok = all("✅" in p for p in parts)
    s = " · ".join(parts)
    if ok:
        return _ok(s)
    return _bad(s, "超分需要 Real-ESRGAN 权重；抠图需要 `pip install rembg`")


def check_web() -> dict:
    """联网：能否访问一个稳定站点。"""
    import urllib.request
    for url in ("https://www.baidu.com", "https://www.qq.com"):
        try:
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": "selftest"})
            with urllib.request.urlopen(req, timeout=6):
                return _ok("可访问外网")
        except Exception:
            continue
    return _bad("外网不通", "检查网络；如果开了代理，可能拦住了请求")


def check_deepseek() -> dict:
    """DeepSeek API 可达性（不带 Key，只看 401 能否返回）。"""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/models",
            headers={"Authorization": "Bearer sk-selftest", "User-Agent": "selftest"})
        try:
            urllib.request.urlopen(req, timeout=8)
            return _ok("API 可达")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return _ok(f"API 可达（{e.code} = 未带 Key，正常）")
            return _bad(f"HTTP {e.code}", "稍后重试；可能是 CDN 节点问题")
    except Exception as e:
        return _bad(f"连不上（{type(e).__name__}）", "检查网络 / 代理")


def check_voice() -> dict:
    """语音：sherpa-onnx + 麦克风设备。"""
    try:
        importlib.import_module("sherpa_onnx")
    except Exception:
        return _bad("sherpa-onnx 未安装", "pip install sherpa-onnx")
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        ins = [d for d in devs if d.get("max_input_channels", 0) > 0]
        if not ins:
            return _bad("没有输入设备", "插麦克风 / 检查系统声音设置")
        return _ok(f"sherpa-onnx · {len(ins)} 个输入设备")
    except Exception as e:
        return _bad(f"音频设备查询失败（{type(e).__name__}）", "检查 sounddevice / 声卡驱动")


def check_gpu_mem() -> dict:
    """显存余量（SDXL 要 7GB，30B 要 18GB）。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return _bad("无 CUDA", "只能 CPU 跑，会很慢")
        free, total = torch.cuda.mem_get_info(0)
        free_gb, total_gb = free / 1024 ** 3, total / 1024 ** 3
        if free_gb < 6:
            return _bad(f"只剩 {free_gb:.1f} / {total_gb:.1f} GB",
                        "关掉占显存的程序（游戏 / 浏览器硬件加速）")
        return _ok(f"{free_gb:.1f} / {total_gb:.1f} GB 可用")
    except Exception as e:
        return _bad(f"查不到（{type(e).__name__}）", "")


# ---------------------------------------------------------------- 汇总

_CHECKS = [
    ("ollama", "Ollama 服务", check_ollama),
    ("models", "本地模型", check_models),
    ("gpu", "GPU 显存", check_gpu_mem),
    ("t2i", "文生图", check_t2i),
    ("inpaint", "图片微改", check_inpaint),
    ("office", "Office 文档", check_office),
    ("files", "文件读写", check_files),
    ("media", "图像处理", check_media_tools),
    ("web", "联网", check_web),
    ("deepseek", "DeepSeek API", check_deepseek),
    ("voice", "语音输入", check_voice),
]

_CACHE: dict[str, Any] = {"ts": 0, "items": [], "ok_count": 0, "total": 0}


def run_all() -> dict:
    """跑全部检测，结果写进缓存并返回。"""
    items = []
    ok_n = 0
    for key, label, fn in _CHECKS:
        t0 = time.time()
        try:
            r = fn()
        except Exception as e:                       # noqa: BLE001
            r = {"ok": False, "detail": f"{type(e).__name__}: {e}", "hint": ""}
        r["key"] = key
        r["label"] = label
        r["ms"] = int((time.time() - t0) * 1000)
        items.append(r)
        if r.get("ok"):
            ok_n += 1

    _CACHE.update({"ts": time.time(), "items": items,
                   "ok_count": ok_n, "total": len(items)})
    return get()


def get() -> dict:
    return {
        "ts": _CACHE["ts"],
        "ok_count": _CACHE["ok_count"],
        "total": _CACHE["total"],
        "items": _CACHE["items"],
        "ready": _CACHE["total"] > 0 and _CACHE["ok_count"] == _CACHE["total"],
    }
