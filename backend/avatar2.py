# -*- coding: utf-8 -*-
"""数字人：一张图 + 一段话 → 会说话的视频（2026-11-15 改为云端 API）

为什么整体换成云端
------------------
本地走过三条路，全部到不了「自然」，根因是同一个：**它们都只重绘 96~256px
的小嘴**，而形象原图的嘴唇有几百像素宽。

  · 参数化 2D 形变（本文件的老版本）：源图是闭嘴的，先天没有「张嘴」的像素，
    只能把唇缝那条 3px 暗线拉宽，唇形轮廓本身不动 —— 看着仍像闭嘴。
    实测开口度中位 0.48 时，唇缝暗带只从 29px 涨到 35px。
  · Wav2Lip（ONNX 96x96）：嘴位整体偏移、口腔糊成色块，唇部锐度只有原图 41%。
  · SadTalker 256：能跑通（锐度 57.2 > Wav2Lip 49.5），但 256x256 是模型硬上限，
    贴回原图还是糊。

唯一能到「最好」的是生成式云端模型：它重绘整张脸，自带真实的牙齿、舌头、
口腔深度和下颌运动，而不是在静态图上拉扯像素。

当前走阿里云百炼 Wan2.2-S2V
--------------------------
  · 只要一个 API Key。用**通用域名** dashscope.aliyuncs.com，不需要业务空间 ID
    （实测：假 Key 真连返回 401 InvalidApiKey，说明路径有效、只差 Key）。
  · 单次音频必须 < 20 秒 → 长文本自动切段 + ffmpeg 拼接（在 avatar_cloud 里）。
  · 生成要 1~3 分钟 → 前端走「提交 → 轮询」，不用长连接。

本地那套渲染代码（avatar25d / avatar3d / avatar_mouth / avatar_lipsync /
wav2lip_engine / wav2lip_onnx / sadtalker_cpu / face3d / avatar_vision /
avatar_phoneme / avatar_tune / talkhead）已于 2026-11-15 整体删除。

存的东西都在 data/avatar2/：
  · avatar.png   当前形象
  · meta.json    来源与时间
  · videos/      生成的 mp4
  · cloud.json   云端配置（API Key / 分辨率）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from urllib.parse import quote

logger = logging.getLogger(__name__)

_IMG = "avatar.png"
_META = "meta.json"

# 前端传哪些 engine 值算「走云端」。本地渲染已删除，所以现在无论传什么，
# speak() 都只走云端 —— 这个元组保留是为了兼容老前端/老调用方。
CLOUD_ENGINES = ("cloud", "api", "wan", "wan2.2-s2v", "s2v", "auto", "param", "")


# ---------------------------------------------------------------- 目录与配置
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir() -> str:
    d = os.path.join(_root(), "data", "avatar2")
    os.makedirs(d, exist_ok=True)
    return d


def _vdir() -> str:
    d = os.path.join(_dir(), "videos")
    os.makedirs(d, exist_ok=True)
    return d


def _img_path() -> str:
    return os.path.join(_dir(), _IMG)


def _read_json(name: str, default: dict) -> dict:
    p = os.path.join(_dir(), name)
    if not os.path.isfile(p):
        return dict(default)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else dict(default)
    except Exception:                                       # noqa: BLE001
        return dict(default)


def _write_json(name: str, d: dict) -> None:
    p = os.path.join(_dir(), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def ffmpeg() -> str:
    """拿 ffmpeg 可执行文件。优先 imageio_ffmpeg 自带的（项目其它模块也这么取）。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:                                       # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    p = shutil.which("ffmpeg")
    return p or ""


# ---------------------------------------------------------------- 形象图
def info() -> dict:
    p = _img_path()
    has = os.path.isfile(p)
    meta = _read_json(_META, {})
    size = None
    if has:
        try:
            from PIL import Image
            with Image.open(p) as im:
                size = list(im.size)
        except Exception:                                   # noqa: BLE001
            size = None
    return {
        "ok": True,
        "has_image": has,
        # 时间戳做缓存击穿：换了形象前端立刻看到新的
        "url": ("/api/avatar2/raw?v=%d" % int(os.path.getmtime(p))) if has else "",
        "size": size,
        "source": meta.get("source") or "",
        "updated": meta.get("updated") or "",
        "dir": _dir(),
        "videos_dir": _vdir(),
    }


