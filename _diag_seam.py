# -*- coding: utf-8 -*-
"""诊断「割裂」：ROI 硬切 / 椭圆补丁 / 背景位移"""
import sys, os, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

from backend import avatar25d as A25
from backend import wav2lip_engine as W2L

# 找形象图
cands = glob.glob("data/avatar2/*.png") + glob.glob("data/avatar2/*.jpg")
cands = [c for c in cands if os.path.getsize(c) > 20000]
print("形象图候选:", cands[:5])
img_path = cands[0]
img = cv2.imread(img_path)
H, W = img.shape[:2]
print("形象图 %s  %dx%d" % (os.path.basename(img_path), W, H))

prep = A25._prep3d(img_path, max_side=1024)
print("ROI:", prep["roi"], " 画布:", prep["wh"])
print("scale=%.3f  zspan=%.3f  fw=%.1f fh=%.1f" % (
    prep["scale"], prep["zspan"], prep["fw"], prep["fh"]))

x0, y0, x1, y1 = prep["roi"]
wgt = prep["wgt"]
print("\n=== A. 位移权重在 ROI 边界处的值 ===")
print("  上边界 y=%d: mean=%.3f max=%.3f" % (y0, wgt[y0, x0:x1].mean(), wgt[y0, x0:x1].max()))
print("  下边界 y=%d: mean=%.3f max=%.3f" % (y1-1, wgt[y1-1, x0:x1].mean(), wgt[y1-1, x0:x1].max()))
print("  左边界 x=%d: mean=%.3f max=%.3f" % (x0, wgt[y0:y1, x0].mean(), wgt[y0:y1, x0].max()))
print("  右边界 x=%d: mean=%.3f max=%.3f" % (x1-1, wgt[y0:y1, x1-1].mean(), wgt[y0:y1, x1-1].max()))

# 背景区（权重≈0）的位移：这是「ROI 内被拖、ROI 外不动」的根源
print("\n=== B. 权重=0 区域是否仍被移动 ===")
for yaw in (0.0, 4.0, 7.5):
    out = A25._warp(img, prep, yaw=yaw, iters=1)
    if yaw == 0:
        continue
    diff = np.abs(out.astype(np.int16) - img.astype(np.int16)).max(axis=2)
    # 只统计权重≈0 的像素
    bgm = wgt < 0.02
    # 只统计 ROI 内部
    inside = np.zeros((H, W), bool); inside[y0:y1, x0:x1] = True
    bg_in_roi = bgm & inside
    bg_out_roi = bgm & (~inside)
    print("  yaw=%.1f°  权重<0.02 且 ROI 内: 被改动像素 %.1f%%  最大改动 %d" % (
        yaw, 100.0 * (diff[bg_in_roi] > 0).mean() if bg_in_roi.any() else 0,
        diff[bg_in_roi].max() if bg_in_roi.any() else 0))
    print("              权重<0.02 且 ROI 外: 被改动像素 %.1f%%  最大改动 %d" % (
        100.0 * (diff[bg_out_roi] > 0).mean() if bg_out_roi.any() else 0,
        diff[bg_out_roi].max() if bg_out_roi.any() else 0))
    # ROI 边界处跨线的梯度突变
    if y1 - 1 < H - 1:
        a = img[y1-2, x0:x1].astype(np.float32)
        b = out[y1, x0:x1].astype(np.float32)
        print("              下边界跨线色差(原图 vs 输出): mean=%.2f max=%.1f" % (
            np.abs(a-b).mean(), np.abs(a-b).max()))

# C. 椭圆补丁：清晰度断层
print("\n=== C. 合成椭圆内外的清晰度/色差 ===")
eng = W2L._ENGINE if hasattr(W2L, "_ENGINE") else None
try:
    eng = W2L.Engine()
except Exception as e:
    print("  引擎加载失败:", e)
    eng = None

if eng is not None:
    dets, _ = eng.det.detect(img, thresh=0.5)
    d = max(dets, key=lambda t: (t[2]-t[0])*(t[3]-t[1]))
    bx = [int(v) for v in d[:4]]
    bx = [max(0, bx[0]), max(0, bx[1]), min(W, bx[2]), min(H, bx[3])]
    print("  人脸框:", bx)
    bw, bh = bx[2]-bx[0], bx[3]-bx[1]
    ax, ay = int(bw*0.31), int(bh*0.22)
    cy = int(bh*0.76)
    print("  椭圆 ax=%d ay=%d cy=%d（占脸框 %.0f%%宽 %.0f%%高）" % (ax, ay, cy, 62, 44))
    # 椭圆面积占整张脸的比例
    print("  椭圆面积 ≈ %.1f%% 脸框面积" % (100.0*np.pi*ax*ay/(bw*bh)))
