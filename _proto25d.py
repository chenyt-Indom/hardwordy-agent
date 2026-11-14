"""伪3D 原型：验证 深度图 + 头姿位移场 是否可行。"""
import os, sys
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from PIL import Image
import backend.face3d as F

IMG = os.path.join(ROOT, "data", "avatar2", "avatar.png")
img = np.asarray(Image.open(IMG).convert("RGB"))
H, W = img.shape[:2]

f3 = F.get()
det = f3._detector(W, H)
_, faces = det.detect(img)
faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
lm = faces[0][4:14].reshape(5, 2).astype(np.float32)
M, _ = cv2.estimateAffinePartial2D(lm, F.TEMPLATE5, method=cv2.LMEDS)
Minv = cv2.invertAffineTransform(M)

r = f3.build(img)
uv = r["uv"]                  # (N,2) 120 归一化
z = r["pts_front"][2]         # 深度
tri = f3.tri
print("N verts", len(uv), "tri", tri.shape)
print("z range", float(z.min()), float(z.max()), "median", float(np.median(z)))
print("z pct", np.percentile(z, [1, 5, 50, 95, 99]).round(2).tolist())

# 120 归一化 -> 原图像素
p120 = (uv * 120.0).astype(np.float32)
ones = np.ones((len(p120), 1), np.float32)
P = np.hstack([p120, ones]) @ Minv.T      # (N,2) 原图像素
print("原图顶点范围 x", P[:, 0].min().round(1), P[:, 0].max().round(1),
      "y", P[:, 1].min().round(1), P[:, 1].max().round(1))
print("原图尺寸", W, H)
# 人脸在 UV 里的位置
print("uv x", uv[:, 0].min().round(3), uv[:, 0].max().round(3))
print("uv y", uv[:, 1].min().round(3), uv[:, 1].max().round(3))
np.save("_p25d_P.npy", P)
np.save("_p25d_z.npy", z)