def save_bytes(raw: bytes, source: str = "") -> dict:
    """把形象图写进 data/avatar2/avatar.png（统一转 PNG）。

    云端模型自己处理整张图，所以这里**不做嘴位检测**了 —— 本地形变时代要
    知道嘴在哪，现在不需要。
    """
    if not raw:
        return {"ok": False, "error": "图片数据为空"}
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
        else:
            im = im.convert("RGB")
        # 太大的图缩一下：云端也要上传，4000px 原图既慢又没必要
        max_side = 1400
        if max(im.size) > max_side:
            r = max_side / float(max(im.size))
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                           Image.LANCZOS)
        im.save(_img_path(), "PNG")
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": "保存形象失败：%s: %s" % (type(e).__name__, e)}
    _write_json(_META, {"source": source or "",
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
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": "base64 解码失败：%s" % e}
    return save_bytes(raw, source=source or "b64")


def reset() -> dict:
    p = _img_path()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            logger.debug("silent-except", exc_info=True)
    return info()


# ---------------------------------------------------------------- 云端状态
def _cloud_status() -> dict:
    """云端配置状态。avatar_cloud 出任何问题都不能让 status() 崩掉。"""
    try:
        from . import avatar_cloud as ac
        c = ac.config()
        return {
            "ready": bool(c.get("ready")),
            "has_key": bool(c.get("has_key")),
            "resolution": c.get("resolution") or "480P",
            "model": c.get("model") or "wan2.2-s2v",
            "missing": c.get("missing") or [],
            "max_audio_sec": ac.MAX_AUDIO_SEC,
            "key_url": ac.KEY_URL,
            "signup_url": ac.SIGNUP_URL,
        }
    except Exception as e:                                  # noqa: BLE001
        return {"ready": False, "has_key": False,
                "missing": ["云端模块不可用"], "error": "%s: %s" % (type(e).__name__, e)}


def _cloud_precheck():
    """云端生成的前置检查。返回 (ok, 错误 dict 或 None)。"""
    from . import avatar_cloud as ac
    c = ac.config()
    if not c.get("ready"):
        return False, {
            "ok": False, "need_key": True,
            "error": "云端口型还没配置：缺%s" % "、".join(c.get("missing") or []),
            "signup_url": ac.SIGNUP_URL, "key_url": ac.KEY_URL,
            "missing": c.get("missing") or [],
        }
    return True, None


def _resolve_mood(mood: str, rate, text: str) -> dict:
    """把「心情 + 语速」解析成最终 TTS 参数。心情模块不可用时退化成默认值。"""
    try:
        from . import avatar2_expr as ex
        return ex.resolve(mood, rate, text)
    except Exception:                                       # noqa: BLE001
        logger.debug("心情解析失败，用默认值", exc_info=True)
        return {"key": "calm", "label": "平静", "rate": 0, "style": ""}


# ---------------------------------------------------------------- 生成
def speak_cloud(text: str, voice: str = "", rate=None, style: str = "",
                resolution: str = "") -> dict:
    """文本 → TTS → 云端 Wan2.2-S2V → mp4（同步版，会阻塞 1~3 分钟）。"""
    from . import avatar_cloud as ac

    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    ok, err = _cloud_precheck()
    if not ok:
        return err

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine="auto", style=(style or "").strip())
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}

    r = ac.generate(_img_path(), a.get("path") or "", resolution=resolution)
    if not r.get("ok"):
        return r
    r["url"] = "/api/avatar2/video/" + quote(r.get("name") or "")
    return r


