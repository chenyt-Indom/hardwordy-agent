"""伪3D 原型 2：深度图光栅化 + 头姿位移场，输出对比图。"""
import os, sys
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from PIL import Image

P = np.load("_p25d_P.npy")      # (N,2) 原图像素
z = np.load("_p25d_z.npy")      # (N,)
IMG = os.path.join(ROOT, "data", "avatar2", "avatar.png")
img = np.asarray(Image.open(IMG).convert("RGB"))
H, W = img.shape[:2]
import backend.face3d as F
tri = F.get().tri


def raster_depth(P, z, tri, W, H, DS=512):
    """把三角面光栅化到 DS 分辨率，输出深度图 + 覆盖 mask。"""
    k = DS / float(max(W, H))
    xy = (P * k).astype(np.float32)
    Tri = tri.T
    a, b, c = xy[Tri[:, 0]], xy[Tri[:, 1]], xy[Tri[:, 2]]
    area = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
            - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    keep = np.abs(area) > 1e-6
    Tri = Tri[keep]
    a, b, c = a[keep], b[keep], c[keep]

    lo = np.floor(np.minimum(np.minimum(a, b), c)).astype(np.int32)
    hi = np.ceil(np.maximum(np.maximum(a, b), c)).astype(np.int32) + 1
    lo = np.clip(lo, 0, DS)
    hi = np.clip(hi, 0, DS)
    bw = hi[:, 0] - lo[:, 0]
    bh = hi[:, 1] - lo[:, 1]
    ok = (bw >= 1) & (bh >= 1)
    Tri, a, b, c, lo, bw, bh = Tri[ok], a[ok], b[ok], c[ok], lo[ok], bw[ok], bh[ok]
    if len(Tri) == 0:
        return np.zeros((DS, DS), np.float32), np.zeros((DS, DS), np.uint8)

    counts = (bw * bh).astype(np.int64)
    total = int(counts.sum())
    tid = np.repeat(np.arange(len(Tri)), counts)
    start = np.repeat(np.cumsum(counts) - counts, counts)
    local = np.arange(total, dtype=np.int64) - start
    px = lo[tid, 0] + (local % bw[tid])
    py = lo[tid, 1] + (local // bw[tid])

    v0, v1, v2 = a[tid], b[tid], c[tid]
    den = ((v1[:, 1] - v2[:, 1]) * (v0[:, 0] - v2[:, 0])
           + (v2[:, 0] - v1[:, 0]) * (v0[:, 1] - v2[:, 1]))
    # 退化三角形（den≈0）会让重心坐标炸掉，插值出 -10 万这种荒谬深度。
    # 直接按面积筛掉：den 就是 2 倍有向面积，像素尺度下 < 0.02 的面积贡献不了任何可见像素。
    good = np.abs(den) > 0.02
    den = np.where(good, den, 1.0)
    fx = px.astype(np.float32) + 0.5
    fy = py.astype(np.float32) + 0.5
    w0 = ((v1[:, 1] - v2[:, 1]) * (fx - v2[:, 0]) + (v2[:, 0] - v1[:, 0]) * (fy - v2[:, 1])) / den
    w1 = ((v2[:, 1] - v0[:, 1]) * (fx - v2[:, 0]) + (v0[:, 0] - v2[:, 0]) * (fy - v2[:, 1])) / den
    w2 = 1.0 - w0 - w1
    inside = good & (w0 >= -0.02) & (w1 >= -0.02) & (w2 >= -0.02)
    if not inside.any():
        return np.zeros((DS, DS), np.float32), np.zeros((DS, DS), np.uint8)

    tid_i, px_i, py_i = tid[inside], px[inside], py[inside]
    b0, b1, b2 = w0[inside], w1[inside], w2[inside]
    zi = z[Tri[:, 0]][tid_i] * b0 + z[Tri[:, 1]][tid_i] * b1 + z[Tri[:, 2]][tid_i] * b2

    flat = py_i * DS + px_i
    order = np.lexsort((zi, flat))
    fs = flat[order]
    first = np.ones(len(fs), bool)
    first[1:] = fs[1:] != fs[:-1]
    sel = order[first]
    fi = flat[sel]
    depth = np.zeros((DS, DS), np.float32)
    mask = np.zeros((DS, DS), np.uint8)
    depth[fi // DS, fi % DS] = zi[sel]
    mask[fi // DS, fi % DS] = 255
    return depth, mask


d, mk = raster_depth(P, z, tri, W, H, DS=512)
cov = (mk > 0).mean()
print("深度图覆盖 %.1f%%  深度范围 %.1f ~ %.1f" % (cov * 100, d[mk > 0].min(), d[mk > 0].max()))
cv2.imwrite("_p25d_depth.png", np.clip((d - d[mk > 0].min()) /
            max(1e-6, d.max() - d[mk > 0].min()) * 255, 0, 255).astype(np.uint8))
cv2.imwrite("_p25d_mask.png", mk)

# 填补空洞（深度图在人脸内部的小洞，用邻域均值补）
dm = cv2.inpaint(np.where(mk > 0, d, 0).astype(np.float32), (mk == 0).astype(np.uint8), 3, cv2.INPAINT_NS)
dm = cv2.GaussianBlur(dm, (0, 0), 3.0)
np.save("_p25d_depthmap.npy", dm)
np.save("_p25d_maskmap.npy", mk)
print("已存 _p25d_depth.png / _p25d_depthmap.npy")
