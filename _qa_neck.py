"""测嘴部清晰度 vs 周围皮肤（用实测唇缝坐标 y≈600）。"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

prep = A25._prep3d(r"D:\local-multimodal-src\data\avatar2\avatar.png", max_side=1024)
img = prep["img"]
cx, cy = prep["cx"], prep["cy"]

def lapv(a):
    return cv2.Laplacian(a.astype(np.float32), cv2.CV_32F).var()

g0 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
regions = {
    "嘴部": (560, 645, int(cx-75), int(cx+75)),
    "下巴": (655, 700, int(cx-60), int(cx+60)),
    "左颊": (480, 560, int(cx-165), int(cx-85)),
    "右颊": (480, 560, int(cx+85), int(cx+165)),
    "额头": (260, 345, int(cx-80), int(cx+80)),
    "鼻梁": (420, 495, int(cx-35), int(cx+35)),
    "脖子": (720, 790, int(cx-70), int(cx+70)),
}
print("原图清晰度（拉普拉斯方差）")
for nm, (y0, y1, x0, x1) in regions.items():
    print(f"  {nm:6s} {lapv(g0[y0:y1, x0:x1]):8.2f}")

vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
cap = cv2.VideoCapture(vs[0])
g = []
while True:
    ok, f = cap.read()
    if not ok: break
    g.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
cap.release()
best, bi = -1, 0
y0, y1, x0, x1 = regions["嘴部"]
for i, f in enumerate(g):
    d = (f[y0:y1, x0:x1] < 60).mean()
    if d > best: best, bi = d, i
print(f"\n张嘴最大帧 #{bi}/{len(g)}（嘴部暗像素 {best*100:.1f}%）")
gm = g[bi]
print("成片 vs 原图：")
for nm, (y0, y1, x0, x1) in regions.items():
    a, b = lapv(g0[y0:y1, x0:x1]), lapv(gm[y0:y1, x0:x1])
    flag = "  <-- 明显更糊" if b / max(a, 1e-6) < 0.75 else ""
    print(f"  {nm:6s} 原图 {a:8.2f} → 成片 {b:8.2f}   比值 {b/max(a,1e-6):5.2f}{flag}")

cv2.imwrite(r"D:\local-multimodal-src\_qa_out\sharp_frame.png", cv2.cvtColor(gm, cv2.COLOR_GRAY2BGR))
print("\n帧已存 _qa_out/sharp_frame.png")
