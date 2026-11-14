"""抽帧 + 客观测量：验证头姿/口型/眨眼是否真的在动，有没有伪影。"""
import os, sys, subprocess
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from backend import avatar2 as A2

exe = A2.ffmpeg()
src = os.path.join(ROOT, "_p25d_test.mp4")
print("视频:", os.path.isfile(src), os.path.getsize(src), "字节")

# 抽 8 帧拼 4x2
tile = os.path.join(ROOT, "_p25d_tiles.png")
cmd = [exe, "-y", "-loglevel", "error", "-i", src,
       "-vf", "select='not(mod(n\\,23))',scale=340:-1,tile=4x2",
       "-frames:v", "1", tile]
subprocess.run(cmd, check=False)
print("拼图:", os.path.isfile(tile))

# 逐帧测量：头部位移 / 嘴部变化 / 帧间抖动
cap = cv2.VideoCapture(src)
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
cap.release()
print("帧数", len(frames))

# 脸区（720 尺度：原 1024 的 305~714 → 214~502）
x0, x1, y0, y1 = 214, 502, 145, 491
crops = [f[y0:y1, x0:x1].astype(np.float32) for f in frames]

diffs = [np.abs(crops[i] - crops[i - 1]).mean() for i in range(1, len(crops))]
print("相邻帧差: 均值 %.2f  中位 %.2f  最大 %.2f" %
      (np.mean(diffs), np.median(diffs), np.max(diffs)))

# 头部位移：以第一帧为基准，找最佳平移量
base = crops[0]
shifts = []
for i in range(0, len(crops), max(1, len(crops) // 30)):
    c = crops[i]
    best, bd = (0, 0), 1e18
    for dy in range(-12, 13, 3):
        for dx in range(-12, 13, 3):
            m = c[max(0, dy):c.shape[0] + min(0, dy), max(0, dx):c.shape[1] + min(0, dx)]
            b = base[max(0, -dy):base.shape[0] + min(0, -dy), max(0, -dx):base.shape[1] + min(0, -dx)]
            if m.shape != b.shape or m.size == 0:
                continue
            d = np.abs(m - b).mean()
            if d < bd:
                bd, best = d, (dx, dy)
    shifts.append(best)
dxs = [s[0] for s in shifts]; dys = [s[1] for s in shifts]
print("头部位移范围: x %.0f~%.0f  y %.0f~%.0f" % (min(dxs), max(dxs), min(dys), max(dys)))
print("不同平移量的帧占比: %d/%d" % (len(set(shifts)), len(shifts)))

# 嘴部区域变化（720 尺度：嘴在 y≈0.515*720=371, 宽 0.255*720=184）
mx0, mx1 = int(0.50 * 720 - 92), int(0.50 * 720 + 92)
my0, my1 = int(0.515 * 720 - 22), int(0.515 * 720 + 22)
mdiffs = [np.abs(frames[i][my0:my1, mx0:mx1].astype(np.float32)
                 - frames[i - 1][my0:my1, mx0:mx1].astype(np.float32)).mean()
          for i in range(1, len(frames))]
print("嘴部帧间差: 均值 %.2f 最大 %.2f" % (np.mean(mdiffs), np.max(mdiffs)))
