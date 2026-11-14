# -*- coding: utf-8 -*-
"""诊断2：验证新的眨眼 ROI 与低通后的曲线抖动（读真实代码，不再硬编码）。"""
import os, sys, asyncio, subprocess, tempfile
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np

BASE = r"D:\local-multimodal-src"
IMG = os.path.join(BASE, "data", "avatar2", "avatar.png")
WAV = os.path.join(tempfile.gettempdir(), "_diag_blink.wav")

from backend import avatar_lipsync as AL
from backend import avatar25d as A25
from backend import avatar2 as A2

prep = A25._prep3d(IMG, max_side=1024)
H, W = prep["wh"]
gap = float(prep["eye_gap"])
ew = max(6.0, gap * 0.17)
eh = max(5.0, gap * 0.085)
print("=== 新眨眼 ROI（读代码实际值）===")
print("眼距 %.1f, 脸宽 %.1f" % (gap, prep["fw"]))
print("单眼 ROI 宽 %.0f px (%.0f%% 眼距, %.0f%% 脸宽), 高 %.0f px"
      % (2 * ew, 2 * ew / gap * 100, 2 * ew / prep["fw"] * 100, 2 * eh))
for i, (ex, ey) in enumerate(prep["eyes"]):
    print("  眼%d bbox x[%d,%d] y[%d,%d]"
          % (i, ex - ew, ex + ew, ey - eh, ey + eh))

sr, y = A2._read_wav(WAV)
fps = 25
n = int(len(y) / float(sr) * fps)
e = AL._energy_curve(y, sr, n, fps)
poses = A25._pose_curve(n, fps, e)
arr = np.array(poses, dtype=np.float64)
names = ["yaw", "pitch", "roll", "tx", "ty", "scl", "blink"]
thr = {"yaw": 0.35, "pitch": 0.30, "roll": 0.25,
       "tx": 0.9, "ty": 0.9, "scl": 0.0012, "blink": 0.40}
print("\n=== 低通后曲线（n=%d）===" % n)
print("%-7s %7s %9s %7s %8s" % ("项", "范围", "最大帧间跳", "p95跳", "超阈值帧"))
for k, nm in enumerate(names):
    v = arr[:, k]
    d = np.abs(np.diff(v)) if len(v) > 1 else np.array([0.0])
    print("%-7s %7.3f %9.3f %7.3f %8d"
          % (nm, v.max() - v.min(), d.max(), np.percentile(d, 95),
             int((d > thr[nm]).sum())))

# 像素换算：实测 yaw 7° ≈ 14.7px
print("\nyaw 最大帧间跳 ≈ %.2f px 位移（按 1°≈2.1px）"
      % (np.abs(np.diff(arr[:, 0])).max() * 2.1))
print("pitch 最大帧间跳 ≈ %.2f px" % (np.abs(np.diff(arr[:, 1])).max() * 2.1))

# 眨眼帧
bl = arr[:, 6]
idx = np.where(bl > 0.02)[0]
print("\n眨眼帧 %d 个: %s" % (len(idx), idx.tolist()))

# 实测：眨眼帧 vs 非眨眼帧，眼外区域是否被改动
import cv2
img = cv2.imread(IMG)
s = min(1.0, 1024 / float(max(img.shape[:2])))
img = cv2.resize(img, (int(img.shape[1] * s) // 2 * 2,
                       int(img.shape[0] * s) // 2 * 2))
if img.shape[1] != W or img.shape[0] != H:
    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)

for amt, tag in [(1.0, "闭眼"), (0.5, "半闭")]:
    out = A25._blink(img, prep, amt)
    d = np.abs(out.astype(np.int16) - img.astype(np.int16)).max(axis=2)
    ys, xs = np.where(d > 6)
    print("\n[%s a=%.1f] 改动像素 %d 个 (占全图 %.2f%%)"
          % (tag, amt, len(ys), len(ys) / float(W * H) * 100))
    if len(ys):
        print("  改动范围 x[%d,%d] y[%d,%d]" % (xs.min(), xs.max(), ys.min(), ys.max()))
        # 眼外区域（离眼中心 > 1.5*ew 或 > 1.5*eh）是否被碰
        bad = 0
        for i, (ex, ey) in enumerate(prep["eyes"]):
            m = ((np.abs(xs - ex) > ew * 1.3) | (np.abs(ys - ey) > eh * 1.3))
            bad += int(m.sum())
        print("  眼框外被改动像素: %d" % bad)
        print("  改动幅度 max=%d mean=%.1f" % (d.max(), d[ys, xs].mean()))
