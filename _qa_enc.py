# -*- coding: utf-8 -*-
"""实测 H.264 编码参数对清晰度的影响：同一批帧，多组 preset/crf 编码后解码比对。"""
import sys, os, subprocess, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
prep = A25._prep3d(P, max_side=1024)
img = prep["img"]
H, W = img.shape[:2]
print("source", W, H, "sharp=%.2f" % cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())

# 造 40 帧带动作的帧
frames = []
for i in range(40):
    t = i / 40.0
    yaw = 6.0 * np.sin(2 * np.pi * t)
    pitch = 3.0 * np.sin(4 * np.pi * t)
    o = A25._warp(img, prep, yaw=float(yaw), pitch=float(pitch),
                  roll=float(1.5 * np.sin(2 * np.pi * t)),
                  tx=float(6 * np.sin(2 * np.pi * t)),
                  ty=float(2 * np.sin(4 * np.pi * t)), scl=1.0)
    frames.append(o)

exe = None
for c in ("ffmpeg", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "ffmpeg.exe")):
    try:
        subprocess.run([c, "-version"], capture_output=True, timeout=10)
        exe = c
        break
    except Exception:
        pass
print("ffmpeg:", exe)

tmpd = tempfile.mkdtemp()
wav = os.path.join(tmpd, "a.wav")
# 1 秒 16k 静音
import wave, struct
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 16000)

cases = [
    ("veryfast/crf20 (当前)", "veryfast", "20"),
    ("medium/crf18", "medium", "18"),
    ("medium/crf16", "medium", "16"),
    ("slow/crf16", "slow", "16"),
    ("slow/crf14", "slow", "14"),
]
for name, preset, crf in cases:
    out = os.path.join(tmpd, "t_%s_%s.mp4" % (preset, crf))
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", "25", "-i", "-", "-i", wav,
           "-c:v", "libx264", "-preset", preset, "-crf", crf,
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for fr in frames:
        p.stdin.write(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB).tobytes())
    p.stdin.close()
    err = p.stderr.read()
    p.wait()
    if not os.path.isfile(out):
        print(name, "FAIL", err[:200]); continue
    cap = cv2.VideoCapture(out)
    ok, dec = cap.read()
    cap.release()
    if not ok:
        print(name, "decode fail"); continue
    dec = cv2.resize(dec, (W, H))
    g = cv2.cvtColor(dec, cv2.COLOR_BGR2GRAY)
    sharp = cv2.Laplacian(g, cv2.CV_64F).var()
    psnr = cv2.PSNR(frames[0], dec)
    sz = os.path.getsize(out) / 1024.0
    print("%-24s sharp=%6.2f  psnr=%5.2f  size=%7.0fKB" % (name, sharp, psnr, sz))

# 无损基准
g0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
print("%-24s sharp=%6.2f" % ("未编码原始帧", cv2.Laplacian(g0, cv2.CV_64F).var()))
