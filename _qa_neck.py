"""端到端：出片 + 逐帧分析脖子区域动态。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar_lipsync as AL
from backend import avatar25d as A25

r = AL.speak("大家好，今天我们来聊一下这个方案的具体做法。",
             blink=False, motion=1.0, max_side=1024)
print("render:", r.get("ok"), r.get("duration"), "s", r.get("frames"), "帧",
      r.get("infer_sec"), "s", r.get("motion_sec"), "s")
if not r.get("ok"):
    print("ERR", r); sys.exit(1)

path = r["path"]
cap = cv2.VideoCapture(path)
frames = []
while True:
    ok, f = cap.read()
    if not ok: break
    frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32))
cap.release()
print("读回", len(frames), "帧", frames[0].shape)

H, W = frames[0].shape
prep = A25._prep3d(r"D:\local-multimodal-src\data\avatar2\avatar.png", max_side=1024)
mk = prep["mask"]; ys, xs = np.where(mk > 0); ymax = int(ys.max())
sc = W / 1024.0
ny0, ny1 = int(ymax * sc), int(min(H, ymax + 90) * sc)
fy0, fy1 = int((ys.min() + 120) * sc), int((ymax - 60) * sc)
print(f"脖子带 y=[{ny0},{ny1}]  脸部带 y=[{fy0},{fy1}]")

diffs, ndx, fdx = [], [], []
for i in range(1, len(frames)):
    d = np.abs(frames[i] - frames[i-1])
    diffs.append(d.mean())
    (a, _), _ = cv2.phaseCorrelate(frames[i-1][fy0:fy1], frames[i][fy0:fy1])
    (b, _), _ = cv2.phaseCorrelate(frames[i-1][ny0:ny1], frames[i][ny0:ny1])
    fdx.append(a); ndx.append(b)

diffs = np.array(diffs); fdx = np.array(fdx); ndx = np.array(ndx)
print(f"\n全图帧间差   mean={diffs.mean():.3f} max={diffs.max():.3f} p99={np.percentile(diffs,99):.3f}")
# 突变检测：> 1.2x 均值
spike = np.sum(diffs > diffs.mean() * 1.2)
print(f"突变帧（>1.2x均值）: {spike} / {len(diffs)}")
print(f"脸 dx 范围 [{fdx.min():+.2f},{fdx.max():+.2f}]  幅度 {fdx.max()-fdx.min():.2f}px")
print(f"脖 dx 范围 [{ndx.min():+.2f},{ndx.max():+.2f}]  幅度 {ndx.max()-ndx.min():.2f}px")
print(f"脖子/脸 位移幅度比 = {(ndx.max()-ndx.min())/max(1e-6, fdx.max()-fdx.min()):.2f}")
print(f"脖子/脸 同向帧占比 = {np.mean(np.sign(fdx)==np.sign(ndx))*100:.0f}%")

# 脖子带的帧间变化（只看脖子，排除脸）
nd = []
for i in range(1, len(frames)):
    nd.append(np.abs(frames[i][ny0:ny1] - frames[i-1][ny0:ny1]).mean())
nd = np.array(nd)
print(f"脖子带帧间差 mean={nd.mean():.3f} max={nd.max():.3f} p99={np.percentile(nd,99):.3f}")

# 存几张关键帧
out = r"D:\local-multimodal-src\_qa_out"
os.makedirs(out, exist_ok=True)
cap = cv2.VideoCapture(path)
idx = [0, len(frames)//4, len(frames)//2, 3*len(frames)//4]
for j, i in enumerate(idx):
    cap.set(cv2.CAP_PROP_POS_FRAMES, i)
    ok, f = cap.read()
    if ok:
        cv2.imwrite(os.path.join(out, f"neck_{j}.png"), f)
cap.release()
print("帧已存", out)
print("VIDEO", path)
