"""Wav2Lip 驱动的口型同步数字人

与 avatar2 的区别：
  avatar2      —— 几何形变（拉伸嘴部），单图没有口腔信息，口型天然受限
  avatar_lipsync —— Wav2Lip 生成式模型，直接生成含牙齿/口腔的真实嘴型

复用 avatar2 的：TTS、音频转码、ffmpeg 编码、视频目录
新增：SCRFD 检测 + Wav2Lip ONNX 推理 + 眨眼/头动层
"""
import os
import subprocess
import time
import uuid

import numpy as np
import cv2

from . import avatar2 as A2
from . import wav2lip_engine as W2L
from .face_detect_scrfd import SCRFD

_MODELS = os.path.join(A2._root(), "data", "models", "wav2lip")
_ENGINE = None
_DET = None


def _paths():
    return {
        "onnx": os.path.join(_MODELS, "wav2lip_gan.onnx"),
        "det": os.path.join(_MODELS, "scrfd_2.5g_bnkps.onnx"),
        "mel": os.path.join(_MODELS, "_mel_basis.npy"),
        "sr": os.path.join(_MODELS, "bigface_x4.onnx"),
    }


def available() -> bool:
    p = _paths()
    return os.path.isfile(p["onnx"]) and os.path.isfile(p["det"])


def _engine():
    global _ENGINE, _DET
    if _ENGINE is None:
        p = _paths()
        _DET = SCRFD(p["det"])
        # 注：4x 超分(bigface_x4.onnx)实测在合成阶段是负收益
        # （嘴部最终显示尺寸远小于 384，超分高频被缩放与细节回注吃掉），故不启用
        _ENGINE = W2L.Wav2LipEngine(p["onnx"], _DET, mel_npy=p["mel"], threads=8)
    return _ENGINE


def info() -> dict:
    p = _paths()
    ok = available()
    d = {
        "available": ok,
        "engine": "wav2lip_gan (ONNX)",
        "model_dir": _MODELS,
        "files": {k: (os.path.isfile(v) and os.path.getsize(v)) or 0 for k, v in p.items()},
        "fps": W2L.FPS,
        "note": "生成式口型同步：直接生成嘴部像素，含口腔结构",
    }
    if ok:
        try:
            d["onnx_inputs"] = [i.name for i in _engine().sess.get_inputs()]
        except Exception as e:                          # noqa: BLE001
            d["available"] = False
            d["load_error"] = "%s: %s" % (type(e).__name__, e)
    return d


# ------------------------------------------------------------------ 动作层
def _blink_frame(frame, face_box, t, period=3.2, dur=0.18):
    """在眼部做上下位移模拟眨眼（只压缩眼裂，不改五官）"""
    x1, y1, x2, y2 = face_box
    h = y2 - y1
    # 眼带：人脸框 28%~46% 高度
    ey1 = int(y1 + h * 0.28)
    ey2 = int(y1 + h * 0.46)
    if ey2 - ey1 < 4:
        return frame
    # 眨眼相位
    ph = (t % period) / period
    blink_center = 0.82
    if not (blink_center <= ph <= blink_center + dur / period):
        return frame
    k = (ph - blink_center) / (dur / period)     # 0..1
    close = np.sin(np.pi * k)                    # 0->1->0
    band = frame[ey1:ey2, x1:x2]
    bh = band.shape[0]
    if bh < 6:
        return frame
    # 垂直压缩 + 轻微下移，模拟眼皮落下
    new_h = max(2, int(bh * (1.0 - 0.45 * close)))
    comp = cv2.resize(band, (band.shape[1], new_h), interpolation=cv2.INTER_LINEAR)
    out = frame.copy()
    out[ey1:ey2, x1:x2] = band                # 先保留原样
    top = ey1 + int((bh - new_h) * 0.35)
    out[top:top + new_h, x1:x2] = comp
    return out


def _head_move(frame, box, t, amp_x=9.0, amp_y=6.0):
    """整体亚像素级平移，让头部有呼吸感（边缘镜像填充，不留黑边）"""
    dx = amp_x * np.sin(2 * np.pi * t / 5.3) + 2.2 * np.sin(2 * np.pi * t / 2.1)
    dy = amp_y * np.sin(2 * np.pi * t / 6.7 + 1.1) + 1.4 * np.sin(2 * np.pi * t / 3.3)
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0]),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REFLECT_101)


