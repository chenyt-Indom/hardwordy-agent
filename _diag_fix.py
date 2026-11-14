"""验证修复：头发权重 / 眨眼位置 / 抽搐 / 眨眼曲线平滑度。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

from backend import avatar25d as A25
from backend import avatar2 as A2

img_path = A2._img_path()
prep = A25._prep3d(img_path, max_side=1024)
W, H = prep["wh"]
mk, wg = prep["mask"], prep["wgt"]
x0, y0, x1, y1 = prep["roi"]
ys, xs = np.where(mk > 0)
fw = float(xs.max() - xs.min())
fh = float(ys.max() - ys.min())
print(f"画布 {W}x{H}  roi=({x0},{y0},{x1},{y1})")
print(f"脸 bbox x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]")

# ---- 1) 头发区权重（修复前 0.025 / 0.047）
top = wg[max(0, ys.min() - int(fh * 0.35)):ys.min(), xs.min():xs.max()]
left = wg[ys.min():ys.max(), max(0, xs.min() - int(fw * 0.25)):xs.min()]
print(f"\n[1] 头顶条带 wgt mean={top.mean():.3f} max={top.max():.3f} >0.9占比={float((top>0.9).mean())*100:.1f}%")
print(f"    左侧条带 wgt mean={left.mean():.3f} max={left.max():.3f} >0.9占比={float((left>0.9).mean())*100:.1f}%")
print(f"    脸内 wgt mean={wg[mk>0].mean():.3f}")

# ---- 2) _shift_of 方向是否与 _warp 一致（用实际像素验证）
print("\n[2] _shift_of 与 _warp 一致性（yaw=7.5°）：")
yaw = 7.5
ex, ey = prep["eyes"][0]
dx, dy = A25._shift_of(prep, ex, ey, yaw=yaw, pitch=0.0)
warped = A25._warp(prep["img"].copy(), prep, yaw=yaw, pitch=0.0, iters=2)
# 在 warp 后图上，用局部模板匹配找眼睛实际位置
pad = 40
rx0 = int(max(0, ex + dx - pad)); rx1 = int(min(W, ex + dx + pad))
ry0 = int(max(0, ey + dy - pad)); ry1 = int(min(H, ey + dy + pad))
tpl = prep["img"][int(ey - 18):int(ey + 18), int(ex - 24):int(ex + 24)]
if tpl.size and rx1 - rx0 > tpl.shape[1] and ry1 - ry0 > tpl.shape[0]:
    res = cv2.matchTemplate(warped[ry0:ry1, rx0:rx1], tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    real_dx = (rx0 + loc[0] + 24) - ex
    real_dy = (ry0 + loc[1] + 18) - ey
    print(f"    预测位移=({dx:+.1f},{dy:+.1f})  实测位移=({real_dx:+.1f},{real_dy:+.1f})")
    print(f"    误差={math.hypot(dx-real_dx, dy-real_dy):.2f}px  置信度={mx:.3f}")

# ---- 3) 眨眼曲线平滑度
n, fps = 300, 25.0
rng = np.random.RandomState(0)
energy = np.clip(np.sin(np.linspace(0, 12, n)) * 0.5 + 0.5 + rng.randn(n) * 0.05, 0, 1)
poses = A25._pose_curve(n, fps, energy)
P = np.array(poses, dtype=np.float32)
bl = P[:, 6]
d = np.abs(np.diff(bl))
on = (bl > 0.02).astype(int)
starts = int((np.diff(np.r_[0, on]) == 1).sum())
print(f"\n[3] 眨眼曲线: 次数={starts} 峰值={bl.max():.2f} 帧间Δmax={d.max():.3f} Δmean={d.mean():.4f}")
print(f"    单次持续≈{on.sum()/max(starts,1)/fps:.2f}s")
for nm, i in [("yaw", 0), ("pitch", 1), ("roll", 2), ("tx", 3), ("ty", 4)]:
    dd = np.abs(np.diff(P[:, i]))
    print(f"    {nm:5s} 帧间Δmax={dd.max():.3f} Δmean={dd.mean():.3f}")

# ---- 4) 眨眼帧：眼睛位置是否落在真实眼睛上
print("\n[4] 眨眼峰值渲染（amount=1.0, yaw=0）：")
frame = prep["img"].copy()
bl1 = A25._blink(frame, prep, 1.0, 0.0, 0.0, 0.0, 0.0)
diff = np.abs(bl1.astype(np.int16) - frame.astype(np.int16)).sum(2)
dy_, dx_ = np.where(diff > 12)
if dy_.size:
    print(f"    改动区域 x[{dx_.min()},{dx_.max()}] y[{dy_.min()},{dy_.max()}]  像素数={dy_.size}")
    for i, (ex, ey) in enumerate(prep["eyes"]):
        print(f"    眼{i} 中心=({ex:.0f},{ey:.0f})")
    # 改动区是否覆盖两个眼睛中心
    for i, (ex, ey) in enumerate(prep["eyes"]):
        hit = diff[int(max(0,ey-8)):int(ey+8), int(max(0,ex-10)):int(ex+10)].mean()
        print(f"    眼{i} 附近平均改动={hit:.1f} (>10 说明确实动了眼睛)")

# 存对比图
eyes_union = [prep["eyes"][0], prep["eyes"][1]]
cx = int(np.mean([e[0] for e in eyes_union]))
cy = int(np.mean([e[1] for e in eyes_union]))
rw, rh = 230, 120
ox0, ox1 = max(0, cx - rw), min(W, cx + rw)
oy0, oy1 = max(0, cy - rh), min(H, cy + rh)
crop_a = frame[oy0:oy1, ox0:ox1]
crop_b = bl1[oy0:oy1, ox0:ox1]
big = np.hstack([crop_a, np.full((crop_a.shape[0], 6, 3), 255, np.uint8), crop_b])
big = cv2.resize(big, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
cv2.imwrite(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_blink_cmp.png"),
            cv2.cvtColor(big, cv2.COLOR_RGB2BGR))
print("    对比图: _blink_cmp.png (左=原图 右=眨眼)")
