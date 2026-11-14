"""端到端验证：真实出片 → 测头发与脸的位移是否一致（分裂检测）+ 抽搐指标。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

from backend import avatar_lipsync as AL
from backend import avatar25d as A25
from backend import avatar2 as A2

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_v")
os.makedirs(OUT, exist_ok=True)

text = "大家好，这是修复之后的数字人效果。头部转动的时候，头发应该跟着一起动，不能留在原地。"

# 1) 出片（眨眼关；speak 内部自己做 TTS）
res = AL.speak(text, blink=False, head=True, max_side=1024, motion=1.0)
print("出片返回:", {k: res.get(k) for k in
                   ("ok", "error", "path", "frames", "seconds", "motion", "size") if k in res})
if not res.get("ok"):
    print("出片失败"); sys.exit(1)
mp4 = res.get("path") or ""
if not os.path.isfile(mp4):
    print("找不到视频:", mp4); sys.exit(1)
import shutil
shutil.copyfile(mp4, os.path.join(OUT, "fix.mp4"))

# 3) 抽帧
cap = cv2.VideoCapture(mp4)
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
cap.release()
print("读到帧:", len(frames))
if len(frames) < 20:
    print("帧太少"); sys.exit(1)

prep = A25._prep3d(A2._img_path(), max_side=1024)
W, H = prep["wh"]
mk = prep["mask"]
ys, xs = np.where(mk > 0)
fh = float(ys.max() - ys.min())
fw = float(xs.max() - xs.min())

# 区域：头发（头顶上方）、脸（面部中心）
hair_box = (int(xs.min() + fw * 0.20), max(0, int(ys.min() - fh * 0.30)),
            int(xs.min() + fw * 0.80), max(10, int(ys.min() - fh * 0.04)))
face_box = (int(xs.min() + fw * 0.25), int(ys.min() + fh * 0.20),
            int(xs.min() + fw * 0.75), int(ys.min() + fh * 0.55))
print("头发区:", hair_box, " 脸区:", face_box)

def track(tpl_img, box, target, pad=26):
    x0, y0, x1, y1 = box
    tpl = tpl_img[y0:y1, x0:x1]
    if tpl.size == 0:
        return None
    tx0 = max(0, x0 - pad); ty0 = max(0, y0 - pad)
    tx1 = min(target.shape[1], x1 + pad); ty1 = min(target.shape[0], y1 + pad)
    if tx1 - tx0 < tpl.shape[1] or ty1 - ty0 < tpl.shape[0]:
        return None
    r = cv2.matchTemplate(target[ty0:ty1, tx0:tx1], tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    return (tx0 + loc[0] - x0, ty0 + loc[1] - y0, mx)

base = frames[0]
print("\n[分裂检测] 相对第 0 帧的位移（头发 vs 脸，应同向且幅度接近）")
rows = []
for i in range(5, len(frames), max(1, len(frames) // 12)):
    h = track(base, hair_box, frames[i])
    f = track(base, face_box, frames[i])
    if h and f:
        rows.append((i, h, f))
        print(f"  帧{i:4d}  头发 dx={h[0]:+3d} dy={h[1]:+3d}(c={h[2]:.2f})   "
              f"脸 dx={f[0]:+3d} dy={f[1]:+3d}(c={f[2]:.2f})   "
              f"差={abs(h[0]-f[0])+abs(h[1]-f[1])}")

if rows:
    hd = np.array([[r[1][0], r[1][1]] for r in rows], float)
    fd = np.array([[r[2][0], r[2][1]] for r in rows], float)
    print(f"\n  头发位移幅度 mean={np.abs(hd).mean():.2f}px max={np.abs(hd).max():.0f}px")
    print(f"  脸位移幅度   mean={np.abs(fd).mean():.2f}px max={np.abs(fd).max():.0f}px")
    # 同向率：两者 dx 符号一致的帧占比
    same = np.mean(np.sign(hd[:, 0]) == np.sign(fd[:, 0]))
    print(f"  dx 同向率={same*100:.0f}%  (>70% 说明一起动)")
    print(f"  平均差异={np.abs(hd-fd).mean():.2f}px  (<2px 说明没有分裂)")

# 4) 抽搐：相邻帧全图差异的抖动（二阶差分）
print("\n[抽搐检测] 相邻帧像素差的二阶差分（尖峰=抽搐）")
diffs = []
for i in range(1, len(frames)):
    d = np.abs(frames[i].astype(np.int16) - frames[i-1].astype(np.int16)).mean()
    diffs.append(d)
diffs = np.array(diffs)
d2 = np.abs(np.diff(diffs))
print(f"  帧间差 mean={diffs.mean():.2f} max={diffs.max():.2f}")
print(f"  二阶差 max={d2.max():.3f} p99={np.percentile(d2,99):.3f}")
big = int((d2 > diffs.mean() * 1.2).sum())
print(f"  突变帧数(>1.2×均值)={big} / {len(d2)}")

# 5) 抽 3 帧拼图看头发
idx = [len(frames)//6, len(frames)//2, len(frames)*5//6]
tiles = [cv2.resize(frames[i], (420, 420), interpolation=cv2.INTER_AREA) for i in idx]
sep = np.full((420, 6, 3), 255, np.uint8)
cv2.imwrite(os.path.join(OUT, "hair_check.png"),
            cv2.cvtColor(np.hstack([tiles[0], sep, tiles[1], sep, tiles[2]]),
                         cv2.COLOR_RGB2BGR))
print("\n图: _v/hair_check.png  帧", idx)
print("视频:", mp4)
