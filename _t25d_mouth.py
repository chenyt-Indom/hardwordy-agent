"""核实口型：直接渲染 op=0 / 0.5 / 1.0 三帧，量唇缝宽度并出对比图。"""
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
cx, cy, mw, mh = p2["geom"]
print("画布 %dx%d  嘴中心 (%.0f,%.0f) 唇宽 %.0f 唇高 %.0f" % (W, H, cx, cy, mw, mh))


def lip_gap(fr):
    """量唇缝：嘴部矩形里逐行亮度，找最暗的一条带的宽度（>阈值算张开）。"""
    x0 = int(max(0, cx - mw * 0.6)); x1 = int(min(W, cx + mw * 0.6))
    y0 = int(max(0, cy - mh * 1.6)); y1 = int(min(H, cy + mh * 1.6))
    roi = cv2.cvtColor(fr[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32)
    row = roi.mean(axis=1)
    base = np.median(row)
    dark = row < (base - 12)
    return int(dark.sum()), float(row.min()), base


tiles = []
for op in (0.0, 0.5, 1.0):
    fr = A2._frame(p2, op, 1.0, amp=1.2)
    fr = np.asarray(fr.convert("RGB"))
    g, mn, base = lip_gap(fr)
    print("op=%.1f  暗带行数 %d  最暗 %.1f  基准 %.1f" % (op, g, mn, base))
    # 加伪3D 再量一次（确认 warp 不会抹掉口型）
    fw = A25._warp(fr, p3, 3.0, -2.0, 1.0, 4.0, 2.0, 1.004)
    g2, _, _ = lip_gap(fw)
    print("      +伪3D 后 暗带行数 %d" % g2)
    tiles.append(fr[int(cy - mh * 2):int(cy + mh * 2),
                    int(cx - mw * 0.8):int(cx + mw * 0.8)])

hmax = max(t.shape[0] for t in tiles)
tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 0,
                            cv2.BORDER_CONSTANT, value=(0, 0, 0)) for t in tiles]
strip = np.hstack(tiles)
strip = cv2.resize(strip, (strip.shape[1] * 2, strip.shape[0] * 2),
                   interpolation=cv2.INTER_NEAREST)
cv2.imwrite("_p25d_mouth.png", cv2.cvtColor(strip, cv2.COLOR_RGB2BGR))
print("已存 _p25d_mouth.png（左=闭嘴 中=半张 右=全张）")

# 全片口型曲线
sr, y = A2._read_wav(A2._to_wav16k(sorted(
    [os.path.join(ROOT, "data", "tts", f) for f in os.listdir(os.path.join(ROOT, "data", "tts"))
     if f.endswith(".mp3")], key=os.path.getmtime)[-1]))
params = A2._build_params(y, sr, [], A2.FPS, amp=1.2)
ops = np.array([p[0] for p in params])
print("全片张口度: 均值 %.3f 中位 %.3f 最大 %.3f  >0.5 的帧 %.0f%%"
      % (ops.mean(), np.median(ops), ops.max(), (ops > 0.5).mean() * 100))
