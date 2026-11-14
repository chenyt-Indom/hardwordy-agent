"""诊断：逐帧检查形状/dtype，并捕获 ffmpeg stderr。"""
import os, sys, subprocess
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
from backend import avatar2 as A2
from backend import avatar25d as A25

img = A2._img_path()
p2 = A2._prep(img)
p3 = A25._prep3d(img)
print("p2 wh", p2["wh"], " p3 wh", p3["wh"])

wav = A2._to_wav16k(sorted(
    [os.path.join(ROOT, "data", "tts", f) for f in os.listdir(os.path.join(ROOT, "data", "tts"))
     if f.endswith(".mp3")], key=os.path.getmtime)[-1])
sr, y = A2._read_wav(wav)
params = A2._build_params(y, sr, [], A2.FPS, amp=1.2)
poses = A25._pose_curve(len(params), A2.FPS, [p[0] for p in params])
print("帧数", len(params), " poses 元组长度", len(poses[0]))

for i in [0, 1, 40]:
    op, wd = params[i]
    yaw, pit, rol, tx, ty, scl, bl = poses[i]
    fr = A25._mouth_frame(p2, op, wd, amp=1.2)
    a = np.asarray(fr.convert("RGB"))
    b = A25._warp(a, p3, yaw, pit, rol, tx, ty, scl)
    c = A25._blink(b, p3, bl, yaw, pit, tx, ty) if bl > 0.02 else b
    print("帧%d op=%.2f pose=(%.1f,%.1f,%.1f,%.1f,%.1f,%.4f,%.2f) shapes %s %s %s dtype %s"
          % (i, op, yaw, pit, rol, tx, ty, scl, bl, a.shape, b.shape, c.shape, c.dtype))

# 直接喂 ffmpeg 3 帧看报错
exe = A2.ffmpeg()
W, H = p3["wh"]
out = os.path.join(ROOT, "_p25d_diag.mp4")
cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
       "-s", "%dx%d" % (W, H), "-r", "25", "-i", "-", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", out]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    for i in range(3):
        op, wd = params[i]
        yaw, pit, rol, tx, ty, scl, bl = poses[i]
        fr = A25._mouth_frame(p2, op, wd, amp=1.2)
        a = np.asarray(fr.convert("RGB"))
        b = A25._warp(a, p3, yaw, pit, rol, tx, ty, scl)
        p.stdin.write(np.ascontiguousarray(b).tobytes())
    p.stdin.close()
    err = p.stderr.read()
    print("ffmpeg rc", p.wait(), "err", err[:300])
except Exception as e:
    print("写入失败:", type(e).__name__, e)
    print("ffmpeg err:", p.stderr.read()[:300] if p.stderr else b"")
    p.kill()
