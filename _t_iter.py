"""测：_warp 的迭代次数对速度与质量的影响（决定能否提速）"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2
from backend import avatar2 as A2
from backend import avatar25d as A25
import math

A2.MAX_SIDE = 1024
img_path = A2._img_path()
prep = A25._prep3d(img_path, max_side=1024)
img = cv2.imread(img_path)
s = 1024 / float(max(img.shape[:2]))
img = cv2.resize(img, (int(img.shape[1]*s)//2*2, int(img.shape[0]*s)//2*2),
                 interpolation=cv2.INTER_AREA)

# 复制一份 _warp，允许指定迭代次数
def warp_n(img, prep, iters, yaw=0.0, pitch=0.0, roll=0.0, tx=0.0, ty=0.0, scl=1.0):
    W, H = prep["wh"]; cx, cy = prep["cx"], prep["cy"]; scale = prep["scale"]
    x0, y0, x1, y1 = prep["roi"]
    ry, rx, rz = math.radians(yaw), math.radians(pitch), math.radians(roll)
    cyw, syw = max(math.cos(ry), 0.55), math.sin(ry)
    cxp, sxp = max(math.cos(rx), 0.55), math.sin(rx)
    u, v = np.meshgrid(np.arange(x0, x1, dtype=np.float32),
                       np.arange(y0, y1, dtype=np.float32))
    _s = max(0.90, min(1.10, float(scl)))
    u = cx + (u - tx - cx) / _s
    v = cy + (v - ty - cy) / _s
    if abs(rz) > 1e-6:
        cr, sr = math.cos(-rz), math.sin(-rz)
        du, dv = u - cx, v - cy
        u, v = cx + du*cr - dv*sr, cy + du*sr + dv*cr
    zn, wg = prep["zn"], prep["wgt"]
    su, sv = u.copy(), v.copy()
    for _ in range(iters):
        zs = cv2.remap(zn, su, sv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        ws = cv2.remap(wg, su, sv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        su = cx + (u - cx)/cyw - scale*zs*syw*ws/cyw
        sv = cy + (v - cy)/cxp - scale*zs*sxp*ws/cxp
    sub = img[y0:y1, x0:x1]
    out = img.copy()
    out[y0:y1, x0:x1] = cv2.remap(sub, su - x0, sv - y0, cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
    return out

kw = dict(yaw=6, pitch=-5, roll=3, tx=11, ty=-8, scl=1.012)
ref = None
for it in (1, 2, 3):
    t0 = time.time()
    for _ in range(10):
        o = warp_n(img, prep, it, **kw)
    dt = (time.time() - t0) / 10 * 1000
    if ref is None:
        ref = o.astype(np.float32)
    d = np.abs(o.astype(np.float32) - ref).mean()
    print("iters=%d  %.1fms  与2次参考差=%.4f" % (it, dt, d))
    cv2.imwrite("_t_out/it_%d.png" % it, o)

# 官方 _warp 作参考
t0 = time.time()
for _ in range(10):
    o2 = A25._warp(img, prep, **kw)
print("官方 _warp  %.1fms" % ((time.time()-t0)/10*1000))
print("官方 vs iters=2 差:", np.abs(o2.astype(np.float32) - ref).mean())
