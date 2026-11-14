"""实验：roll/pitch 支点下移到颈椎底部，能否消掉脖子剪切。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
base = A25._prep3d(P, max_side=1024)
W, H = base["wh"]; mk = base["mask"]
ys, xs = np.where(mk > 0); ymax = int(ys.max())
cx, cy = base["cx"], base["cy"]
img = base["img"]; gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
ny0, ny1 = ymax, min(H, ymax + 90)          # 脖子带
fy0, fy1 = int(ys.min()) + 120, ymax - 60   # 脸部带

def pivot_shift(deg, d):
    """绕 (cx, cy+d) 旋转 = 绕 (cx,cy) 旋转 + 平移补偿。"""
    r = math.radians(deg)
    return d * math.sin(r), d * (1.0 - math.cos(r))

def run(tag, roll=0.0, pitch=0.0, d_roll=0.0, d_pitch=0.0):
    kw = {}
    if roll:
        tx, ty = pivot_shift(roll, d_roll)
        kw.update(roll=roll, tx=tx, ty=ty)
    if pitch:
        # pitch 的支点：纵向压缩中心下移，用 ty 补偿近似
        d = d_pitch
        kw.update(pitch=pitch, ty=-d * (1 - math.cos(math.radians(pitch))) * -1)
    out = A25._warp(img, base, **kw)
    g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
    dn = np.abs(g1[ny0:ny1] - gray[ny0:ny1])
    df = np.abs(g1[fy0:fy1] - gray[fy0:fy1])
    print(f" {tag:22s} 脖pixdiff={dn.mean():6.3f} >8={ (dn>8).mean()*100:5.2f}%  "
          f"脸pixdiff={df.mean():6.2f} 脸位移≈{abs(kw.get('tx',0)):.1f}px")

print("=== roll=5，支点从脸中心下移 d ===")
for d in (0, 80, 150, 230, 320):
    run(f"roll5 d={d}", roll=5.0, d_roll=d)

print("\n=== roll=3，支点下移 ===")
for d in (0, 150, 230):
    run(f"roll3 d={d}", roll=3.0, d_roll=d)

print("\n=== 参考：不动 ===")
run("noop", roll=0.0)

# 额外：把整条脖子+头作为刚体，用相位相关看脖子和脸是否同向
print("\n=== 同向性（roll5 d=230）===")
tx, ty = pivot_shift(5.0, 230)
out = A25._warp(img, base, roll=5.0, tx=tx, ty=ty)
g1 = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
(fx, _), r1 = cv2.phaseCorrelate(gray[fy0:fy1], g1[fy0:fy1])
(nx, _), r2 = cv2.phaseCorrelate(gray[ny0:ny1], g1[ny0:ny1])
print(f" 脸 dx={fx:+.2f}(resp {r1:.3f})  脖 dx={nx:+.2f}(resp {r2:.3f})  同向={'是' if (fx>0)==(nx>0) else '否'}")
