"""实验：Wav2Lip 帧能否套上 25D 的深度位移头姿（真正的旋转，不只是平移）"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2

from backend import avatar2 as A2
from backend import avatar25d as A25

img_path = A2._img_path()
print("形象图:", img_path)
A2.MAX_SIDE = 1024          # 和 lipsync 的 proc_side 对齐

t0 = time.time()
prep = A25._prep3d(img_path)
print("prep 耗时: %.2fs" % (time.time() - t0))
print("wh:", prep["wh"], "roi:", prep["roi"])
print("fw/fh: %.1f / %.1f  scale=%.2f" % (prep["fw"], prep["fh"], prep["scale"]))
print("eyes:", prep["eyes"], "gap=%.1f" % prep["eye_gap"])

img = cv2.imread(img_path)
s = 1024 / float(max(img.shape[:2]))
img = cv2.resize(img, (int(img.shape[1] * s) // 2 * 2, int(img.shape[0] * s) // 2 * 2),
                 interpolation=cv2.INTER_AREA)
print("img shape:", img.shape, "prep wh:", prep["wh"])

poses = [
    ("yaw-6", dict(yaw=-6)),
    ("yaw+6", dict(yaw=6)),
    ("pitch-5", dict(pitch=-5)),
    ("pitch+5", dict(pitch=5)),
    ("roll+4", dict(roll=4)),
    ("combo", dict(yaw=5, pitch=-4, roll=3, tx=10, ty=-7, scl=1.012)),
]
os.makedirs("_t_out", exist_ok=True)
for name, kw in poses:
    t1 = time.time()
    out = A25._warp(img, prep, **kw)
    dt = (time.time() - t1) * 1000
    diff = np.abs(out.astype(np.int16) - img.astype(np.int16)).mean()
    cv2.imwrite("_t_out/warp_%s.png" % name, out)
    print("  %-8s %6.1fms  meanDiff=%.2f" % (name, dt, diff))

for a in (0.5, 1.0):
    out = A25._blink(img, prep, a)
    diff = np.abs(out.astype(np.int16) - img.astype(np.int16)).mean()
    cv2.imwrite("_t_out/blink_%.1f.png" % a, out)
    print("  blink %.1f  meanDiff=%.2f" % (a, diff))

rows = []
for name, _ in poses:
    im = cv2.imread("_t_out/warp_%s.png" % name)
    cv2.putText(im, name, (12, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 255), 3)
    rows.append(im)
grid = np.vstack([np.hstack(rows[:3]), np.hstack(rows[3:])])
grid = cv2.resize(grid, (grid.shape[1] // 2, grid.shape[0] // 2))
cv2.imwrite("_t_out/poses_grid.png", grid)
print("对比图:", grid.shape)
