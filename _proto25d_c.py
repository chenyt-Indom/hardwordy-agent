"""伪3D 原型 3：头姿位移场（视差 + 透视压缩 + 侧倾），输出对比图。"""
import os, sys
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from PIL import Image

img = np.asarray(Image.open(os.path.join(ROOT, "data", "avatar2", "avatar.png")).convert("RGB"))
H, W = img.shape[:2]
dm = np.load("_p25d_depthmap.npy")     # (512,512) 深度
mk = np.load("_p25d_maskmap.npy")      # (512,512) mask

# 深度图放大到原图分辨率
depth = cv2.resize(dm, (W, H), interpolation=cv2.INTER_LINEAR)
mask = cv2.resize(mk, (W, H), interpolation=cv2.INTER_NEAREST)

# 人脸区域统计
ys, xs = np.where(mask > 0)
cx, cy = float(xs.mean()), float(ys.mean())
face_w = float(xs.max() - xs.min())
face_h = float(ys.max() - ys.min())
z_in = depth[mask > 0]
z0 = float(np.median(z_in))
z_span = float(np.percentile(z_in, 98) - np.percentile(z_in, 2))
print("脸中心 (%.0f,%.0f) 宽 %.0f 高 %.0f  深度中位 %.1f 跨度 %.1f"
      % (cx, cy, face_w, face_h, z0, z_span))

# 深度(模型单位) -> 像素 的换算。
# BFM 正视图 UV 的 x 跨度实测 0.699，×120 = 83.9 个模型单位，正好铺满 face_w 像素，
# 所以 k_px = face_w / 83.9。位移(px) = Δz * k_px * sin(角度)。
# ⚠ 第一版写成 face_w*0.5/z_span（≈4.6），漏了 k_px 这一步，位移不到 1px，
#   导致 gain 从 0.38 调到 0.85 像素差纹丝不动（3.76 → 3.76）。
GAIN = float(sys.argv[1]) if len(sys.argv) > 1 else 0.38
K_PX = face_w / 83.9
SCALE = z_span * K_PX * GAIN
print("K_PX=%.2f SCALE=%.1f  最大位移≈%.1f px"
      % (K_PX, SCALE, SCALE * np.sin(np.deg2rad(14))))
zn = np.clip((depth - z0) / max(z_span, 1e-6), -1.0, 1.0)

# 位移权重：脸内为 1，向外 30px 渐变到 0（避免脖子/肩膀撕裂）
wgt = cv2.GaussianBlur((mask > 0).astype(np.float32), (0, 0), 30.0)
wgt = np.clip(wgt / max(wgt.max(), 1e-6), 0, 1)

yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)


def warp(yaw=0.0, pitch=0.0, roll=0.0):
    """反向映射：对每个目标像素求源像素。"""
    ry, rx, rz = np.deg2rad(yaw), np.deg2rad(pitch), np.deg2rad(roll)
    cyw, syw = np.cos(ry), np.sin(ry)
    cxp, sxp = np.cos(rx), np.sin(rx)

    u, v = xx.copy(), yy.copy()

    # 1) 撤销 roll：目标坐标绕脸中心反向转回去
    if abs(rz) > 1e-6:
        cr, sr = np.cos(-rz), np.sin(-rz)
        du, dv = u - cx, v - cy
        u, v = cx + du * cr - dv * sr, cy + du * sr + dv * cr

    # 2) 迭代求源坐标：透视压缩 + 深度视差
    su, sv = u.copy(), v.copy()
    for _ in range(2):
        zn_s = cv2.remap(zn, su.astype(np.float32), sv.astype(np.float32),
                         cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        w_s = cv2.remap(wgt, su.astype(np.float32), sv.astype(np.float32),
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        su = cx + (u - cx) / cyw - SCALE * zn_s * syw * w_s / cyw
        sv = cy + (v - cy) / cxp - SCALE * zn_s * sxp * w_s / cxp

    out = cv2.remap(img, su.astype(np.float32), sv.astype(np.float32),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out


cases = [("yaw-14", warp(yaw=-14)), ("yaw+14", warp(yaw=14)),
         ("pitch-10", warp(pitch=-10)), ("pitch+10", warp(pitch=10)),
         ("roll+8", warp(roll=8)),
         ("all", warp(yaw=12, pitch=-8, roll=6))]
for name, im in cases:
    cv2.imwrite("_p25d_%s.png" % name, cv2.cvtColor(im, cv2.COLOR_RGB2BGR))
    diff = np.abs(im.astype(np.int16) - img.astype(np.int16)).mean()
    print("%-8s 平均像素差 %.2f" % (name, diff))

# 拼一张：原图 + 5 个姿态，缩到每格 300
tiles = [img] + [c[1] for c in cases]
names = ["original"] + [c[0] for c in cases]
tiles = [cv2.resize(t, (300, 300), interpolation=cv2.INTER_AREA) for t in tiles]
strip = np.hstack(tiles)
out = "_p25d_strip_%s.png" % str(GAIN).replace(".", "")
cv2.imwrite(out, cv2.cvtColor(strip, cv2.COLOR_RGB2BGR))
print("已存", out, " GAIN =", GAIN)
