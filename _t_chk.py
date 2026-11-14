"""优化后画质检查：张嘴帧 + 头姿变化帧"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar2 as A2

root = A2._root()
mp4 = os.path.join(root, "_t_out", "opt_1.0.mp4")
cap = cv2.VideoCapture(mp4)
frames = []
while True:
    ok, f = cap.read()
    if not ok: break
    frames.append(f)
cap.release()
print("frames:", len(frames))

mx1, mx2, my1, my2 = 420, 610, 570, 650
sc = []
for f in frames:
    g = cv2.cvtColor(f[my1:my2, mx1:mx2], cv2.COLOR_BGR2GRAY)
    sc.append(float((g < 70).mean()))
sc = np.array(sc)
print("嘴部暗像素占比 min=%.4f med=%.4f max=%.4f" % (sc.min(), np.median(sc), sc.max()))

# 嘴部放大：最大张 / 中位 / 最小
order = np.argsort(sc)
picks = [int(order[-1]), int(order[len(order)//2]), int(order[0])]
row = []
for i in picks:
    crop = frames[i][my1-35:my2+35, mx1-45:mx2+45]
    crop = cv2.resize(crop, (crop.shape[1]*2, crop.shape[0]*2), interpolation=cv2.INTER_CUBIC)
    cv2.putText(crop, "#%d" % i, (6, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    row.append(crop)
cv2.imwrite(os.path.join(root, "_t_out", "opt_mouth.png"), np.hstack(row))
print("嘴部图:", picks)

# 整帧序列（看头姿）
idx = [0, len(frames)//5, 2*len(frames)//5, 3*len(frames)//5, 4*len(frames)//5]
row2 = []
for i in idx:
    im = frames[i].copy()
    cv2.putText(im, "#%d" % i, (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
    row2.append(im)
g = np.hstack(row2)
g = cv2.resize(g, (g.shape[1]//3, g.shape[0]//3))
cv2.imwrite(os.path.join(root, "_t_out", "opt_seq.png"), g)
print("序列图:", g.shape, idx)
