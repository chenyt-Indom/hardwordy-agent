# -*- coding: utf-8 -*-
"""性能档位 / 上下文长度（2026-11-08 用户要求）。

用户原话：「模型被调用的参数量不能低于 workbuddy 的，只能多不能少，
性能可以自选是快速、中等、高还是极高，以及上下文使用也可以自己无极调节大小」。

所以这里的**下限**是有意抬高的：即使选"快速"，输出额度与上下文也不低于
WorkBuddy 的默认档（它的对话默认上限在 8k 输出 / 16k 上下文量级）。
- 输出（max_tokens）：fast 8192 / medium 16384 / high 32768 / ultra 65536
- 上下文（num_ctx）： fast 16384 / medium 40960 / high 65536 / ultra 131072
- DS 推理档（reasoning_effort）：low / medium / high / high

`num_ctx_override` 一旦 >0 就**覆盖**档位给的上下文（用户手动无极调节）。
本机显存有限时，上下文开太大会拖慢甚至失败 —— 界面上要提示。
"""
from __future__ import annotations

import time

from . import config

PROFILES = {
    "fast":   {"name": "快速", "max_tokens": 8192,  "num_ctx": 16384,  "effort": "low"},
    "medium": {"name": "中等", "max_tokens": 16384, "num_ctx": 40960,  "effort": "medium"},
    "high":   {"name": "高",   "max_tokens": 32768, "num_ctx": 65536,  "effort": "high"},
    "ultra":  {"name": "极高", "max_tokens": 65536, "num_ctx": 131072, "effort": "high"},
}

MIN_CTX, MAX_CTX = 4096, 1048576      # 无极调节范围（云模型 1M 也能开到）
MIN_OUT = 8192                        # 输出下限（不允许低于这个，用户明确要求）

# ---------------- 模型能力探测的缓存（2026-11-11 加）----------------
# ⚠️ 为什么必须缓存：本机连 127.0.0.1:11434 时，**即使 Ollama 没开、被拒绝，
#    Windows 也要等满 2 秒**才返回 ConnectionRefused（实测 2004 ms，原始 socket 也是）。
#    而探测一次要连两个接口（/api/tags + /api/show）= 4 秒。
#    这段代码在 /api/chat 里是**同步阻塞**调用，Ollama 一关，每条消息白等 6 秒
#    （perf.get() 4s + _installed_models() 2s），整个事件循环一起冻住。
#
# 策略：成功缓存 60 秒；失败缓存 120 秒（失败时更要省，因为大概率还会失败）。
# 缓存按**模型名**分桶 —— 换模型立刻失效，不会把 A 模型的能力安到 B 头上。
_DET_CACHE = {"key": "", "t": 0.0, "fail_t": 0.0, "val": None}
_OK_TTL = 60.0
_FAIL_TTL = 120.0


def _detect_model_raw() -> dict:
    """真正去问 Ollama 的那一段（可能有 4 秒网络开销，只在缓存失效时走）。"""
    out = {"model": "", "max_ctx": MAX_CTX, "ok": False}
    try:
        cfg = config.load_config() or {}
        model = str(cfg.get("default_model") or "")
        base = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
        # 配置里的 default 可能没下载（这台机器就是 30b 未装、实际跑 8b）。
        # 探测要以**真正在用的**那个为准，否则读到的是"上限 131072"而不是模型的 262144。
        try:
            from . import main as M
            have = M._installed_models()
            if have and model not in have:
                model = next((n for n in have if "qwen3-vl" in n), "") or have[0]
        except Exception:
            pass
        if not model:
            return out
        out["model"] = model
        import json as _j

        import urllib.request as _u
        req = _u.Request(base + "/api/show",
                         data=_j.dumps({"model": model}).encode(),
                         headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=8) as r:
            d = _j.loads(r.read().decode("utf-8", "ignore"))
        mi = d.get("model_info") or {}
        for k, v in mi.items():
            if k.lower().endswith("context_length"):
                try:
                    n = int(v)
                    if n > 4096:
                        out["max_ctx"] = min(n, MAX_CTX)
                        out["ok"] = True
                except Exception:
                    pass
                break
    except Exception:
        pass
    return out


def detect_model(force: bool = False) -> dict:
    """自动读取当前模型真实支持的上下文上限（**带缓存**，见文件顶部的说明）。

    Ollama 的 /api/show 里 model_info 带 `<arch>.context_length`，
    qwen3-vl 实测是 262144 —— 比写死的 131072 大一倍，所以不能写死。

    Args:
        force: 跳过缓存强制重探（设置面板点「刷新」时用）。
               注意：Ollama 没开时这一下要等 4 秒，别放进对话路径。
    """
    cfg = config.load_config() or {}
    model = str(cfg.get("default_model") or "")
    now = time.time()
    c = _DET_CACHE
    if not force and c["val"] is not None and c["key"] == model:
        if c["t"] and (now - c["t"]) < _OK_TTL:
            return c["val"]                      # 成功缓存
        if c["fail_t"] and (now - c["fail_t"]) < _FAIL_TTL:
            return c["val"]                      # 失败缓存（沿用上次结果，不再等超时）
    out = _detect_model_raw()
    c["key"] = model
    c["val"] = out
    if out.get("ok"):
        c["t"] = now
        c["fail_t"] = 0.0
    else:
        c["fail_t"] = now
        c["t"] = 0.0
    return out


