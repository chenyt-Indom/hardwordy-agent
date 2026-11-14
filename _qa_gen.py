# -*- coding: utf-8 -*-
"""只出片，逐步打印耗时"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
print("import 完成", flush=True)
from backend import avatar_lipsync as AL, avatar2 as A2, wav2lip_engine as W2L
print("模块加载完成", flush=True)

wav = "_qa_out/s.wav"
sr, y = A2._read_wav(wav)
print("音频 %.2fs" % (len(y)/sr), flush=True)

img = cv2.imread("data/avatar2/avatar.png")
t = time.time()
eng = AL._engine()
print("引擎 %.1fs" % (time.time()-t), flush=True)

t = time.time()
frames, meta = eng.render(img, y.astype(np.float32))
print("Wav2Lip %d 帧 %.1fs" % (len(frames), time.time()-t), flush=True)

from backend import avatar25d as A25
t = time.time()
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
print("3DMM 拟合 %.1fs" % (time.time()-t), flush=True)

poses = A25._pose_curve(len(frames), W2L.FPS, AL._energy_curve(y, sr, len(frames), W2L.FPS))
t = time.time()
out = []
for i, fr in enumerate(frames):
    yaw, pit, rol, tx, ty, scl, bl = poses[i]
    out.append(A25._warp(fr, prep, yaw=max(-7.5, min(7.5, yaw*1.3)),
                         pitch=pit*2.0, roll=max(-5, min(5, rol*1.3)),
                         tx=tx*1.3, ty=ty*1.3, scl=scl, iters=1))
print("动作层 %d 帧 %.1fs" % (len(out), time.time()-t), flush=True)

np.save("_qa_out/frames.npy", np.array(out, dtype=np.uint8)[::2])
print("已存采样帧", flush=True)
