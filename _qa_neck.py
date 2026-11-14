"""A/B 实验：脖子 zn 归零 + 权重快速衰减，看能不能消掉反向对拉。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
base = A25._prep3d(P, max_side=1024)
W, H = base["wh"]; mk = base["mask"]; wg0 = base["wgt"]; zn0 = base["zn"]
ys, xs = np.where(mk > 0)
ymax = int(ys.max()); cx, cy = base["cx"], base["cy"]
img = base["img"]
gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

# 脖子带：网格底以下 0~90px（真正的脖子）
ny0, ny1 = ymax, min(H, ymax + 90)
face_y0, face_y1 = int(ys.min()) + 100, ymax - 40   # 脸部参考带

def variant(zn_mode, w_mode):
    p = dict(base)
    zn = zn0.copy(); wg = wg0.copy()
    yy = np.arange(H, dtype=np.float32)[:, None] * np.ones((1, W), np.float32)
    below = np.clip((yy - ymax) / 70.0, 0.0, 1.0)      # 0=下巴线 1=下方70px
    if zn_mode == "zero":
        # 脖子是中轴，深度应居中于脸（zn≈0），不能沿用下巴侧面的深度
        zn = zn * (1.0 - below)
    if w_mode == "fast":
        # 脖子权重按距离快速衰减，而不是 sigma=14 的高斯各向同性扩散
        wg = wg * (1.0 - below) ** 1.5
    p["zn"] = zn.astype(np.float32); p["wgt"] = wg.astype(np.float32)
    return p

def measure(p, tag):
    rows = []
    for name, kw in (("yaw7.5", dict(yaw=7.5)), ("yaw-7.5", dict(yaw=-7.5)),
                     ("roll5", dict(roll=5)), ("pitch6", dict(pitch=6))):
        out = A25._warp(img, p, **kw)
        g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
        g0 = gray.astype(np.float32)
        (fx, _), _ = cv2.phaseCorrelate(g0[face_y0:face_y1], g1[face_y0:face_y1])
        (nx, _), _ = cv2.phaseCorrelate(g0[ny0:ny1], g1[ny0:ny1])
        d = np.abs(g1[ny0:ny1] - g0[ny0:ny1])
        rows.append((name, fx, nx, d.mean(), (d > 8).mean() * 100))
    print(f"\n--- {tag} ---")
    print(" pose      脸dx    脖dx   同向?  脖pixdiff  >8占比")
    for name, fx, nx, dm, dp in rows:
        same = "是" if (fx == 0 or nx == 0 or (fx > 0) == (nx > 0)) else "否⚠"
        print(f" {name:8s} {fx:+6.2f} {nx:+6.2f}  {same:4s}  {dm:7.3f}  {dp:6.2f}%")

measure(base, "原版")
measure(variant("zero", "none"), "A: 脖子 zn→0")
measure(variant("none", "fast"), "B: 脖子权重快速衰减")
measure(variant("zero", "fast"), "A+B 同时")
