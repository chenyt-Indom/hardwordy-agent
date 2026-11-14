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
def _energy_curve(y, sr, n, fps):
    """每帧音频能量（0..1），让头跟着说话节奏动，而不是机械匀速摆。"""
    e = np.zeros(n, np.float32)
    if n <= 0 or len(y) == 0:
        return e
    for i in range(n):
        a = int(i / float(fps) * sr)
        b = int((i + 1) / float(fps) * sr)
        if b > a and a < len(y):
            seg = np.asarray(y[a:min(b, len(y))], np.float32)
            if seg.size:
                e[i] = float(np.sqrt(np.mean(seg * seg)))
    ref = float(np.percentile(e, 95)) if n > 1 else float(e.max())
    if ref < 1e-6:
        return e
    return np.clip(e / ref, 0.0, 1.0)


def _apply_motion(frames, img_path, proc_side, y, sr, fps, blink, motion):
    """在 Wav2Lip 生成帧上叠加头部动作。

    优先走 25D 深度位移（3DMM 拟合出真实深度图，按头姿做位移 → 有真正的
    转动和体积感）；3D 模型缺失或拟合失败时，回退到旧的「整体平移 + 眼带
    压缩」，保证功能不会因为缺模型直接挂掉。
    """
    n = len(frames)
    if n == 0:
        return frames, "none"
    m = float(max(0.0, min(2.0, motion)))

    prep = None
    poses = None
    try:
        from . import avatar25d as A25
        if A25.available():
            prep = A25._prep3d(img_path, max_side=proc_side)
            poses = A25._pose_curve(n, fps, _energy_curve(y, sr, n, fps))
    except Exception:                                   # noqa: BLE001
        prep, poses = None, None

    if prep is None or poses is None:
        # ---- 回退：旧的平移 + 眼带压缩（无旋转，但保证能动）
        out = []
        for i, fr in enumerate(frames):
            t = i / float(fps)
            if m > 0.01:
                fr = _head_move(fr, (0, 0, fr.shape[1], fr.shape[0]), t,
                                amp_x=9.0 * m, amp_y=6.0 * m)
            if blink:
                fr = _blink_frame(fr, (0, 0, fr.shape[1], fr.shape[0]), t)
            out.append(fr)
        return out, "pan"

    # ---- 25D：旋转 + 平移 + 缩放 + 眨眼（眼睛位置来自 3DMM，比固定眼带准）
    out = []
    for i, fr in enumerate(frames):
        yaw, pit, rol, tx, ty, scl, bl = poses[i]
        if abs(m - 1.0) > 1e-3:
            yaw *= m
            pit *= m * PITCH_BOOST
            rol *= m
            tx *= m
            ty *= m
            scl = 1.0 + (scl - 1.0) * m
        else:
            pit *= PITCH_BOOST
        if m <= 0.01:
            out.append(fr)
            continue
        fr = A25._warp(fr, prep, yaw=yaw, pitch=pit, roll=rol,
                       tx=tx, ty=ty, scl=scl)
        if blink and bl > 0.02:
            fr = A25._blink(fr, prep, min(1.0, bl * max(m, 0.5)),
                            yaw, pit, tx, ty)
        out.append(fr)
    return out, "25d"


# 点头（pitch）在正脸照片上的透视变化天然很弱（实测 pitch=10° 全图差仅 2.0，
# 而 yaw=6° 有 1.06 但左右不对称、肉眼立刻能看出）。所以给 pitch 单独加一档
# 增益，让「说话时点头」这个最重要的动作看得出来。
PITCH_BOOST = 2.0


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
           on_progress=None, max_side=1024, motion=1.0):
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
    # 尺寸策略（两条线分开，很重要）：
    #   · 处理尺寸 proc —— Wav2Lip 只在 96x96 上生成嘴部，喂给它更大的图**不会**
    #     增加嘴部信息量，反而让脸框更大、贴回时放大倍数更高。所以处理尺寸
    #     取 min(用户选择, 原图长边)，绝不上采样原图。
    #   · 输出尺寸 out —— 用户要的画面尺寸，靠渲染完再放大（纯插值）。
    # 结果：选 1536 得到更大的画面，但嘴部细节仍由 96x96 决定 —— 这点已在
    # 前端选项文案里如实写明，不吹成"更清晰"。
    H, W = img.shape[:2]
    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        max_side = 1024
    max_side = max(480, min(1920, max_side))
    proc_side = min(max_side, max(H, W))
    if max(H, W) > proc_side:
        s = proc_side / float(max(H, W))
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
    motion_mode = "none"
    t_motion = 0.0
    if head or blink or motion > 0.01:
        t1 = time.time()
        frames, motion_mode = _apply_motion(
            frames, img_path, proc_side, y, sr, fps, blink,
            motion if head else 0.0)
        t_motion = time.time() - t1

    # 输出放大：处理尺寸之上只做插值放大（libx264 要求宽高偶数）
    scale = max_side / float(max(W, H))
    if scale > 1.001:
        oW = max(2, int(round(W * scale / 2.0)) * 2)
        oH = max(2, int(round(H * scale / 2.0)) * 2)
        frames = [cv2.resize(f, (oW, oH), interpolation=cv2.INTER_CUBIC)
                  for f in frames]
        W, H = oW, oH

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
            "motion": motion_mode, "motion_sec": round(t_motion, 2),
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
        "max_side": max(480, min(1920, int(max_side or 1024))),
        "lipsync_label": "Wav2Lip 生成式口型",
        "audio_url": a.get("url") or "",
        "audio_engine": a.get("engine") or "",
        "text": t[:200],
    }
