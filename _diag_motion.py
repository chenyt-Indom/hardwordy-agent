"""诊断：头发分裂 / 眨眼偏移 / 抽搐 三个问题的量化根因。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

from backend import avatar25d as A25
from backend import avatar2 as A2

img_path = A2._img_path()
print("形象图:", img_path)
prep = A25._prep3d(img_path, max_side=1024)
W, H = prep["wh"]
mk = prep["mask"]
wg = prep["wgt"]
x0, y0, x1, y1 = prep["roi"]
print(f"画布 {W}x{H}  roi=({x0},{y0},{x1},{y1})")

ys, xs = np.where(mk > 0)
print(f"脸网格 bbox: x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]")
print(f"roi 相对脸 bbox 的外扩: 左{xs.min()-x0} 右{x1-xs.max()} 上{ys.min()-y0} 下{y1-ys.max()}")

# ---- 1) 头发区权重（脸 bbox 上方 / 左右外侧）
top_band = wg[max(0, ys.min() - int((ys.max() - ys.min()) * 0.35)):ys.min(), xs.min():xs.max()]
print(f"\n[1] 头顶上方条带 wgt: mean={top_band.mean():.3f} max={top_band.max():.3f} >0.1占比={float((top_band>0.1).mean())*100:.1f}%")

left_band = wg[ys.min():ys.max(), max(0, xs.min() - int((xs.max()-xs.min())*0.25)):xs.min()]
print(f"    左侧外侧条带 wgt: mean={left_band.mean():.3f} max={left_band.max():.3f} >0.1占比={float((left_band>0.1).mean())*100:.1f}%")

# roi 之外的行（如果头发超出 roi，完全不 warp）
above_roi = wg[:y0, :]
print(f"    roi 上方(wg 有意义区): wgt mean={above_roi.mean():.4f} max={above_roi.max():.3f}")

# ---- 2) 眨眼 ROI 实际尺寸
eyes = prep["eyes"]
gap = prep["eye_gap"]
print(f"\n[2] 眼睛中心: {[(round(e[0],1), round(e[1],1)) for e in eyes]}  眼距={gap:.1f}px")
ew = gap * 0.42
eh = ew * 0.62
print(f"    当前眨眼半宽={ew:.1f} 半高={eh:.1f} → ROI {2*ew:.0f}x{2*eh:.0f}px")
print(f"    单眼实际宽约 {gap*0.36:.1f}px、眼裂高约 {gap*0.12:.1f}px")
print(f"    ⚠ ROI 宽是单眼宽的 {2*ew/(gap*0.36):.2f} 倍、高是眼裂的 {2*eh/(gap*0.12):.2f} 倍")
for i, (ex, ey) in enumerate(eyes):
    print(f"    眼{i} ROI: x[{int(ex-ew)},{int(ex+ew)}] y[{int(ey-eh)},{int(ey+eh)}]")

# ---- 3) 姿态曲线帧间连续性
n = 300
fps = 25.0
rng = np.random.RandomState(0)
energy = np.clip(np.sin(np.linspace(0, 12, n)) * 0.5 + 0.5 + rng.randn(n) * 0.05, 0, 1)
poses = A25._pose_curve(n, fps, energy)
P = np.array(poses, dtype=np.float32)
names = ["yaw", "pitch", "roll", "tx", "ty", "scl", "blink"]
print(f"\n[3] 姿态曲线 {n} 帧，各维帧间差分（绝对值最大 / 均值）：")
for i, nm in enumerate(names):
    d = np.abs(np.diff(P[:, i]))
    print(f"    {nm:6s} 范围[{P[:,i].min():7.2f},{P[:,i].max():7.2f}] 帧间Δmax={d.max():6.2f} Δmean={d.mean():6.3f}")

# 二阶差分（加速度 = 抽搐感）
print("    二阶差分（突变指标）：")
for i, nm in enumerate(names):
    d2 = np.abs(np.diff(P[:, i], 2))
    print(f"    {nm:6s} Δ²max={d2.max():6.3f} Δ²mean={d2.mean():6.3f}")

# 眨眼次数
bl = P[:, 6]
on = (bl > 0.02).astype(int)
starts = int(((np.diff(np.r_[0, on]) == 1)).sum())
print(f"\n    眨眼次数={starts}  单次持续约 {float(on.sum())/max(starts,1)/fps:.2f}s")
print(f"    眨眼峰值={bl.max():.2f}")
