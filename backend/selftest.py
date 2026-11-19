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
import sys
import tempfile
import time
from typing import Any

from . import config
import logging
logger = logging.getLogger(__name__)


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


def check_models(target: str = "") -> dict:
    """本地模型是否已下载。

    `target` 为界面上**当前选用的那个模型**（用户要求「选用什么模型就显示
    什么模型的状态，检验也是一样」）。为空 / "auto" 时退回 config 的默认模型。
    """
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

    tgt = (target or "").strip()
    if tgt and tgt.lower() != "auto":
        if tgt in have or (tgt + ":latest") in have:
            return _ok("已装 " + tgt)
        return _bad("未下载：" + tgt,
                    "点状态卡片下方的「⬇ 下载模型」；或 PowerShell 里 `ollama pull " + tgt + "`")

    need = [cfg.get("default_model"), cfg.get("code_model")]
    miss = [m for m in need if m and m not in have and (m + ":latest") not in have]
    if miss:
        return _bad("未下载：" + ", ".join(miss),
                    "点状态卡片下方的「⬇ 下载模型」，或 PowerShell 里 `ollama pull " + miss[0] + "`")
    return _ok("已装 " + str(len(have)) + " 个模型")


def check_cloud_model(target: str = "") -> dict:
    """云端模型：本地不用下载，只核对这个 id 认不认识（防止前端传错名字）。"""
    tgt = (target or "").strip()
    if not tgt:
        return _ok("未指定（默认 deepseek-flash）")
    try:
        from . import deepseek_client
        for m in deepseek_client.MODELS:
            if m.get("id") == tgt:
                wire = m.get("wire_model") or tgt
                extra = "" if wire == tgt else "（实际调用 " + wire + "）"
                return _ok("云端 " + str(m.get("name") or tgt) + "，无需下载" + extra)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return _bad("未知的云端模型 id：" + tgt, "刷新页面重新选择模型")


