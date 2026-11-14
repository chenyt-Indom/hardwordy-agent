# -*- coding: utf-8 -*-
"""分析已生成的帧：三区色差 / 清晰度断层 / ROI 背景位移"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar_lipsync as AL

fr = np.load("_qa_out/frames.npy")
print("帧数 %d  尺寸 %s" % (len(fr), fr.shape[1:3]), flush=True)

img0 = cv2.imread("data/avatar2/avatar.png")
H, W = img0.shape[:2]

from backend.face_detect_scrfd import SCRFD
det = SCRFD(AL._paths()["det"])
dets, _ = det.detect(img0, thresh=0.5)
d = max(dets, key=lambda t: (t[2]-t[0])*(t[3]-t[1]))
bx = [max(0, int(d[0])), max(0, int(d[1])), min(W, int(d[2])), min(H, int(d[3]))]
bw, bh = bx[2]-bx[0], bx[3]-bx[1]
cx = bx[0] + int(bw*0.50); cy = bx[1] + int(bh*0.76)
ax = int(bw*0.31); ay = int(bh*0.22)
print("脸框 %s  椭圆中心(%d,%d) 半径(%d,%d)" % (bx, cx, cy, ax, ay), flush=True)

m_mouth = np.zeros((H, W), np.uint8)
cv2.ellipse(m_mouth, (cx, cy), (int(ax*0.60), int(ay*0.42)), 0, 0, 360, 1, -1)
m_ell = np.zeros((H, W), np.uint8)
cv2.ellipse(m_ell, (cx, cy), (ax, ay), 0, 0, 360, 1, -1)
m_rest = (m_ell > 0) & (m_mouth == 0)
m_out = np.zeros((H, W), np.uint8)
cv2.ellipse(m_out, (cx, cy), (int(ax*1.9), int(ay*1.9)), 0, 0, 360, 1, -1)
m_out = (m_out > 0) & (m_ell == 0)

def st(p, m):
    g = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    return float(lap[m].var()), float(np.abs(p[m].astype(np.float32) - img0[m].astype(np.float32)).mean())

print("\n=== 逐帧 ===", flush=True)
rows = []
for i in range(0, len(fr), max(1, len(fr)//8)):
    p = fr[i]
    if p.shape[:2] != (H, W):
        p = cv2.resize(p, (W, H), interpolation=cv2.INTER_AREA)
    sm, dm = st(p, m_mouth)
    sr, dr = st(p, m_rest)
    so, do = st(p, m_out)
    rows.append((dm, dr, do, sr, so))
    print("  f%02d  色差 嘴%.1f 椭内%.1f 椭外%.1f | 锐度 内%.0f 外%.0f" % (i, dm, dr, do, sr, so), flush=True)

print("\n=== 汇总 ===", flush=True)
print("  嘴核心色差   %.2f  （该变）" % np.mean([r[0] for r in rows]))
print("  椭圆内非嘴   %.2f  （补丁感指标，越小越好）" % np.mean([r[1] for r in rows]))
print("  椭圆外脸区   %.2f" % np.mean([r[2] for r in rows]))
print("  清晰度比 内/外 = %.2f （1.0 最好）" % (np.mean([r[3] for r in rows])/max(1e-6, np.mean([r[4] for r in rows]))))

# ROI 背景位移
from backend import avatar25d as A25
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
x0, y0, x1, y1 = prep["roi"]
print("\nROI %s" % (prep["roi"],))
for yaw in (4.0, 7.5):
    o = A25._warp(img0, prep, yaw=yaw, iters=1)
    diff = np.abs(o.astype(np.int16) - img0.astype(np.int16)).max(axis=2)
    wg = prep["wgt"]
    ins = np.zeros((H, W), bool); ins[y0:y1, x0:x1] = True
    bg = (wg < 0.02) & ins
    print("  yaw=%.1f° 背景(权重<0.02,ROI内) 改动 %.1f%% 最大 %d" % (
        yaw, 100.0*(diff[bg] > 0).mean() if bg.any() else 0, diff[bg].max() if bg.any() else 0))
