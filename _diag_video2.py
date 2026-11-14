# -*- coding: utf-8 -*-
"""抽出眨眼帧并放大眼部，判断实际眨眼效果。"""
import os, sys, tempfile
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2

BASE = r"D:\local-multimodal-src"
OUT = os.path.join(BASE, "_diag_out")
MP4 = os.path.join(OUT, "blink_test.mp4")

from backend import avatar25d as A25
from backend import avatar_lipsync as AL
from backend import avatar2 as A2

prep = A25._prep3d(os.path.join(BASE, "data", "avatar2", "avatar.png"),
                   max_side=720)
H, W = prep["wh"]
eyes = prep["eyes"]
gap = prep["eye_gap"]
ew = gap * 0.17
eh = gap * 0.085
x0 = int(min(e[0] for e in eyes) - ew * 2.6)
x1 = int(max(e[0] for e in eyes) + ew * 2.6)
y0 = int(min(e[1] for e in eyes) - eh * 4.5)
y1 = int(max(e[1] for e in eyes) + eh * 3.0)
print("眼部区 x[%d,%d] y[%d,%d] (%dx%d) 图尺寸 %dx%d"
      % (x0, x1, y0, y1, x1 - x0, y1 - y0, W, H))

# 找出眨眼帧（用同一套曲线）
sr, y = A2._read_wav(os.path.join(tempfile.gettempdir(), "_diag_blink.wav"))
fps = 25
n = int(len(y) / float(sr) * fps)
e = AL._energy_curve(y, sr, n, fps)
poses = A25._pose_curve(n, fps, e)
bl = np.array([p[6] for p in poses])
idx = np.where(bl > 0.05)[0]
print("眨眼相关帧:", idx.tolist())

cap = cv2.VideoCapture(MP4)
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(f)
cap.release()
print("视频帧数", len(frames))

# 取第一次眨眼的连续 7 帧 + 一次睁眼帧做对照
groups = [("眨眼1", list(range(max(0, idx[0] - 1), min(len(frames), idx[0] + 8))))]
if len(idx) > 10:
    groups.append(("眨眼2", list(range(max(0, idx[-1] - 1), min(len(frames), idx[-1] + 8)))))

for name, ids in groups:
    tiles = []
    for i in ids:
        if i >= len(frames):
            continue
        f = frames[i]
        crop = f[y0:y1, x0:x1]
        z = 3
        crop = cv2.resize(crop, (crop.shape[1] * z, crop.shape[0] * z),
                          interpolation=cv2.INTER_NEAREST)
        cv2.putText(crop, "#%d b=%.2f" % (i, bl[i] if i < len(bl) else 0),
                    (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
        tiles.append(crop)
    if not tiles:
        continue
    w = tiles[0].shape[1]
    sheet = np.hstack(tiles)
    p = os.path.join(OUT, "blink_%s.png" % name)
    cv2.imwrite(p, sheet)
    print("已写", p, sheet.shape)
