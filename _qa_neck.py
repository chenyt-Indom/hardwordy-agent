"""诊断2：脖子区域的实际运动。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
prep = A25._prep3d(P, max_side=1024)
W, H = prep["wh"]; mk = prep["mask"]; wg = prep["wgt"]; zn = prep["zn"]
cx, cy, fw, fh = prep["cx"], prep["cy"], prep["fw"], prep["fh"]
ys, xs = np.where(mk > 0)
print(f"IMG {W}x{H}  face cx={cx:.0f} cy={cy:.0f} fw={fw:.0f} fh={fh:.0f}")
print(f"mesh  y=[{ys.min()},{ys.max()}]  x=[{xs.min()},{xs.max()}]  scale={prep['scale']:.2f}")
print(f"wgt>0.5 y=[{np.where(wg>0.5)[0].min()},{np.where(wg>0.5)[0].max()}]")

img = prep["img"]
# 找衣领位置：黑色区域起点
gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
for y in range(H-1, 0, -1):
    if gray[y].mean() > 90:
        print(f"画面底部亮行（衣服下缘以上）y={y}")
        break
# 每 20 行统计暗度（衣服黑）
print("\nrow  mean_gray  wgt  zn   |  yaw7.5_dx  pitch6_dy  roll5_dx")
rows = list(range(int(ys.max())-20, min(H, int(ys.max())+360), 20))
for y in rows:
    rw = wg[y]; rz = zn[y]
    m = rw > 0.02
    zs = np.median(rz[m]) if m.any() else 0.0
    syw = math.sin(math.radians(7.5)); sxp = math.sin(math.radians(6.0)); sr = math.sin(math.radians(5.0))
    d1 = prep["scale"]*zs*syw*rw.mean()
    d2 = prep["scale"]*zs*sxp*rw.mean()
    d3 = abs(y - cy)*sr*rw.mean()
    print(f"{y:4d}  {gray[y].mean():6.1f}  {rw.mean():.3f} {zs:+.3f} | {d1:+7.2f} {d2:+7.2f} {d3:+7.2f}")

# 实测三档姿态下，脖子带（下巴下 40~160px）的像素变化
print("\n=== 脖子带实测（网格底部以下 40~160px）===")
y0 = min(H-1, int(ys.max())+40); y1 = min(H, int(ys.max())+160)
for name, kw in (("yaw7.5", dict(yaw=7.5)), ("yaw-7.5", dict(yaw=-7.5)),
                 ("pitch6", dict(pitch=6)), ("roll5", dict(roll=5))):
    out = A25._warp(img, prep, **kw)
    d = np.abs(out[y0:y1].astype(np.float32) - img[y0:y1].astype(np.float32))
    print(f"  {name:8s} neck pixdiff mean={d.mean():.3f} max={d.max():.1f} >8的占比={(d>8).mean()*100:.2f}%")