def check_tools() -> dict:
    """工具「绝对调度」对账：模型看到的每个工具，都必须真有实现。

    用户要求：「确保模型对工具的绝对调度以及通顺的保障」。

    为什么只做**静态对账**、不真跑一遍：真跑会写文件、画图、联网、弹问答框，
    启动自检绝不该有副作用。而"调度不通"最常见的根因恰恰是静态的：
      ① 工具 schema 里写了 xxx，`dispatch()` 里**忘了加分支** → 模型一调就
         「未知工具」，用户看到的是"它说要做但什么都没发生"。
      ② schema 本身不合法（required 里的字段 properties 里没有 / 没有
         properties / 重名）→ DeepSeek 直接 400，**整批工具全用不了**，
         而且报错信息完全看不出是哪个工具的问题。
      ③ 工具定义体积（KB）—— 本机访问 api.deepseek.com 会随机落到腾讯
         EdgeOne CDN，它对大 POST 概率性返回 550（见 deepseek_client 的说明）。
         这个数字必须一直看得见，改工具时才知道有没有踩线。
    """
    import inspect
    import json as _j
    import re

    from . import tools as _tools

    cfg = config.load_config() or {}
    try:
        schemas = _tools.make_schemas(
            web_enabled=bool(cfg.get("web_enabled")),
            kb_enabled=bool(cfg.get("rag_enabled")),
            code_exec=bool(cfg.get("code_exec_enabled")),
            office_gen=True,
            ask_mode=str(cfg.get("ask_mode") or "quick"),
        )
    except Exception as e:                           # noqa: BLE001
        return _bad(f"生成工具清单失败（{type(e).__name__}）: {e}",
                    "这是代码问题，先看后端日志")

    names: list[str] = []
    bad_schema: list[str] = []
    for s in schemas or []:
        fn = (s or {}).get("function") or {}
        nm = str(fn.get("name") or "")
        if not nm:
            bad_schema.append("（无名）")
            continue
        names.append(nm)
        params = fn.get("parameters") or {}
        props = params.get("properties")
        # ⚠️ `properties: {}` 对**无参数工具**（get_time / workspace_list…）是合法的，
        #    不能判失败；只有整个 properties 缺失才算不合法。
        if not isinstance(props, dict):
            bad_schema.append(nm + ":缺 properties")
            continue
        req = params.get("required") or []
        miss = [k for k in req if k not in props]
        if miss:
            bad_schema.append(nm + ":required 缺 " + ",".join(miss))

    dup = sorted({n for n in names if names.count(n) > 1})

    # dispatch 里真正实现的分支：源码里全是 `if name == "xxx":`
    try:
        src = inspect.getsource(_tools.dispatch)
        wired = set(re.findall(r'name\s*==\s*"([^"]+)"', src))
        # 有些工具是**按前缀统一分派**的（如 selfdev_* 走 name.startswith），
        # 只认 `name == "x"` 会把它们误判成"没实现"。
        prefixes = set(re.findall(r'name\.startswith\(\s*"([^"]+)"\s*\)', src))
    except Exception:                                # noqa: BLE001
        return _bad("读不到 dispatch() 源码", "这是打包/权限问题，检查安装目录")

    missing = sorted(n for n in set(names)
                     if n not in wired
                     and not any(n.startswith(p) for p in prefixes))
    if missing:
        return _bad("没有实现：" + ", ".join(missing),
                    "这些工具已经交给模型了，但 dispatch() 里没有分支 —— "
                    "模型一调就报未知工具。补分支或从 make_schemas 里去掉")
    if bad_schema:
        return _bad("schema 不合法：" + "; ".join(bad_schema[:4]),
                    "DeepSeek 会因此直接 400、整批工具全用不了；按提示补齐 properties")
    if dup:
        return _bad("工具名重复：" + ", ".join(dup), "去掉重复的那个定义")

    kb = len(_j.dumps(schemas, ensure_ascii=False).encode("utf-8")) / 1024.0
    note = "，定义 %.1f KB" % kb
    if kb > 60:
        note += "（偏大，DeepSeek 通道注意 550 重试）"
    return _ok(f"{len(names)} 个工具全部可达{note}")


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
    """文生图：**云端通道**是否就绪（2026-11-08 起本地 SDXL 已下线）。

    这里不打真实出图请求（会花钱、要几秒），只确认：
      ① 服务地址填了、域名解析得通；② 接口在（401 = 要 Key，说明路是通的）。
    真正能不能画要看用户在界面里填的 Key —— Key 在浏览器里，后端看不到，
    所以这里只说"通道可达"，Key 的状态由前端自己显示。
    """
    cfg = config.load_config() or {}
    base = str(cfg.get("image_api_base") or "https://xingpan.site/v1").rstrip("/")
    model = str(cfg.get("image_api_model") or "gpt-image-2.5-flare")
    if not base.startswith("http"):
        return _bad("image_api_base 不是合法地址：" + base,
                    "config.json 里填成 https://xingpan.site/v1 这样的完整地址")
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(base + "/models",
                                     headers={"Authorization": "Bearer sk-probe"})
        try:
            urllib.request.urlopen(req, timeout=8)
            reach = "可达"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                reach = f"可达（{e.code}=要 Key，正常）"
            elif e.code == 404:
                return _bad(f"{base}/models 返回 404",
                            "检查 image_api_base 是否少了或多了 /v1")
            else:
                reach = f"可达（HTTP {e.code}）"
    except Exception as e:                       # noqa: BLE001
        return _bad(f"连不上 {base}（{type(e).__name__}）",
                    "检查网络；或把 image_api_base 换成可用的兼容服务地址")
    return _ok(f"云端 · {model} · {reach}")


