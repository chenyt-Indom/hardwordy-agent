"""测嘴部清晰度 vs 周围皮肤，确认残留「割裂」是否为清晰度差异。"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25
from backend import avatar_lipsync as AL

prep = A25._prep3d(r"D:\local-multimodal-src\data\avatar2\avatar.png", max_side=1024)
img = prep["img"]; mk = prep["mask"]
ys, xs = np.where(mk > 0)
cx, cy = prep["cx"], prep["cy"]

# 用 SCRFD 找嘴的位置（与引擎一致）
from backend import face_detect_scrfd as FD
det = FD.get() if hasattr(FD, "get") else FD.Detector()
r = det.detect(img) if hasattr(det, "detect") else None
print("detect:", None if r is None else len(r[1]) if isinstance(r, tuple) else "?")

def lapv(a):
    return cv2.Laplacian(a.astype(np.float32), cv2.CV_32F).var()

# 原图基准
g0 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
print(f"\n原图清晰度基准（拉普拉斯方差）")
regions = {
    "嘴部(96区)": (int(cy+40), int(cy+110), int(cx-70), int(cx+70)),
    "下巴": (int(cy+130), int(cy+190), int(cx-60), int(cx+60)),
    "左颊": (int(cy+20), int(cy+90), int(cx-180), int(cx-90)),
    "右颊": (int(cy+20), int(cy+90), int(cx+90), int(cx+180)),
    "额头": (int(cy-220), int(cy-140), int(cx-80), int(cx+80)),
    "鼻梁": (int(cy-60), int(cy+10), int(cx-35), int(cx+35)),
}
for nm, (y0, y1, x0, x1) in regions.items():
    print(f"  原图 {nm:10s} {lapv(g0[y0:y1, x0:x1]):8.2f}")

# 成片
vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
cap = cv2.VideoCapture(vs[0])
g = []
while True:
    ok, f = cap.read()
    if not ok: break
    g.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
cap.release()
# 取一张张嘴最大的帧
best, bi = -1, 0
for i, f in enumerate(g):
    y0, y1, x0, x1 = regions["嘴部(96区)"]
    d = (f[y0:y1, x0:x1] < 60).mean()
    if d > best: best, bi = d, i
print(f"\n张嘴最大帧 #{bi}（暗像素占比 {best*100:.1f}%）")
gm = g[bi]
print("成片清晰度 / 与原图比：")
for nm, (y0, y1, x0, x1) in regions.items():
    a, b = lapv(g0[y0:y1, x0:x1]), lapv(gm[y0:y1, x0:x1])
    print(f"  {nm:10s} 原图 {a:8.2f} → 成片 {b:8.2f}   比值 {b/max(a,1e-6):5.2f}")

cv2.imwrite(r"D:\local-multimodal-src\_qa_out\sharp_frame.png", cv2.cvtColor(gm, cv2.COLOR_GRAY2BGR))
print("\n帧已存 _qa_out/sharp_frame.png")
