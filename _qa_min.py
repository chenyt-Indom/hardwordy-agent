# -*- coding: utf-8 -*-
"""最小化：生成 + 存图，不做复杂分析"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
t00 = time.time()
def log(m): print("[%5.1fs] %s" % (time.time()-t00, m), flush=True)

from backend import avatar_lipsync as AL, avatar2 as A2, wav2lip_engine as W2L
log("import ok")

sr, y = A2._read_wav("_qa_out/s.wav")
log("audio %.2fs" % (len(y)/sr))

img = cv2.imread("data/avatar2/avatar.png")
H0, W0 = img.shape[:2]
log("img %dx%d" % (W0, H0))

eng = AL._engine()
log("engine ok")

frames, meta = eng.render(img, y.astype(np.float32))
log("wav2lip %d frames" % len(frames))

from backend import avatar25d as A25
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
log("3dmm ok")

poses = A25._pose_curve(len(frames), W2L.FPS, AL._energy_curve(y, sr, len(frames), W2L.FPS))
out = []
for i, fr in enumerate(frames):
    yaw, pit, rol, tx, ty, scl, bl = poses[i]
    out.append(A25._warp(fr, prep, yaw=max(-7.5, min(7.5, yaw*1.3)),
                         pitch=pit*2.0, roll=max(-5, min(5, rol*1.3)),
                         tx=tx*1.3, ty=ty*1.3, scl=scl, iters=1))
log("motion ok")

cv2.imwrite("_qa_out/cmp_orig.png", img)
cv2.imwrite("_qa_out/cmp_fix.png", out[len(out)//3])
cv2.imwrite("_qa_out/cmp_fix2.png", out[len(out)//2])
log("saved")
