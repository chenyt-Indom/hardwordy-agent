# -*- coding: utf-8 -*-
"""数字人：形象文件 + 口型参数 + 看屏幕（2026-11-12）。

为什么走"参数化口型"而不是 Wav2Lip
----------------------------------
用户要"数字人 + 音频驱动口型"。但本机 Python 3.14 下 torch 导入就报
`shm.dll` 错误，Wav2Lip / SadTalker 这类**生成式口型**根本跑不起来
（要另开 3.11 环境 + 下几百 MB 权重）。

所以这里走**参数化口型**：
  · 一张立绘（用户的照片二次元化，或 AI 生成）；
  · 一个"嘴部矩形"（归一化坐标，可校准）；
  · 前端用 Web Audio 的 AnalyserNode 取音频包络，实时改嘴部矩形的
    高度/宽度 → 看起来就在说话。

代价（必须说清，不能吹）：这是**形变**不是**重绘**，嘴的形状不会跟着
音素变（不会做出"b/p/m"的闭唇）。二次元立绘上观感可以接受，写实人脸会偏假。
要真口型得装 torch + Wav2Lip。

存的东西都在 data/avatar/：
  · avatar.png     当前形象
  · mouth.json     嘴部矩形 {x,y,w,h}（0~1 归一化）+ 眨眼参数
"""
from __future__ import annotations

import json
import os
import time

from . import config

# 嘴部默认位置：脸的中下部。用户可以用前端"校准"拖动，别指望默认值就准。
DEFAULT_MOUTH = {"x": 0.50, "y": 0.72, "w": 0.16, "h": 0.065}
_MOUTH_FILE = "mouth.json"
_IMG_FILE = "avatar.png"
_META_FILE = "meta.json"


def _dir() -> str:
    d = config.data("avatar")
    os.makedirs(d, exist_ok=True)
    return d


def _img_path() -> str:
    return os.path.join(_dir(), _IMG_FILE)


def _read_json(name: str, default: dict) -> dict:
    p = os.path.join(_dir(), name)
    if not os.path.isfile(p):
        return dict(default)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else dict(default)
    except Exception:                                  # noqa: BLE001
        return dict(default)


def _write_json(name: str, d: dict) -> None:
    p = os.path.join(_dir(), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def mouth() -> dict:
    m = dict(DEFAULT_MOUTH)
    m.update({k: float(v) for k, v in _read_json(_MOUTH_FILE, {}).items()
              if k in ("x", "y", "w", "h")})
    # 夹到合法区间，别让脏数据把嘴画到画面外
    m["x"] = min(max(m["x"], 0.0), 1.0)
    m["y"] = min(max(m["y"], 0.0), 1.0)
    m["w"] = min(max(m["w"], 0.02), 0.9)
    m["h"] = min(max(m["h"], 0.01), 0.5)
    return m


def set_mouth(d: dict) -> dict:
    cur = mouth()
    for k in ("x", "y", "w", "h"):
        if k in (d or {}):
            try:
                cur[k] = float(d[k])
            except (TypeError, ValueError):
                pass
    _write_json(_MOUTH_FILE, cur)
    return mouth()


def info() -> dict:
    p = _img_path()
    has = os.path.isfile(p)
    meta = _read_json(_META_FILE, {})
    return {
        "ok": True,
        "has_image": has,
        # 加个时间戳做缓存击穿：换了形象前端要立刻看到新的
        "url": ("/api/avatar/raw?v=%d" % int(os.path.getmtime(p))) if has else "",
        "mouth": mouth(),
        "source": meta.get("source") or "",
        "updated": meta.get("updated") or "",
        "dir": _dir(),
    }


def save_bytes(raw: bytes, source: str = "") -> dict:
    """把形象图写进 data/avatar/avatar.png（统一转 PNG，免得前端按后缀猜 mime）。"""
    if not raw:
        return {"ok": False, "error": "图片数据为空"}
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        # 太大的图缩一下：前端只显示 ~150px 宽，原图 4000px 白占内存
        max_side = 1200
        if max(im.size) > max_side:
            r = max_side / float(max(im.size))
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                           Image.LANCZOS)
        im.save(_img_path(), "PNG")
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "保存形象失败：%s: %s" % (type(e).__name__, e)}
    _write_json(_META_FILE, {"source": source or "",
                             "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    return info()


def save_from_path(src: str, source: str = "") -> dict:
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "找不到图片文件：%s" % src}
    with open(src, "rb") as f:
        return save_bytes(f.read(), source=source or os.path.basename(src))


def save_from_b64(b64: str, source: str = "") -> dict:
    import base64
    s = (b64 or "").strip()
    if "," in s[:80] and s.lower().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s)
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "base64 解码失败：%s" % e}
    return save_bytes(raw, source=source or "b64")


def reset() -> dict:
    p = _img_path()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return info()


# ---------- 看屏幕（唤醒时"看看你在干嘛"） ----------
def look(question: str = "", ds_key: str = "") -> dict:
    """截一张当前应用的图，交给视觉模型说一句看到什么。

    ⚠️ 截的是**本应用自己的页面**（localhost:8000），不是整个桌面 ——
       无头浏览器只能截网页。要截桌面得换 PIL.ImageGrab，那会把用户
       屏幕上的隐私内容也拍进去，默认不做。
    """
    from . import vision
    try:
        shot = vision.screenshot()
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "截图失败：%s: %s" % (type(e).__name__, e)}
    if not shot.get("ok"):
        return shot
    q = (question or "").strip() or (
        "这是用户正在用的 AI 助手界面截图。用**一句中文口语**说说你看到"
        "用户在做什么（比如在看某个面板、在读某段回答）。不超过 30 字，"
        "不要列点、不要评价界面好坏。")
    r = vision.describe(shot.get("path") or shot.get("image_path") or "", q, ds_key)
    out = dict(r)
    out["shot"] = shot.get("path") or shot.get("image_path") or ""
    return out