def speak_async(text: str, voice: str = "", rate=None, style: str = "",
                resolution: str = "", mode: str = "",
                force_base: bool = False) -> dict:
    """云端生成的异步版：立刻返回 job_id，前端轮询进度。

    一次生成 1~3 分钟（长文本切多段更久），挂在 HTTP 长连接上很容易被中间层
    掐断，前端也拿不到「第几段」。所以统一走「提交 → 轮询」。
    """
    from . import avatar_cloud as ac

    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    ok, err = _cloud_precheck()
    if not ok:
        return err

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine="auto", style=(style or "").strip())
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}

    jid = ac.generate_async(_img_path(), a.get("path") or "", resolution=resolution,
                            mode=mode, force_base=force_base)
    return {"ok": True, "job_id": jid}


def speak(text: str, voice: str = "", rate=None, style: str = "",
          engine: str = "auto", mood: str = "", expr: bool = False,
          amp: float = 1.0) -> dict:
    """文本 → TTS → 云端口型 → mp4。这是给工具/前端调的主入口。

    engine / expr / amp 是本地形变时代留下的参数，现在不再起作用，
    保留形参只是为了不打断既有调用方（tools.py 等）。

    mood  = 心情：auto/""（按语境自动判断）/ happy/calm/serious/gentle/
            sad/excited/surprise/worried/confident
    rate  = 语速 -50~50；None 表示用心情的建议值
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    cfg = _resolve_mood(mood, rate, t)
    final_rate = int(cfg.get("rate", 0))
    final_style = (style or "").strip() or str(cfg.get("style") or "")

    r = speak_cloud(t, voice=voice, rate=final_rate, style=final_style)
    if r.get("ok"):
        r.update({
            "mood": cfg.get("key", "calm"),
            "mood_label": cfg.get("label", "平静"),
            "mood_auto": bool(cfg.get("auto")),
            "mood_confident": bool(cfg.get("auto_confident")),
            "mood_why": cfg.get("auto_why") or [],
            "rate": final_rate,
            "style": final_style,
        })
    return r


def status() -> dict:
    """能力自检：让前端如实告诉用户现在能做什么、缺什么。"""
    exe = ffmpeg()
    cloud = _cloud_status()
    return {
        "ok": True,
        "ffmpeg": bool(exe),
        "ffmpeg_path": exe,
        "has_image": os.path.isfile(_img_path()),
        # 只剩云端一条路，前端据此显示「云端口型」而不是本地形变
        "engines": ["cloud"],
        "cloud": cloud,
        "note": ("云端 Wan2.2-S2V，可以生成" if cloud.get("ready")
                 else "还没填 API Key，去百炼控制台拿一个（只要 Key，不需要业务空间 ID）"),
    }


# ---------------------------------------------------------------- 视频管理
def list_videos(limit: int = 50, scope: str = "avatar") -> list:
    """列出可浏览的 mp4，按时间倒序。

    scope="avatar"  → 数字人生成的视频（data/avatar2/videos）
    scope="library" → 生成文库里的 mp4（视频编辑产物、样片等）
    scope="all"     → 两边合并
    每项带 scope 字段，删除时按它决定删哪边（两边删除语义不同：
    数字人视频直接删；文库文件进回收站，可恢复）。
    """
    sc = (scope or "avatar").lower()
    if sc not in ("avatar", "library", "all"):
        sc = "avatar"
    items = []

    if sc in ("avatar", "all"):
        d = _vdir()
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for f in names:
            if not f.lower().endswith(".mp4"):
                continue
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append({"name": f, "bytes": st.st_size,
                          "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(st.st_mtime)),
                          "_ts": st.st_mtime,
                          "scope": "avatar", "rel": f, "folder": "",
                          "url": "/api/avatar2/video/" + quote(f)})

    if sc in ("library", "all"):
        try:
            from . import doclib
            for f in doclib.list_files():
                if f.get("ext") not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
                    continue
                items.append({"name": f["name"], "bytes": f["size"],
                              "mtime": f["modified"], "_ts": f["mtime"],
                              "scope": "library", "rel": f["rel"],
                              "folder": f.get("folder", ""),
                              "url": "/api/doclib/download?rel="
                                     + quote(f["rel"])})
        except Exception:                                   # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    items.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for it in items:
        it.pop("_ts", None)
    return items[:limit]


def video_path(name: str) -> str:
    """安全取视频路径（挡目录穿越）。"""
    base = os.path.basename(name or "")
    if not base or not base.lower().endswith(".mp4"):
        return ""
    p = os.path.join(_vdir(), base)
    return p if os.path.isfile(p) else ""


def delete_video(name: str, scope: str = "avatar") -> dict:
    """删视频。scope=library 时走 doclib（进回收站可恢复）。"""
    sc = (scope or "avatar").lower()
    if sc == "library":
        try:
            from . import doclib
            return doclib.delete_file(name)
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    p = video_path(name)
    if not p:
        return {"ok": False, "error": "没有这个视频"}
    try:
        os.remove(p)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def concat_videos(names, out_name: str = "") -> dict:
    """把多段数字人视频按顺序拼成一个 mp4（长文本分段生成后合并用）。

    分段产物编码参数一致（h264 + aac、同分辨率同帧率），所以先用
    concat demuxer + `-c copy` 直接流拷贝，几秒完成；万一参数不一致
    导致 copy 失败，再退回重编码一次（慢但一定能成）。
    """
    exe = ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not names:
        return {"ok": False, "error": "没有可拼接的视频"}
    paths = []
    for n in names:
        p = os.path.join(_vdir(), os.path.basename(str(n)))
        if not os.path.isfile(p):
            return {"ok": False, "error": "分段视频不存在：%s" % n}
        paths.append(p)
    if len(paths) == 1:
        nm = os.path.basename(paths[0])
        return {"ok": True, "name": nm, "parts": 1,
                "duration": _probe_duration(paths[0]),
                "url": "/api/avatar2/video/" + quote(nm)}

    name = out_name or ("avatar_" + time.strftime("%Y%m%d_%H%M%S") + "_"
                        + uuid.uuid4().hex[:4] + ".mp4")
    out = os.path.join(_vdir(), os.path.basename(name))
    lst = os.path.join(_vdir(), "_concat_%s.txt" % uuid.uuid4().hex[:8])
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                # concat demuxer 的路径要转义单引号；反斜杠统一成 / 更稳
                f.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-f", "concat",
                            "-safe", "0", "-i", lst, "-c", "copy",
                            "-movflags", "+faststart", out],
                           capture_output=True, timeout=300)
        ok = r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000
        if not ok:
            # 兜底：参数不一致时重编码
            r2 = subprocess.run([exe, "-y", "-loglevel", "error", "-f", "concat",
                                 "-safe", "0", "-i", lst,
                                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                                 "-movflags", "+faststart", out],
                                capture_output=True, timeout=900)
            if r2.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 1000:
                return {"ok": False,
                        "error": "拼接失败：%s" % ((r2.stderr or r.stderr or b"")[:300]
                                                  .decode("utf-8", "ignore"))}
    except subprocess.TimeoutExpired as te:
        # ffmpeg 卡住不能把整个 HTTP 请求拖死：超时就明确报错
        # （/api/avatar2/concat 端点本身没有 try，不在这里兜就会变成 500）
        return {"ok": False,
                "error": "ffmpeg 拼接超时（%d 秒）：分段视频可能损坏，或编码参数不一致"
                         % int(getattr(te, "timeout", 0) or 0)}
    finally:
        try:
            os.remove(lst)
        except Exception:                                   # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    # 时长直接读合并后的成品：累加各段时若某一段探测失败会返回 0，
    # 实测就出现过 101 秒的片子报成 54.6 秒（用户会以为丢了一半）。
    dur = round(_probe_duration(out), 2)
    return {"ok": True, "name": os.path.basename(out), "parts": len(paths),
            "duration": dur, "bytes": os.path.getsize(out),
            "url": "/api/avatar2/video/" + quote(os.path.basename(out))}


def _probe_duration(path: str) -> float:
    """读 mp4 时长（秒）；失败返回 0，不影响拼接结果。"""
    try:
        import re as _re
        exe = ffmpeg()
        r = subprocess.run([exe, "-i", path], capture_output=True, timeout=30)
        s = (r.stderr or b"").decode("utf-8", "ignore")
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", s)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:                                       # noqa: BLE001
        return 0.0
