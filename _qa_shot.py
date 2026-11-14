# -*- coding: utf-8 -*-
"""并排对比：旧/新编码同一位置帧（含嘴部 4 倍放大）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

VD = r"D:\local-multimodal-src\data\avatar2\videos"
old = os.path.join(VD, "lipsync_20261114_185444_aa0c.mp4")
new = os.path.join(VD, "lipsync_20261114_190718_1327.mp4")
outdir = r"D:\local-multimodal-src\_qa_out"
os.makedirs(outdir, exist_ok=True)

def grab(p, idx):
    cap = cv2.VideoCapture(p)
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, f = cap.read()
    cap.release()
    return f if ok else None

a = grab(old, 30)
b = grab(new, 30)
if a is None or b is None:
    print("取帧失败", a is None, b is None); sys.exit(1)
b = cv2.resize(b, (a.shape[1], a.shape[0]))

# 上半部：整帧并排
top = np.hstack([a, np.full((a.shape[0], 8, 3), 255, np.uint8), b])
# 下半部：嘴部放大 3 倍（中心区域）
def mouth(img):
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.52), int(h * 0.78)
    x0, x1 = int(w * 0.30), int(w * 0.70)
    c = img[y0:y1, x0:x1]
    return cv2.resize(c, (c.shape[1] * 3, c.shape[0] * 3), interpolation=cv2.INTER_CUBIC)
ma, mb = mouth(a), mouth(b)
if ma.shape != mb.shape:
    mb = cv2.resize(mb, (ma.shape[1], ma.shape[0]))
bot = np.hstack([ma, np.full((ma.shape[0], 8, 3), 255, np.uint8), mb])
W = max(top.shape[1], bot.shape[1])
def pad(x):
    if x.shape[1] < W:
        x = np.hstack([x, np.full((x.shape[0], W - x.shape[1], 3), 255, np.uint8)])
    return x
top, bot = pad(top), pad(bot)
gap = np.full((10, W, 3), 255, np.uint8)
canvas = np.vstack([top, gap, bot])
# 缩到合适宽度
if canvas.shape[1] > 1400:
    s = 1400.0 / canvas.shape[1]
    canvas = cv2.resize(canvas, (1400, int(canvas.shape[0] * s)))
p = os.path.join(outdir, "enc_cmp.png")
cv2.imwrite(p, canvas)
print("saved", p, canvas.shape)
