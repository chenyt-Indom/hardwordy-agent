"""原始分辨率整脸对比：闭嘴 vs 张嘴 vs 张嘴+头姿。"""
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
p3 = A25._prep3d(img)
W, H = p2["wh"]

cases = []
for name, op, pose in [
    ("闭嘴", 0.0, (0, 0, 0, 0, 0, 1.0)),
    ("半张", 0.55, (0, 0, 0, 0, 0, 1.0)),
    ("全张", 1.0, (0, 0, 0, 0, 0, 1.0)),
    ("全张+转头", 1.0, (4.0, -2.0, 1.5, 5.0, 2.0, 1.006)),
    ("闭嘴+转头", 0.0, (-4.0, 2.0, -1.5, -5.0, -2.0, 0.994)),
]:
    fr = A25._mouth_frame(p2, op, 1.0, 1.2)
    fr = np.asarray(fr.convert("RGB"))
    y, pt, r, tx, ty, sc = pose
    fr = A25._warp(fr, p3, y, pt, r, tx, ty, sc)
    cases.append((name, fr))
    print("%-10s 平均亮度 %.1f" % (name, fr.mean()))

sep = np.full((H, 6, 3), 255, np.uint8)
strip = cases[0][1]
for _, f in cases[1:]:
    strip = np.hstack([strip, sep, f])
strip = cv2.resize(strip, (strip.shape[1] // 2, strip.shape[0] // 2),
                   interpolation=cv2.INTER_AREA)
cv2.imwrite("_p25d_face.png", cv2.cvtColor(strip, cv2.COLOR_RGB2BGR))
print("已存 _p25d_face.png（闭嘴|半张|全张|全张+转头|闭嘴+转头，各360px）")
