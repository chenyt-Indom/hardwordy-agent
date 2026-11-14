"""找张嘴最大的帧，并核实 warp 有没有破坏嘴部"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2
from backend import avatar2 as A2

root = A2._root()
mp4 = os.path.join(root, "_t_out", "mot2_1.0.mp4")
cap = cv2.VideoCapture(mp4)
frames = []
while True:
    ok, f = cap.read()
    if not ok: break
    frames.append(f)
cap.release()
print("frames:", len(frames), "size:", frames[0].shape)

# 嘴区：形象图里唇缝在 y≈600/1024（之前校准过），宽约 168
mx1, mx2 = 420, 610
my1, my2 = 570, 650

scores = []
for f in frames:
    g = cv2.cvtColor(f[my1:my2, mx1:mx2], cv2.COLOR_BGR2GRAY)
    scores.append(float((g < 70).mean()))
scores = np.array(scores)
print("暗像素占比: min=%.4f  median=%.4f  max=%.4f" % (scores.min(), np.median(scores), scores.max()))

order = np.argsort(scores)
pick = [int(order[-1]), int(order[-len(order)//6]), int(order[len(order)//2]),
        int(order[len(order)//6]), int(order[0])]
row = []
for i in pick:
    im = frames[i].copy()
    cv2.rectangle(im, (mx1, my1), (mx2, my2), (0, 255, 0), 2)
    cv2.putText(im, "#%d %.2f%%" % (i, scores[i]*100), (10, 44),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
    row.append(im)
grid = np.hstack(row)
grid = cv2.resize(grid, (grid.shape[1]//3, grid.shape[0]//3))
cv2.imwrite(os.path.join(root, "_t_out", "mouth_seq.png"), grid)
print("嘴部序列:", grid.shape, "帧号:", pick)

# 同时输出嘴部区域放大对比
row2 = []
for i in pick:
    crop = frames[i][my1-30:my2+30, mx1-40:mx2+40]
    crop = cv2.resize(crop, (crop.shape[1]*2, crop.shape[0]*2), interpolation=cv2.INTER_CUBIC)
    cv2.putText(crop, "#%d" % i, (6, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    row2.append(crop)
g2 = np.hstack(row2)
cv2.imwrite(os.path.join(root, "_t_out", "mouth_zoom.png"), g2)
print("嘴部放大:", g2.shape)
