"""测：warp 对脖子/衣领的破坏程度，以及不同 wgt 收窄方案的效果"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2
from backend import avatar2 as A2
from backend import avatar25d as A25

A2.MAX_SIDE = 1024
img_path = A2._img_path()
prep = A25._prep3d(img_path)
img = cv2.imread(img_path)
s = 1024 / float(max(img.shape[:2]))
img = cv2.resize(img, (int(img.shape[1] * s) // 2 * 2, int(img.shape[0] * s) // 2 * 2),
                 interpolation=cv2.INTER_AREA)
H, W = img.shape[:2]
print("W,H:", W, H)
print("face cx,cy: %.1f %.1f  fw,fh: %.1f %.1f" % (prep["cx"], prep["cy"], prep["fw"], prep["fh"]))
mk = prep["mask"]
ys, xs = np.where(mk > 0)
print("mask bbox: x %d~%d  y %d~%d" % (xs.min(), xs.max(), ys.min(), ys.max()))

def zones(img):
    H, W = img.shape[:2]
    return {
        "脸":    (int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())),
        "下巴下": (int(ys.max()), min(H, int(ys.max()) + 80), int(xs.min()), int(xs.max())),
        "脖子":  (int(ys.max()) + 40, min(H, int(ys.max()) + 200), W // 2 - 150, W // 2 + 150),
        "衣领":  (int(ys.max()) + 150, min(H, int(ys.max()) + 350), W // 2 - 250, W // 2 + 250),
        "背景":  (40, 200, 40, 200),
    }

kw = dict(yaw=5, pitch=-4, roll=3, tx=10, ty=-7, scl=1.012)
base = A25._warp(img, prep, **kw)
print("\n=== 默认 wgt（高斯 sigma=34）===")
for name, (y0, y1, x0, x1) in zones(img).items():
    if y1 <= y0 or x1 <= x0: continue
    d = np.abs(base[y0:y1, x0:x1].astype(np.int16) - img[y0:y1, x0:x1].astype(np.int16)).mean()
    print("  %-6s y%4d~%4d  meanDiff=%.2f" % (name, y0, y1, d))

# 尝试收窄 wgt
print("\n=== 收窄 wgt 各档 ===")
orig_wgt = prep["wgt"]
for sig in (26.0, 20.0, 14.0, 9.0):
    w = cv2.GaussianBlur((mk > 0).astype(np.float32), (0, 0), sig)
    w = np.clip(w / max(float(w.max()), 1e-6), 0.0, 1.0)
    p2 = dict(prep); p2["wgt"] = w
    out = A25._warp(img, p2, **kw)
    d_face = np.abs(out[int(ys.min()):int(ys.max()), int(xs.min()):int(xs.max())].astype(np.int16)
                    - img[int(ys.min()):int(ys.max()), int(xs.min()):int(xs.max())].astype(np.int16)).mean()
    y0 = int(ys.max()) + 40; y1 = min(H, y0 + 160)
    d_neck = np.abs(out[y0:y1, W // 2 - 150:W // 2 + 150].astype(np.int16)
                    - img[y0:y1, W // 2 - 150:W // 2 + 150].astype(np.int16)).mean()
    y2 = int(ys.max()) + 150; y3 = min(H, y2 + 200)
    d_col = np.abs(out[y2:y3, W // 2 - 250:W // 2 + 250].astype(np.int16)
                   - img[y2:y3, W // 2 - 250:W // 2 + 250].astype(np.int16)).mean()
    print("  sigma=%4.1f  脸=%5.2f  脖子=%5.2f  衣领=%5.2f" % (sig, d_face, d_neck, d_col))
    cv2.imwrite("_t_out/wgt_%d.png" % int(sig), out)

# pitch 加强
print("\n=== pitch 幅度 ===")
for p in (4, 6, 8, 10):
    out = A25._warp(img, prep, pitch=-p)
    d = np.abs(out.astype(np.int16) - img.astype(np.int16)).mean()
    print("  pitch=%2d  meanDiff=%.2f" % (p, d))
    cv2.imwrite("_t_out/pitch_%d.png" % p, out)