# ------------------------------------------------------------------ 主流程
def render(img_path, wav_path, out_mp4, blink=True, head=True, fps=W2L.FPS,
           on_progress=None, max_side=1024):
    exe = A2.ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}
    if not available():
        return {"ok": False, "error": "Wav2Lip 模型未就绪，请先下载模型"}

    img = cv2.imread(img_path)
    if img is None:
        return {"ok": False, "error": "形象图读取失败"}
    # 限制尺寸（Wav2Lip 只要脸，大图徒增编码开销；但嘴部显示尺寸正比于原图，
    # 所以分辨率越高嘴部越大越清楚 —— 由前端「输出分辨率」决定）
    H, W = img.shape[:2]
    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        max_side = 1024
    max_side = max(480, min(1920, max_side))
    if max(H, W) > max_side:
        s = max_side / float(max(H, W))
        img = cv2.resize(img, (int(W * s) // 2 * 2, int(H * s) // 2 * 2),
                         interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]

    sr, y = A2._read_wav(wav_path)
    if sr != W2L.SR:
        # 重采样到 16k（线性插值，够用）
        n_new = int(len(y) * W2L.SR / sr)
        y = np.interp(np.linspace(0, len(y) - 1, n_new),
                      np.arange(len(y)), y).astype(np.float32)
        sr = W2L.SR
    dur = len(y) / float(sr)
    if dur < 0.2:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}
    if dur > A2.MAX_SECONDS:
        return {"ok": False, "error": "音频 %.1f 秒，超过 %d 秒上限" % (dur, A2.MAX_SECONDS)}

    eng = _engine()
    t0 = time.time()
    frames, meta = eng.render(img, y.astype(np.float32))
    t_infer = time.time() - t0
    if not frames:
        return {"ok": False, "error": "没有生成任何帧"}
    box = meta["box"]

    # 动作层
    if blink or head:
        out_frames = []
        for i, fr in enumerate(frames):
            t = i / float(fps)
            if head:
                fr = _head_move(fr, box, t)
            if blink:
                fr = _blink_frame(fr, box, t)
            out_frames.append(fr)
        frames = out_frames

    n = len(frames)
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-",
           "-i", wav_path,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, fr in enumerate(frames):
            rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
            proc.stdin.write(rgb.tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                       # noqa: BLE001
                    pass
    except Exception as e:                              # noqa: BLE001
        try:
            proc.kill()
        except Exception:                               # noqa: BLE001
            pass
        return {"ok": False, "error": "编码中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
    err = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s" % (err or b"")[:400].decode("utf-8", "ignore")}
    return {"ok": True, "frames": n, "duration": round(dur, 2),
            "size": [W, H], "path": out_mp4,
            "bytes": os.path.getsize(out_mp4),
            "infer_sec": round(t_infer, 2),
            "box": box, "fps": fps}


def speak(text, voice="", rate=None, style="", engine="auto",
          blink=True, head=True, max_side=1024):
    """文本 → TTS → Wav2Lip → mp4"""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(A2._img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图"}
    if not available():
        return {"ok": False, "error": "Wav2Lip 模型未就绪"}

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=style or "")
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    audio = a.get("path") or ""
    try:
        wav = A2._to_wav16k(audio)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "音频转码失败：%s: %s" % (type(e).__name__, e)}

    name = "lipsync_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4] + ".mp4"
    out = os.path.join(A2._vdir(), name)
    r = render(A2._img_path(), wav, out, blink=blink, head=head,
               max_side=max_side)
    if not r.get("ok"):
        return r
    from urllib.parse import quote
    return {
        "ok": True, "name": name,
        "url": "/api/avatar2/video/" + quote(name),
        "path": out,
        "duration": r["duration"], "size": r["size"],
        "frames": r["frames"], "bytes": r["bytes"],
        "infer_sec": r.get("infer_sec"),
        "blink": bool(blink), "head": bool(head),
        "lipsync_label": "Wav2Lip 生成式口型",
        "audio_url": a.get("url") or "",
        "audio_engine": a.get("engine") or "",
        "text": t[:200],
    }
