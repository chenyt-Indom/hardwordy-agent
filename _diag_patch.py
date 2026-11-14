# -*- coding: utf-8 -*-
"""从最近生成的成片抽帧，量化「割裂」：清晰度断层 / 色差 / 边界梯度"""
import sys, os, glob, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar2 as A2

vids = sorted(glob.glob("data/avatar2/videos/*.mp4"), key=os.path.getmtime, reverse=True)
print("最近成片:")
for v in vids[:4]:
    print("  %s  %.1fMB  %s" % (os.path.basename(v), os.path.getsize(v)/1e6,
                                __import__("time").strftime("%m-%d %H:%M", __import__("time").localtime(os.path.getmtime(v)))))

exe = A2.ffmpeg()
tmp = "_frames"
os.makedirs(tmp, exist_ok=True)
for f in glob.glob(tmp + "/*.png"):
    os.remove(f)

v = vids[0]
print("\n抽帧:", os.path.basename(v))
subprocess.run([exe, "-y", "-loglevel", "error", "-i", v, "-vf", "fps=2", tmp + "/f%03d.png"], check=False)
fs = sorted(glob.glob(tmp + "/*.png"))
print("抽到 %d 帧" % len(fs))
if not fs:
    raise SystemExit("无帧")

img0 = cv2.imread("data/avatar2/avatar.png")
H, W = img0.shape[:2]
print("原图 %dx%d" % (W, H))

# 人脸框（用于定位椭圆）
from backend import avatar_lipsync as AL
from backend.face_detect_scrfd import SCRFD
det = SCRFD(AL._paths()["det"])
dets, _ = det.detect(img0, thresh=0.5)
d = max(dets, key=lambda t: (t[2]-t[0])*(t[3]-t[1]))
bx = [max(0, int(d[0])), max(0, int(d[1])), min(W, int(d[2])), min(H, int(d[3]))]
bw, bh = bx[2]-bx[0], bx[3]-bx[1]
print("人脸框", bx, "尺寸 %dx%d" % (bw, bh))

# 椭圆参数（与 _composite 一致）
cx = bx[0] + int(bw*0.50)
cy = bx[1] + int(bh*0.76)
ax = int(bw*0.31); ay = int(bh*0.22)
print("椭圆 中心(%d,%d) 半径(%d,%d)  面积占脸框 %.1f%%" % (cx, cy, ax, ay,
      100.0*np.pi*ax*ay/(bw*bh)))

# 构造椭圆内/外的掩版
m_in = np.zeros((H, W), np.uint8)
cv2.ellipse(m_in, (cx, cy), (int(ax*0.55), int(ay*0.55)), 0, 0, 360, 1, -1)
m_ring = np.zeros((H, W), np.uint8)   # 椭圆外一圈（同一张脸上）
cv2.ellipse(m_ring, (cx, cy), (int(ax*1.6), int(ay*1.6)), 0, 0, 360, 1, -1)
cv2.ellipse(m_ring, (cx, cy), (int(ax*1.25), int(ay*1.25)), 0, 0, 360, 0, -1)
print("椭圆内 %d 像素 / 环带 %d 像素" % (m_in.sum(), m_ring.sum()))


def sharp(p):
    g = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    return float(lap.var())


print("\n=== 清晰度（拉普拉斯方差，越大越锐） ===")
print("  原图  椭圆内 %.1f   环带 %.1f   比值 %.2f" % (
    sharp(img0[m_in > 0].reshape(-1, 1)) if False else 0, 0, 0))
gi = cv2.cvtColor(img0, cv2.COLOR_BGR2GRAY).astype(np.float32)
li = cv2.Laplacian(gi, cv2.CV_32F)
print("  原图      椭圆内 %8.1f   环带 %8.1f   比值 %.2f" % (
    li[m_in > 0].var(), li[m_ring > 0].var(), li[m_in > 0].var()/max(1e-6, li[m_ring > 0].var())))

vals = []
for f in fs[::max(1, len(fs)//6)][:6]:
    p = cv2.imread(f)
    if p is None:
        continue
    if p.shape[:2] != (H, W):
        p = cv2.resize(p, (W, H), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
    l = cv2.Laplacian(g, cv2.CV_32F)
    a, b = l[m_in > 0].var(), l[m_ring > 0].var()
    # 色差
    da = np.abs(p[m_in > 0].astype(np.float32) - img0[m_in > 0].astype(np.float32)).mean()
    db = np.abs(p[m_ring > 0].astype(np.float32) - img0[m_ring > 0].astype(np.float32)).mean()
    vals.append((os.path.basename(f), a, b, a/max(1e-6, b), da, db))
    print("  %s  椭圆内 %7.1f  环带 %7.1f  比值 %.2f   色差 内%.2f 外%.2f" % vals[-1])

if vals:
    r = np.mean([v[3] for v in vals])
    print("\n平均清晰度比（内/外）= %.2f   （1.0 = 完全一致；<0.8 肉眼可辨「补丁」）" % r)
    print("平均色差  内 %.2f / 外 %.2f" % (np.mean([v[4] for v in vals]), np.mean([v[5] for v in vals])))

# 椭圆边界梯度突变
print("\n=== 椭圆边缘的梯度突变 ===")
p = cv2.imread(fs[len(fs)//2])
if p.shape[:2] != (H, W):
    p = cv2.resize(p, (W, H), interpolation=cv2.INTER_AREA)
ang = np.linspace(0, 2*np.pi, 720, endpoint=False)
pts = np.stack([cx + ax*np.cos(ang), cy + ay*np.sin(ang)], 1)
gin = cv2.cvtColor(img0, cv2.COLOR_BGR2GRAY).astype(np.float32)
gout = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
# 沿法线方向取样：边界内外各 6px
diff_inside, diff_outside = [], []
for (px, py) in pts:
    nx, ny = (px-cx)/max(1e-6, ax), (py-cy)/max(1e-6, ay)
    nl = np.hypot(nx, ny) or 1.0
    nx, ny = nx/nl, ny/nl
    for dd, acc in ((4, diff_inside), (-4, diff_outside)):
        xx, yy = int(px + nx*dd), int(py + ny*dd)
        if 0 <= xx < W and 0 <= yy < H:
            acc.append(abs(gout[yy, xx] - gin[yy, xx]))
print("  椭圆内 4px 处平均改动 %.2f   椭圆外 4px 处平均改动 %.2f" % (
    np.mean(diff_inside), np.mean(diff_outside)))
print("  → 差值越大越像「贴了一块」")
