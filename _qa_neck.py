"""验证：脖子扭是否消除 + 脸部未被削弱 + 眨眼定位仍准。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
prep = A25._prep3d(P, max_side=1024)
W, H = prep["wh"]; mk = prep["mask"]
ys, xs = np.where(mk > 0); ymax = int(ys.max())
cx, cy = prep["cx"], prep["cy"]
img = prep["img"]; gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
ny0, ny1 = ymax, min(H, ymax + 90)
fy0, fy1 = int(ys.min()) + 120, ymax - 60
print(f"prep: cx={cx:.0f} cy={cy:.0f} 下颌线y={ymax} roll_pivot_y={prep['roll_pivot_y']:.0f}")

def run(tag, **kw):
    out = A25._warp(img, prep, **kw)
    g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
    dn = np.abs(g1[ny0:ny1] - gray[ny0:ny1])
    df = np.abs(g1[fy0:fy1] - gray[fy0:fy1])
    (fx, _), _ = cv2.phaseCorrelate(gray[fy0:fy1], g1[fy0:fy1])
    (nx, _), _ = cv2.phaseCorrelate(gray[ny0:ny1], g1[ny0:ny1])
    same = "OK" if (abs(fx) < 0.2 or abs(nx) < 0.2 or (fx > 0) == (nx > 0)) else "REV!"
    print(f" {tag:14s} 脖diff={dn.mean():6.3f} >8={ (dn>8).mean()*100:5.2f}% | "
          f"脸diff={df.mean():6.2f} | 脸dx={fx:+5.2f} 脖dx={nx:+5.2f} {same}")

print("\n=== 修复后（roll 上限 3.2°，支点在下颌线）===")
run("yaw +7.5", yaw=7.5)
run("yaw -7.5", yaw=-7.5)
run("pitch +6", pitch=6)
run("pitch -5", pitch=-5)
run("roll +3.2", roll=3.2)
run("roll -3.2", roll=-3.2)
run("roll5(旧值)", roll=5.0)
run("组合典型", yaw=4, pitch=3, roll=2, tx=8, ty=5)

# 眨眼定位一致性：_shift_of 与 _warp 实测是否吻合
print("\n=== 眨眼定位一致性（眼睛点被头带到哪）===")
for kw in (dict(yaw=6.0), dict(yaw=-6.0), dict(roll=3.0), dict(roll=-3.0),
           dict(yaw=4, roll=2, pitch=2, tx=8, ty=5)):
    dx, dy = A25._shift_of(prep, cx, cy - 150, **kw)   # 取一个眼睛附近的点
    # 实测：用 _warp 把该点周围的块挪过去，看峰值在哪
    x0, y0 = int(cx) - 40, int(cy) - 190
    patch = gray[y0:y0+80, x0:x0+80]
    out = A25._warp(img, prep, **kw)
    g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
    tm = cv2.matchTemplate(g1[max(0,y0-60):y0+140, max(0,x0-60):x0+140],
                           patch, cv2.TM_CCOEFF_NORMED)
    _, mx, _, mloc = cv2.minMaxLoc(tm)
    ex = mloc[0] + max(0, x0-60) - x0
    ey = mloc[1] + max(0, y0-60) - y0
    print(f" {str(kw):48s} 预测=({dx:+5.2f},{dy:+5.2f}) 实测=({ex:+5.2f},{ey:+5.2f}) "
          f"误差={math.hypot(dx-ex, dy-ey):5.2f}px conf={mx:.3f}")
