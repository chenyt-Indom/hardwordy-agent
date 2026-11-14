# -*- coding: utf-8 -*-
"""眨眼视觉对比：原图 / 半闭 / 全闭，并放大眼部区域。"""
import os, sys
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2

BASE = r"D:\local-multimodal-src"
IMG = os.path.join(BASE, "data", "avatar2", "avatar.png")
OUT = os.path.join(BASE, "_diag_out")
os.makedirs(OUT, exist_ok=True)

from backend import avatar25d as A25

prep = A25._prep3d(IMG, max_side=1024)
H, W = prep["wh"]
img = cv2.imread(IMG)
s = min(1.0, 1024 / float(max(img.shape[:2])))
img = cv2.resize(img, (int(img.shape[1] * s) // 2 * 2,
                       int(img.shape[0] * s) // 2 * 2))
if img.shape[1] != W or img.shape[0] != H:
    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)

gap = float(prep["eye_gap"]); ew = gap * 0.17; eh = gap * 0.085
# 眼部联合区域
x0 = int(min(e[0] for e in prep["eyes"]) - ew * 2)
x1 = int(max(e[0] for e in prep["eyes"]) + ew * 2)
y0 = int(min(e[1] for e in prep["eyes"]) - eh * 3)
y1 = int(max(e[1] for e in prep["eyes"]) + eh * 3)
print("眼部裁剪区 x[%d,%d] y[%d,%d]  (%dx%d)" % (x0, x1, y0, y1, x1 - x0, y1 - y0))

frames = []
for amt, tag in [(0.0, "原图"), (0.45, "半闭"), (0.85, "峰值")]:
    out = A25._blink(img, prep, amt) if amt > 0 else img
    d = np.abs(out.astype(np.int16) - img.astype(np.int16)).max(axis=2)
    ys, xs = np.where(d > 6)
    # 严格判定：改动必须落在两眼 bbox 并集内
    if len(ys):
        inside = np.zeros(len(ys), bool)
        for (ex, ey) in prep["eyes"]:
            inside |= ((np.abs(xs - ex) <= ew * 1.35) &
                       (np.abs(ys - ey) <= eh * 1.35))
        print("[%s] 改动 %d px, 其中框内 %d, 框外 %d, 高度 %d px, max幅度 %d"
              % (tag, len(ys), int(inside.sum()), int((~inside).sum()),
                 (ys.max() - ys.min() + 1) if len(ys) else 0, d.max()))
    crop = out[y0:y1, x0:x1]
    crop = cv2.resize(crop, ((x1 - x0) * 3, (y1 - y0) * 3),
                      interpolation=cv2.INTER_NEAREST)
    cv2.putText(crop, tag, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                (0, 0, 255), 2)
    frames.append(crop)

# 竖排拼图
w = min(f.shape[1] for f in frames)
frames = [cv2.resize(f, (w, int(f.shape[0] * w / f.shape[1]))) for f in frames]
sheet = np.vstack(frames)
p = os.path.join(OUT, "blink_compare.png")
cv2.imwrite(p, sheet)
print("已写:", p, sheet.shape)
