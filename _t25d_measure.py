"""客观测量：唇分离距离、鼻子是否被动、各区域像素差。"""
import os, sys
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from backend import avatar2 as A2
from backend import avatar25d as A25

img = A2._img_path()
p2 = A2._prep(img)
W, H = p2["wh"]
cx, cy, mw, mh = p2["geom"]

f0 = np.asarray(A25._mouth_frame(p2, 0.0, 1.0, 1.2).convert("RGB"))
f1 = np.asarray(A25._mouth_frame(p2, 1.0, 1.0, 1.2).convert("RGB"))

g0 = cv2.cvtColor(f0, cv2.COLOR_RGB2GRAY).astype(np.float32)
g1 = cv2.cvtColor(f1, cv2.COLOR_RGB2GRAY).astype(np.float32)


def profile(g, x, y0, y1):
    return g[y0:y1, int(x - mw * 0.10):int(x + mw * 0.10)].mean(axis=1)


y0, y1 = int(cy - mh * 2.2), int(cy + mh * 2.2)
pr0 = profile(g0, cx, y0, y1)
pr1 = profile(g1, cx, y0, y1)
print("剖面长度", len(pr0), " y0=%d" % y0)


def lip_edges(pr):
    """找唇缝：最暗点 + 左右各扩展到达皮肤亮度一半的位置。"""
    i = int(np.argmin(pr))
    lo, hi = pr.max(), pr.min()
    thr = lo + (hi - lo) * 0.55
    a = i
    while a > 0 and pr[a] < thr:
        a -= 1
    b = i
    while b < len(pr) - 1 and pr[b] < thr:
        b += 1
    return a, b, b - a, i


a0, b0, w0, i0 = lip_edges(pr0)
a1, b1, w1, i1 = lip_edges(pr1)
print("闭嘴: 暗带 %d~%d 宽 %d 最暗行 %d (亮度 %.0f)" % (a0, b0, w0, i0, pr0.min()))
print("全张: 暗带 %d~%d 宽 %d 最暗行 %d (亮度 %.0f)" % (a1, b1, w1, i1, pr1.min()))
print("→ 唇缝加宽 %d px" % (w1 - w0))

# 分区像素差
regions = {
    "鼻子":  (int(cx - mw * 0.45), int(cy - mh * 3.0), int(cx + mw * 0.45), int(cy - mh * 1.5)),
    "人中":  (int(cx - mw * 0.20), int(cy - mh * 1.5), int(cx + mw * 0.20), int(cy - mh * 0.8)),
    "唇部":  (int(cx - mw * 0.45), int(cy - mh * 0.8), int(cx + mw * 0.45), int(cy + mh * 0.8)),
    "下巴":  (int(cx - mw * 0.40), int(cy + mh * 1.5), int(cx + mw * 0.40), int(cy + mh * 3.0)),
    "两颊":  (int(cx + mw * 0.55), int(cy - mh), int(cx + mw * 0.95), int(cy + mh)),
}
for name, (x0, yy0, x1, yy1) in regions.items():
    x0 = max(0, x0); x1 = min(W, x1); yy0 = max(0, yy0); yy1 = min(H, yy1)
    d = np.abs(f0[yy0:yy1, x0:x1].astype(np.int16) - f1[yy0:yy1, x0:x1].astype(np.int16)).mean()
    print("%-4s 区域 (%d,%d)-(%d,%d)  平均差 %.2f" % (name, x0, yy0, x1, yy1, d))

# 大图：只放大嘴部 3 倍
crop = lambda f: f[max(0, int(cy - mh * 1.8)):int(cy + mh * 1.8),
                   int(cx - mw * 0.7):int(cx + mw * 0.7)]
a, b = crop(f0), crop(f1)
sep = np.full((a.shape[0], 12, 3), 255, np.uint8)
big = np.hstack([a, sep, b])
big = cv2.resize(big, (big.shape[1] * 3, big.shape[0] * 3), interpolation=cv2.INTER_CUBIC)
cv2.imwrite("_p25d_mouth2.png", cv2.cvtColor(big, cv2.COLOR_RGB2BGR))
print("已存 _p25d_mouth2.png（左闭嘴 | 右全张，3倍放大）")
