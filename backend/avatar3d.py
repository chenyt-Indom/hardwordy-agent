# -*- coding: utf-8 -*-
"""3D 数字人：真 3D 人脸网格重建 + 音频驱动口型 + 头姿 + 眨眼 → mp4。

和 avatar2 的分工：
  avatar2 = 2D 形变（快，只能正面）
  avatar3d = 3D 网格重建（慢，能转头/侧脸，五官由网格拓扑保证）

依赖：face3d（ONNX 重建 + 软渲染）+ avatar2 的音频分析（复用，不重复实现）
"""
import math
import os
import subprocess
import tempfile

import cv2
import numpy as np

import avatar2 as A2
import face3d

FPS = 25
DEFAULT_SIZE = 512


def _load_img(path):
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    return img


def _head_pose(t, dur, strength=1.0):
    """说话时的头部微动：慢速摇头 + 轻微点头，多个不同周期叠加避免机械感。"""
    ph = t * 2.0 * math.pi
    ry = (5.0 * math.sin(ph * 0.31) + 2.2 * math.sin(ph * 0.73 + 1.1)) * strength
    rx = (2.6 * math.sin(ph * 0.23 + 0.7) + 1.1 * math.sin(ph * 0.61)) * strength
    rz = (1.4 * math.sin(ph * 0.17 + 2.0)) * strength
    return ry, rx, rz


def _blink_curve(n, fps, seed=0):
    """自然眨眼：每 2.5~5.5 秒一次，每次约 0.14 秒。"""
    rng = np.random.RandomState(seed)
    out = np.zeros(n, np.float32)
    t = 0.6 + rng.rand() * 1.5
    while t * fps < n:
        c = int(t * fps)
        w = max(2, int(0.14 * fps))
        for k in range(w):
            i = c + k
            if 0 <= i < n:
                out[i] = max(out[i], math.sin(math.pi * k / w))
        t += 2.5 + rng.rand() * 3.0
    return out


def render(img_path, wav_path, out_mp4, fps=FPS, size=DEFAULT_SIZE,
           amp=1.2, head_move=1.0, blink_on=True, words=None,
           on_progress=None, limit_sec=0.0):
    """把形象图 + 音频渲染成 3D 数字人视频。

    amp      : 口型幅度 0.6~2.0（作用在音频张口曲线上）
    head_move: 头部微动强度 0~2（0 = 完全不动）
    """
    img = _load_img(img_path)
    if img is None:
        raise ValueError("形象图读不出来：%s" % img_path)
    f3 = face3d.get()
    model = f3.build(img)
    if model is None:
        raise ValueError("没在形象图里检测到人脸")

    wav16 = A2._to_wav16k(wav_path)
    sr, y = A2._read_wav(wav16)
    if y is None or len(y) == 0:
        raise ValueError("音频读不出来")
    if words is None:
        try:
            words = A2._asr_words(wav16)
        except Exception:                                   # noqa: BLE001
            words = []

    params = A2._build_params(y, sr, words, fps=fps, amp=amp)
    n = len(params)
    if limit_sec and limit_sec > 0:
        n = min(n, int(limit_sec * fps))
    if n <= 0:
        raise ValueError("音频太短")

    blinks = _blink_curve(n, fps) if blink_on else np.zeros(n, np.float32)

    tex = model["crop"]
    uv = model["uv"]
    tri = f3.tri
    base = model["pts_front"]
    dur = n / float(fps)

    W = H = int(size) // 2 * 2
    tmpdir = tempfile.mkdtemp(prefix="av3d_")
    frames = os.path.join(tmpdir, "f%05d.png")
    scale = W / 256.0

    for i in range(n):
        t = i / float(fps)
        op = params[i][0]
        ry, rx, rz = _head_pose(t, dur, head_move)
        frame = face3d.render_avatar(model, tex, uv, tri,
                                     ry=ry, rx=rx, rz=rz,
                                     openness=op, blink=float(blinks[i]),
                                     size=256, pts_base=base)
        if scale != 1.0:
            frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_CUBIC)
        cv2.imencode(".png", frame)[1].tofile(frames % i)
        if on_progress and (i % 10 == 0 or i == n - 1):
            on_progress(i + 1, n)

    cmd = [A2.ffmpeg(), "-y", "-loglevel", "error",
           "-framerate", str(fps), "-i", frames,
           "-i", wav16,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 合成失败：%s" % (r.stderr or "")[-500:])

    # 清帧
    try:
        for fn in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, fn))
        os.rmdir(tmpdir)
    except OSError:
        pass
    return {"ok": True, "path": out_mp4, "frames": n,
            "duration": round(dur, 2), "size": [W, H]}


if __name__ == "__main__":
    import sys
    import time

    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        face3d.ROOT, "data", "avatar2", "avatar.png")
    wav = sys.argv[2] if len(sys.argv) > 2 else ""
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        face3d.ROOT, "data", "avatar2", "videos", "_test3d.mp4")
    if not wav:
        print("用法: python avatar3d.py <图> <wav> [out.mp4]")
        sys.exit(2)
    t0 = time.time()
    r = render(src, wav, out, limit_sec=3.0,
               on_progress=lambda i, n: print(f"  {i}/{n}", flush=True))
    print(r, f"{time.time()-t0:.1f}s")
