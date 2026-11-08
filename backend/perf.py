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

from . import config

PROFILES = {
    "fast":   {"name": "快速", "max_tokens": 8192,  "num_ctx": 16384,  "effort": "low"},
    "medium": {"name": "中等", "max_tokens": 16384, "num_ctx": 40960,  "effort": "medium"},
    "high":   {"name": "高",   "max_tokens": 32768, "num_ctx": 65536,  "effort": "high"},
    "ultra":  {"name": "极高", "max_tokens": 65536, "num_ctx": 131072, "effort": "high"},
}

MIN_CTX, MAX_CTX = 4096, 131072       # 无极调节范围
MIN_OUT = 8192                        # 输出下限（不允许低于这个，用户明确要求）


def detect_model() -> dict:
    """**自动读取**当前模型真实支持的上下文上限（用户要求：先探测再给选项）。

    Ollama 的 /api/show 里 model_info 带 `<arch>.context_length`，
    qwen3-vl 实测是 262144 —— 比我们写死的 131072 大一倍。
    之前把上限写死，等于白白浪费一半能力。探测失败就退回保守值。
    """
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
                        out["max_ctx"] = min(n, 262144)
                        out["ok"] = True
                except Exception:
                    pass
                break
    except Exception:
        pass
    return out


def get() -> dict:
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
    det = detect_model()
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