def check_inpaint() -> dict:
    """图片微改：走云端 /images/edits（和文生图同一个服务，共用 Key）。"""
    cfg = config.load_config() or {}
    base = str(cfg.get("image_api_base") or "https://xingpan.site/v1").rstrip("/")
    if not base.startswith("http"):
        return _bad("image_api_base 没配对", "config.json 里填完整地址")
    try:
        import requests  # noqa: F401
    except Exception:
        return _bad("缺 requests", "pip install requests")
    return _ok("云端 edits · " + base + "/images/edits（Key 与文生图共用）")


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
                logger.debug("silent-except", exc_info=True)
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
    # ⚠️ 2026-11-08：超分/抠图**运行时都要 torch**。
    #   原来只查"权重在不在""rembg 能不能 import"，torch 坏了照样报 ✅ —— 假绿灯。
    #   实测这台机器 torch 的 4 个 DLL 被系统拒绝加载，功能其实用不了。
    _torch_ok = True
    try:
        importlib.import_module("torch")
    except Exception:
        _torch_ok = False
        parts = [p.replace("✅", "⚠️") for p in parts if p.startswith("超分")]
        if not parts:
            parts = ["超分⚠️"]
    try:
        importlib.import_module("rembg")
        parts.append("抠图✅" if _torch_ok else "抠图⚠️(torch 不可用)")
    except Exception:
        parts.append("抠图❌")
    try:
        importlib.import_module("PIL")
        parts.append("Pillow✅")
    except Exception:
        parts.append("Pillow❌")
    # ⚠️ 只把 "❌" 当失败；"⚠️" 表示"依赖不全、暂时用不了"，不该染红整项自检
    ok = not any("❌" in p for p in parts)
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
    """显存余量（SDXL 要 7GB，30B 要 18GB）。

    ⚠️ 2026-11-08 改：**不再依赖 torch**。
    实测这台机器上 Python314 那份 torch 有 4 个 DLL（shm/torch/torch_cuda/
    torch_python）被系统拒绝加载（WinError 5，可读但不可执行），
    于是「GPU 显存」永远显示 ❌ —— 但显卡本身完全正常，nvidia-smi 能查。
    诊断显存这种系统信息不该被一个 Python 包的加载问题拖累，
    所以先用 nvidia-smi（系统自带、最可靠），查不到再退回 torch。
    """
    import shutil
    import subprocess
    from .procwin import NOWIN as _NOWIN
    exe = shutil.which("nvidia-smi") or r"C://Windows//System32//nvidia-smi.exe"
    if os.path.isfile(exe):
        try:
            r = subprocess.run(
                [exe, "--query-gpu=memory.free,memory.total",
                 "--format=csv,noheader,nounits"],
                 creationflags=_NOWIN,
                capture_output=True, text=True, timeout=12)
            line = (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else ""
            if line:
                parts = [x.strip() for x in line.split(",")]
                free_gb = float(parts[0]) / 1024
                total_gb = float(parts[1]) / 1024
                # ⚠️ 2026-11-08：**本机模型占着显存是正常的**，不能报红。
                #   实测 8B 加载后就占 11.1GB，只剩 0.3GB —— 旧逻辑直接判 ❌，
                #   用户看到的"GPU 显存 只剩 0.3GB"像是出故障，其实一切正常。
                loaded_gb = 0.0
                try:
                    import urllib.request as _u
                    import json as _j
                    _url = (config.load_config() or {}).get("ollama_url") \
                        or "http://127.0.0.1:11434"
                    with _u.urlopen(_url.rstrip("/") + "/api/ps", timeout=6) as _r:
                        _d = _j.loads(_r.read().decode("utf-8", "ignore"))
                    loaded_gb = sum((m.get("size") or 0) for m in
                                    (_d.get("models") or [])) / 1024 ** 3
                except Exception:
                    loaded_gb = 0.0
                if free_gb < 6:
                    if loaded_gb >= 4:
                        return _ok("%.1f / %.1f GB 可用"
                                   "（其中 %.1f GB 是本机模型占用，正常）"
                                   % (free_gb, total_gb, loaded_gb))
                    return _bad("只剩 %.1f / %.1f GB" % (free_gb, total_gb),
                                "关掉占显存的程序（游戏 / 浏览器硬件加速）")
                return _ok("%.1f / %.1f GB 可用" % (free_gb, total_gb))
        except Exception:
            logger.debug("silent-except", exc_info=True)

    # 退回 torch（能导入就用它，导入失败就如实说清楚，别只丢一个异常名）
    try:
        import torch
    except Exception as e:                            # noqa: BLE001
        return _bad("torch 加载失败（%s）" % type(e).__name__,
                    "本地绘图/超分/抠图会不可用；**云端绘图、对话、文档都不受影响**。"
                    "多数是 torch 的 DLL 被安全策略拦了，重装 torch 可修。")
    try:
        if not torch.cuda.is_available():
            return _bad("无 CUDA", "只能 CPU 跑，会很慢")
        free, total = torch.cuda.mem_get_info(0)
        free_gb, total_gb = free / 1024 ** 3, total / 1024 ** 3
        if free_gb < 6:
            return _bad("只剩 %.1f / %.1f GB" % (free_gb, total_gb),
                        "关掉占显存的程序（游戏 / 浏览器硬件加速）")
        return _ok("%.1f / %.1f GB 可用" % (free_gb, total_gb))
    except Exception as e:                            # noqa: BLE001
        return _bad("查不到（%s）" % type(e).__name__, "")


# ---------------------------------------------------------------- 汇总

# 每项检测都可能"与当前选用的模型无关"：
#   用 DeepSeek 云端模型时，本地 Ollama / 本地模型有没有下载**不影响使用**；
#   用本地模型时，DeepSeek 通不通**也不影响使用**。
# 所以每项带一个 scope，run_all(model) 据此决定是真跑还是标"跳过"。
# 跳过的项仍然显示在面板里（⚪ + 原因），只是不计入 ok/total —— 既不误导，也不隐瞒。
SCOPE_ANY = ""          # 任何模式都必须正常（文件、图片、Office、语音……）
SCOPE_LOCAL = "local"   # 只在本地模型模式才要求
SCOPE_CLOUD = "cloud"   # 只在云端模型模式才要求

_CHECKS = [
    ("ollama", "Ollama 服务", check_ollama, SCOPE_LOCAL),
    ("models", "当前模型", check_models, SCOPE_LOCAL),
    ("tools", "工具调度", check_tools, SCOPE_ANY),
    ("gpu", "GPU 显存", check_gpu_mem, SCOPE_ANY),
    ("t2i", "文生图", check_t2i, SCOPE_ANY),
    ("inpaint", "图片微改", check_inpaint, SCOPE_ANY),
    ("office", "Office 文档", check_office, SCOPE_ANY),
    ("files", "文件读写", check_files, SCOPE_ANY),
    ("media", "图像处理", check_media_tools, SCOPE_ANY),
    ("web", "联网", check_web, SCOPE_ANY),
    ("deepseek", "DeepSeek API", check_deepseek, SCOPE_CLOUD),
    ("voice", "语音输入", check_voice, SCOPE_ANY),
]

# 缓存按模型分开存：切模型后各看各的状态，互不覆盖。
# 键是归一化后的模型名（"" 表示"跟随配置 / auto"）。
_CACHE: dict[str, Any] = {}
_CACHE_MAX = 8


def _norm(model: str | None) -> str:
    m = (model or "").strip()
    return "" if m.lower() in ("", "auto") else m


def _is_cloud(model: str) -> bool:
    return _norm(model).lower().startswith("deepseek")


def _empty(model: str = "") -> dict:
    return {"ts": 0, "model": _norm(model), "items": [], "ok_count": 0,
            "total": 0, "skipped": 0, "ready": False}


def run_all(model: str = "") -> dict:
    """跑全部检测，结果写进缓存并返回。

    `model` = 界面当前选用的模型（"qwen3-vl:30b" / "deepseek-flash" / "" 跟随配置）。
    """
    mdl = _norm(model)
    cloud = _is_cloud(mdl)
    cur = SCOPE_CLOUD if cloud else SCOPE_LOCAL
    items = []
    ok_n = 0
    skip_n = 0

    for entry in _CHECKS:
        key, label, fn = entry[0], entry[1], entry[2]
        scope = entry[3] if len(entry) > 3 else SCOPE_ANY
        t0 = time.time()

        # 与当前模型无关 → 不跑，标"跳过"（仍然显示，只是不计分）
        if scope and scope != cur:
            items.append({
                "key": key, "label": label, "ok": True, "skip": True,
                "detail": ("本轮用云端模型，本地通道不参与" if scope == SCOPE_LOCAL
                           else "本轮用本地模型，云端通道不参与"),
                "hint": "", "ms": 0,
            })
            skip_n += 1
            continue

        try:
            if key == "models":
                r = check_cloud_model(mdl) if cloud else fn(mdl)
            else:
                r = fn()
        except Exception as e:                       # noqa: BLE001
            r = {"ok": False, "detail": f"{type(e).__name__}: {e}", "hint": ""}
        r["key"] = key
        r["label"] = label
        r["ms"] = int((time.time() - t0) * 1000)
        items.append(r)
        if r.get("ok"):
            ok_n += 1

    res = {"ts": time.time(), "model": mdl, "items": items,
           "ok_count": ok_n, "total": len(items) - skip_n,
           "skipped": skip_n, "ready": (len(items) - skip_n) > 0 and ok_n == (len(items) - skip_n)}

    if len(_CACHE) >= _CACHE_MAX and mdl not in _CACHE:
        # 只留最近的几个，避免长期运行无限增长
        for k in sorted(_CACHE, key=lambda k: _CACHE[k].get("ts", 0))[: max(1, _CACHE_MAX // 2)]:
            _CACHE.pop(k, None)
    _CACHE[mdl] = res
    return dict(res)


def get(model: str = "") -> dict:
    """读缓存。没跑过就返回空壳（调用方据此决定要不要现场跑一遍）。"""
    return dict(_CACHE.get(_norm(model)) or _empty(model))


# ---------------------------------------------------------------- 报告卡

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _locate(fn: Any, default: str = "") -> str:
    """把一个函数定位成 `backend/xxx.py:123` 这样的证据行号。

    为什么报告卡一定要带这个：用户问「有没有问题」时，只回一句"失败了"
    等于没说 —— 他没法判断该不该管、也从不知道去哪儿看。带上文件:行号，
    他（或下一个助手）能直接跳到出事的那一行。
    """
    import inspect
    try:
        path = inspect.getsourcefile(fn) or ""
        _, line = inspect.getsourcelines(fn)
        if not path or not line:
            return default
        rel = os.path.relpath(path, _ROOT).replace("\\", "/")
        return "%s:%d" % (rel, line)
    except Exception:                                    # noqa: BLE001
        return default


def _regress_evidence(name: str) -> str:
    """回归用例名 → `backend/regress.py:行号`（找不到就退回文件名）。

    动态用例（`feature.xxx`）本身没有注册在 `_CASES` 里，证据直接指向
    它自己的断言文件 `backend/_feature_tests/xxx.py`。
    """
    if name.startswith("feature."):
        stem = name[len("feature."):]
        return "backend/_feature_tests/%s.py" % stem
    try:
        from . import regress as _regress
        for c in getattr(_regress, "_CASES", None) or []:
            if (c or {}).get("name") == name:
                return _locate(c.get("fn"), "backend/regress.py")
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return "backend/regress.py"


def _regress_severity(why: str) -> str:
    """把「用例本身加载失败」和「断言真的挂了」分开。

    `backend/_feature_tests/` 里有一批老脚本（模块级直接执行、没有 run()），
    regress 一律记成"加载失败" —— 这是**测试写法过时**，不是系统坏了。
    报告卡上必须能分辨：前者给 ⚠️（amber），后者才是 ❌（red），
    否则用户每次点自检都看到一片红，很快就学会无视它了。
    """
    w = why or ""
    if "断言文件加载失败" in w or "缺少 run() 函数" in w:
        return "warn"
    return "error"


_REGRESS_MARK = "@@SELFTEST_REPORT@@"

# 在**子进程**里跑回归。
# 为什么不用现成的 regress.run_disk()：`backend/_feature_tests/` 里有几个历史断言
# 文件是在模块级直接执行脚本的（结尾就是 `sys.exit(...)`），而
# `regress._load_dynamic()` 只捕获 `Exception` —— `SystemExit` 会穿透把整个回归
# 进程带走，run_disk() 于是拿不到结果（"子进程没跑起来"）。报告卡不能因此长期
# 报红，所以在子进程里先把 `sys.exit` 换成普通异常：那几个文件会被 regress 记成
# "加载失败"，其余用例照常跑完，结论才真实可用。
_REGRESS_RUNNER = r'''
import io
import json
import sys

sys.path.insert(0, ".")
# 子进程 stdout 在 Windows 上默认是 GBK，父进程按 utf-8 解码 → 中文全变乱码，
# 于是「加载失败」这类关键字都匹配不上。这里强制 utf-8。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
except Exception:
    pass
_out = sys.stdout


class _FakeExit(Exception):
    pass


def _fake_exit(*a, **k):
    raise _FakeExit("sys.exit")


sys.exit = _fake_exit
try:
    import os as _os
    from backend import regress
    # ⚠️ 用 /dev/null 而不是 StringIO：有几个用例会拿 `sys.stdout.buffer`
    #    再包一层 TextIOWrapper，StringIO 没有 buffer，会把好用例误判成加载失败。
    _buf = open(_os.devnull, "w", encoding="utf-8", errors="replace")
    _old = sys.stdout
    sys.stdout = _buf
    try:
        _r = regress.run(scope=sys.argv[1])
    finally:
        sys.stdout = _old
except BaseException as _e:                       # noqa: BLE001
    _r = {"ok": False, "items": [], "passed": 0, "failed": 1, "skipped": 0,
          "total": 0, "seconds": 0.0,
          "failed_items": [{"name": "regress",
                            "why": "%s: %s" % (type(_e).__name__, _e)}],
          "summary": "回归子进程异常：%s" % type(_e).__name__}
_out.write("@@SELFTEST_REPORT@@" + json.dumps(_r, ensure_ascii=False) + "\n")
_out.flush()
'''


def _run_regress(scope: str = "quick", timeout: int = 240) -> dict:
    """跑一遍行为回归，返回和 `regress.run()` 同构的结果（永不抛异常）。

    优先子进程（读的是磁盘上最新的代码，也不会污染服务进程）；子进程起不来时
    退回进程内跑 —— 两条路径都把 `sys.exit` 换成普通异常，绝不让报告把整个
    服务带走。
    """
    import json as _j
    import subprocess
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, "-c", _REGRESS_RUNNER, scope],
                           cwd=_ROOT, capture_output=True, timeout=timeout)
        txt = (p.stdout or b"").decode("utf-8", "replace")
        line = ""
        for ln in txt.splitlines():
            if ln.startswith(_REGRESS_MARK):
                line = ln[len(_REGRESS_MARK):]
        if line:
            r = _j.loads(line)
            r["via"] = "subprocess"
            return r
        logger.warning("回归子进程没输出（rc=%s），退回进程内", p.returncode)
    except Exception:                                # noqa: BLE001
        logger.warning("回归子进程失败，退回进程内", exc_info=True)

    # 退化路径：进程内跑
    try:
        import contextlib

        from . import regress as _regress
        _orig_exit = sys.exit
        sys.exit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sys.exit"))
        try:
            # 同上：用 /dev/null（有 .buffer），不用 StringIO
            with open(os.devnull, "w", encoding="utf-8", errors="replace") as _dn, \
                    contextlib.redirect_stdout(_dn):
                r = _regress.run(scope=scope)
        finally:
            sys.exit = _orig_exit
        r["via"] = "inproc"
        return r
    except BaseException as e:                       # noqa: BLE001
        return {"ok": False, "items": [], "passed": 0, "failed": 1, "skipped": 0,
                "total": 0, "seconds": round(time.time() - t0, 2),
                "failed_items": [{"name": "regress",
                                  "why": "%s: %s" % (type(e).__name__, e)}],
                "summary": "回归跑不起来：%s" % type(e).__name__}


def build_report(model: str = "", scope: str = "quick", fresh: bool = True,
                 include_regress: bool = True) -> dict:
    """自检报告卡：把「环境自检 + 行为回归」聚合成一张通过/失败清单。

    为什么需要它（用户痛点）
    ------------------------
    「有没有问题」这句话用户要反复问，每问一次就得把验证重跑一遍、再靠聊天
    里的一堆文字去解释 —— 白白浪费轮次。这里一次性把结论固化成结构化的卡片：
        passed = [{"name": ..., "evidence": "backend/xxx.py:123", "detail": ...}]
        failed = [{"name": ..., "evidence": "backend/xxx.py:123",
                   "detail": 真实报错, "hint": 怎么修}]
        ts     = 生成时间（秒级时间戳，另给 time 便于直接显示）
    前端拿到它就能画「通过绿勾 / 失败红叉+证据」，不用自己拼字符串。

    参数
    ----
    model           界面当前选用的模型（""/"auto" = 跟随 config）
    scope           "quick"（秒级）/ "full"（追加全量自检 + 全模块导入）
    fresh           True = 现场重跑环境自检；False = 先用缓存（毫秒级）
    include_regress False = 只出环境自检（回归较慢时用）

    口径
    ----
    · 环境自检里 `skip=True` 的项（与当前模型无关）**不进任何一个清单** ——
      既不冒充"通过"，也不算"失败"，避免误导。
    · 任何一侧抛异常都不会让整个报告消失：异常本身作为一条 failed 写进卡片。
    """
    t0 = time.time()
    mdl = _norm(model)
    sc = scope if scope in ("quick", "full") else "quick"
    passed: list[dict] = []
    failed: list[dict] = []

    # ---- 1) 环境自检 ----
    checks = {e[0]: e[2] for e in _CHECKS if len(e) >= 3}
    try:
        st = get(mdl) if not fresh else {}
        if not (st.get("items") or []):
            st = run_all(mdl)
    except Exception as e:                               # noqa: BLE001
        st = {"items": []}
        failed.append({
            "name": "自检·整体执行",
            "evidence": _locate(run_all, "backend/selftest.py"),
            "detail": "%s: %s" % (type(e).__name__, e),
            "hint": "看后端日志里的完整堆栈",
        })
    for it in st.get("items") or []:
        it = it or {}
        if it.get("skip"):
            continue
        key = str(it.get("key") or "")
        entry = {
            "name": "自检·" + str(it.get("label") or key or "未知项"),
            "evidence": _locate(checks.get(key), "backend/selftest.py"),
            "detail": str(it.get("detail") or ""),
            "source": "selftest",
        }
        if it.get("ok"):
            passed.append(entry)
        else:
            entry["severity"] = "error"
            entry["hint"] = str(it.get("hint") or "")
            failed.append(entry)

    # ---- 2) 行为回归 ----
    reg: dict = {}
    if include_regress:
        reg = _run_regress(sc)
        for it in reg.get("items") or []:
            it = it or {}
            if it.get("skipped"):
                continue
            nm = str(it.get("name") or "")
            why = str(it.get("why") or "")
            entry = {
                "name": "回归·" + nm,
                "evidence": _regress_evidence(nm),
                "detail": why,
                "source": "regress",
            }
            if it.get("ok"):
                passed.append(entry)
            else:
                entry["severity"] = _regress_severity(why)
                entry["hint"] = ("单独跑 `python -m backend.regress %s` 可复现；"
                                 "再看 %s" % (sc, entry["evidence"]))
                failed.append(entry)

    ts = time.time()
    errs = [x for x in failed if (x.get("severity") or "error") == "error"]
    warns = [x for x in failed if (x.get("severity") or "error") != "error"]
    ok = not errs
    head = ("全部通过（%d 项）" % len(passed)) if ok else \
           ("%d 项通过 / %d 项失败：%s" % (
               len(passed), len(errs),
               "；".join(str(x.get("name")) for x in errs[:3])))
    if warns:
        head += "（另有 %d 条用例文件无法加载，不影响系统本身）" % len(warns)
    return {
        "ts": ts,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "ok": ok,
        "model": mdl,
        "scope": sc,
        "passed": passed,
        "failed": failed,
        "passed_count": len(passed),
        "failed_count": len(failed),
        "error_count": len(errs),
        "warn_count": len(warns),
        "total": len(passed) + len(failed),
        "regress": {
            "ran": bool(include_regress),
            "via": reg.get("via") or "",
            "ok": bool(reg.get("ok")),
            "passed": int(reg.get("passed") or 0),
            "failed": int(reg.get("failed") or 0),
            "skipped": int(reg.get("skipped") or 0),
            "seconds": reg.get("seconds") or 0,
        },
        "seconds": round(ts - t0, 2),
        "summary": head,
    }