def detect_cloud(key: str) -> dict:
    """**读取云模型的真实参数**（用户要求：自动读型号与详细信息）。

    DeepSeek 的 /models 会直接返回能力清单，实测：
      deepseek-flash → context_window 1048576（1M）、max_output_tokens 393216、
                       input_modalities ["text","image"]、effort low/high/max
    之前只探测本地 Ollama，云端一律按 128k 处理 —— 等于把 1M 的能力锁死了。
    """
    out = {"ok": False, "models": []}
    if not key:
        return out
    try:
        import json as _j
        import urllib.request as _u
        req = _u.Request("https://api.deepseek.com/models",
                         headers={"Authorization": "Bearer " + key})
        with _u.urlopen(req, timeout=10) as r:
            d = _j.loads(r.read().decode("utf-8", "ignore"))
        for m in (d.get("data") or []):
            out["models"].append({
                "id": m.get("id"), "name": m.get("name") or m.get("id"),
                "context_window": m.get("context_window"),
                "max_output_tokens": m.get("max_output_tokens"),
                "modalities": m.get("input_modalities") or [],
                "effort": (m.get("effort") or {}).get("supported_levels") or [],
            })
        out["ok"] = bool(out["models"])
    except Exception as e:                            # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def get(with_detect: bool = True) -> dict:
    """读当前生效的性能档位 / 上下文 / 输出额度。

    Args:
        with_detect: 是否顺便探测模型真实上下文上限。
            · 设置面板（`/api/perf`）要 True —— 它要显示"你这台机器能开多大"。
            · **对话路径一律传 False**：`num_ctx` 和 `max_tokens` 完全由配置算出来，
              跟探测结果无关（`hi` 恒等于 MAX_CTX，探测只影响展示字段）。
              传 False 就不会为了一个用不到的展示字段，每条消息白等 4 秒。
              2026-11-11 实测：Ollama 关闭时 True 要 4,128 ms，False 是 0 ms。
    """
    cfg = config.load_config() or {}
    prof = str(cfg.get("perf_profile") or "medium")
    if prof not in PROFILES:
        prof = "medium"
    p = PROFILES[prof]
    try:
        ov = int(cfg.get("num_ctx_override") or 0)
    except Exception:
        ov = 0
    ctx = ov if ov > 0 else p["num_ctx"]
    ctx = max(MIN_CTX, min(MAX_CTX, ctx))
    try:
        mt = int(cfg.get("max_tokens") or 0)
    except Exception:
        mt = 0
    # 输出额度取"配置值"和"档位值"里更大的那个 —— 只能多不能少
    out = max(MIN_OUT, p["max_tokens"], mt)
    det = detect_model() if with_detect else {
        "model": str(cfg.get("default_model") or ""),
        "max_ctx": MAX_CTX, "ok": False, "skipped": True}
    hi = max(MAX_CTX, det["max_ctx"])      # 选项上限跟着模型真实能力走
    return {"profile": prof, "name": p["name"], "max_tokens": out,
            "num_ctx": ctx, "effort": p["effort"],
            "override": ov, "profiles": [{"id": k, "name": v["name"],
                                          "max_tokens": v["max_tokens"],
                                          "num_ctx": v["num_ctx"]}
                                         for k, v in PROFILES.items()],
            "min_ctx": MIN_CTX, "max_ctx": hi, "min_out": MIN_OUT,
            # 自动探测结果：前端据此显示"你这台机器实际能开多大"
            "detected": det, "model_ctx_max": det["max_ctx"]}


def set(profile: str = "", num_ctx: int = -1, max_tokens: int = 0) -> dict:
    """改档位 / 上下文 / 输出上限。返回改完的完整设置。"""
    cfg = config.load_config() or {}
    if profile:
        if profile not in PROFILES:
            return {"ok": False, "error": "未知档位：%s" % profile}
        cfg["perf_profile"] = profile
    if num_ctx is not None and int(num_ctx) >= 0:
        n = int(num_ctx)
        if n == 0:
            cfg["num_ctx_override"] = 0          # 0 = 跟随档位
        else:
            hi = max(MAX_CTX, detect_model()["max_ctx"])
            if not (MIN_CTX <= n <= hi):
                return {"ok": False,
                        "error": "上下文要在 %d~%d 之间（上限由当前模型能力决定）"
                                 % (MIN_CTX, hi)}
            cfg["num_ctx_override"] = n
    if max_tokens:
        m = int(max_tokens)
        if m < MIN_OUT:
            return {"ok": False, "error": "输出上限不能低于 %d（这是你定的下限）" % MIN_OUT}
        cfg["max_tokens"] = m
    config.save_config(cfg)
    r = get()
    r["ok"] = True
    return r
