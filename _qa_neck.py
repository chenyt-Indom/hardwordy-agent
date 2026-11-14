"""诊断：脖子区域为什么会跟着头扭。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
prep = A25._prep3d(P, max_side=1024)
W, H = prep["wh"]
print("wh", W, H)
mk = prep["mask"]; wg = prep["wgt"]; zn = prep["zn"]
cx, cy, fw, fh = prep["cx"], prep["cy"], prep["fw"], prep["fh"]
print("face cx,cy", round(cx,1), round(cy,1), "fw,fh", round(fw,1), round(fh,1))

ys, xs = np.where(mk > 0)
print("mesh mask y range", ys.min(), ys.max(), " x range", xs.min(), xs.max())

# 头部权重区域
hy, hx = np.where(wg > 0.5)
print("wgt>0.5  y range", hy.min(), hy.max())
hy2, hx2 = np.where(wg > 0.05)
print("wgt>0.05 y range", hy2.min(), hy2.max())

# 按行统计：每行 wgt 均值、最大位移(px)
print("\n row   wgt_mean  wgt_max  zn_med   disp_yaw7.5(px)  disp_pitch7(px)")
for y in range(int(ys.max()) - 60, min(H, int(ys.max()) + 320), 20):
    row_w = wg[y]
    row_z = zn[y]
    m = row_w > 0.02
    if not m.any():
        print(f"{y:5d}   {row_w.mean():.3f}    {row_w.max():.3f}     --       0.00            0.00")
        continue
    zs = np.median(row_z[m])
    syw = math.sin(math.radians(7.5))
    sxp = math.sin(math.radians(7.0))
    dy = prep["scale"] * zs * syw * row_w.mean()
    dp = prep["scale"] * zs * sxp * row_w.mean()
    print(f"{y:5d}   {row_w.mean():.3f}    {row_w.max():.3f}    {zs:+.3f}    {abs(dy):6.2f}          {abs(dp):6.2f}")

print("\nscale =", round(prep["scale"], 2), " zspan =", round(prep["zspan"], 2))

# 实际 warp 前后的行位移（用模板匹配测每行的水平位移）
img = prep["img"]
for yaw in (7.5,):
    out = A25._warp(img, prep, yaw=yaw, pitch=0.0, roll=0.0, iters=2)
    print(f"\n=== yaw={yaw} 每行实测水平位移（相位相关）===")
    g0 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
    for y in range(int(ys.max()) - 40, min(H, int(ys.max()) + 340), 40):
        band0 = g0[y:y+40]; band1 = g1[y:y+40]
        if band0.std() < 1 or band1.std() < 1:
            print(f"  y={y:4d}  平坦")
            continue
        (dx, dy), resp = cv2.phaseCorrelate(band0, band1)
        diff = np.abs(g1[y:y+40] - g0[y:y+40]).mean()
        print(f"  y={y:4d}  dx={dx:+6.2f}  dy={dy:+6.2f}  resp={resp:.3f}  pixdiff={diff:.2f}")
