# -*- coding: utf-8 -*-
"""生成真实视频 → 抽帧看动态 → 量化帧间跳变（抽搐检测）。"""
import os, sys, tempfile
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2

BASE = r"D:\local-multimodal-src"
IMG = os.path.join(BASE, "data", "avatar2", "avatar.png")
WAV = os.path.join(tempfile.gettempdir(), "_diag_blink.wav")
OUT = os.path.join(BASE, "_diag_out")
os.makedirs(OUT, exist_ok=True)
MP4 = os.path.join(OUT, "blink_test.mp4")

from backend import avatar_lipsync as AL

r = AL.render(IMG, WAV, MP4, blink=True, head=True, max_side=720, motion=1.0)
print("render:", {k: r.get(k) for k in
                  ("ok", "frames", "sec", "motion", "motion_mode", "error")
                  if k in r})
if not r.get("ok"):
    sys.exit(1)

cap = cv2.VideoCapture(MP4)
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(f)
cap.release()
print("解码帧数:", len(frames), "尺寸:", frames[0].shape[:2])

n = len(frames)
# 帧间差：抽搐检测（相邻帧平均绝对差）
diffs = []
for i in range(1, n):
    d = np.abs(frames[i].astype(np.int16) - frames[i - 1].astype(np.int16))
    diffs.append(float(d.mean()))
diffs = np.array(diffs)
print("\n=== 帧间差（抽搐检测）===")
print("均值 %.3f  中位 %.3f  p95 %.3f  max %.3f" %
      (diffs.mean(), np.median(diffs), np.percentile(diffs, 95), diffs.max()))
spikes = np.where(diffs > np.median(diffs) + 6 * max(diffs.std(), 1e-6))[0] + 1
print("疑似抽搐帧（>中位+6σ）:", spikes.tolist()[:20], "共", len(spikes))
top = np.argsort(diffs)[-8:][::-1]
print("帧间差最大的 8 帧:", [(int(i + 1), round(float(diffs[i]), 2)) for i in top])

# 抽帧：开头 / 眨眼前后 / 中段
poses = AL._apply_motion.__doc__ is not None  # 占位，避免误用
pick = [0, 20, 33, 36, 40, 100, 126, 130, 200]
pick = [p for p in pick if p < n]
tiles = []
for i in pick:
    f = frames[i].copy()
    # 裁上半身区域（脸 + 眼）方便看细节
    h, w = f.shape[:2]
    crop = f[int(h * 0.18):int(h * 0.62), int(w * 0.18):int(w * 0.82)]
    crop = cv2.resize(crop, (360, int(360 * crop.shape[0] / crop.shape[1])))
    cv2.putText(crop, "#%d" % i, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (0, 0, 255), 2)
    tiles.append(crop)
rows = []
for i in range(0, len(tiles), 3):
    row = tiles[i:i + 3]
    while len(row) < 3:
        row.append(np.zeros_like(tiles[0]))
    rows.append(np.hstack(row))
sheet = np.vstack(rows)
p = os.path.join(OUT, "blink_video_frames.png")
cv2.imwrite(p, sheet)
print("\n抽帧拼图:", p, sheet.shape, "帧:", pick)
