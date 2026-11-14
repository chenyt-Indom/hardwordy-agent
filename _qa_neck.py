"""确认：动作幅度是否够活 + 残留割裂定位。"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25
from backend import avatar_lipsync as AL

n, fps = 110, 25
e = np.abs(np.sin(np.arange(n) / 7.0)) * 0.8 + 0.1
poses = A25._pose_curve(n, fps, e)
arr = np.array(poses)
names = ["yaw", "pit", "rol", "tx", "ty", "scl", "blink"]
m = 1.0
print("原始曲线（乘 MOTION_GAIN/ROLL_DAMP 后，与 lipsync 一致）")
for i, nm in enumerate(names):
    v = arr[:, i].copy()
    if nm == "yaw": v *= m * AL.MOTION_GAIN
    elif nm == "pit": v *= m * AL.PITCH_BOOST
    elif nm == "rol": v *= m * AL.MOTION_GAIN * AL.ROLL_DAMP
    elif nm in ("tx", "ty"): v *= m * AL.MOTION_GAIN
    print(f"  {nm:5s} 范围 [{v.min():+7.2f}, {v.max():+7.2f}]  峰峰值 {v.max()-v.min():6.2f}  "
          f"帧间最大跳 {np.abs(np.diff(v)).max():.3f}")

# 割裂定位：用真实视频，逐帧测「背景/脖子/脸」三带的帧间差
import glob
vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
if not vs:
    print("no video"); sys.exit(0)
cap = cv2.VideoCapture(vs[0])
frames = []
while True:
    ok, f = cap.read()
    if not ok: break
    frames.append(f)
cap.release()
prep = A25._prep3d(r"D:\local-multimodal-src\data\avatar2\avatar.png", max_side=1024)
mk = prep["mask"]; ys, xs = np.where(mk > 0); ymax = int(ys.max())
H, W = frames[0].shape[:2]; sc = W / 1024.0
bands = {
    "背景左上": (int(20*sc), int(200*sc), int(20*sc), int(200*sc)),
    "脸部": (int((ys.min()+120)*sc), int((ymax-60)*sc), int(400*sc), int(620*sc)),
    "脖子": (int(ymax*sc), int((ymax+90)*sc), int(430*sc), int(600*sc)),
    "衣领": (int((ymax+120)*sc), int((ymax+200)*sc), int(380*sc), int(650*sc)),
}
print("\n各区域帧间差（成片 110 帧）")
for nm, (y0, y1, x0, x1) in bands.items():
    v = [np.abs(frames[i][y0:y1, x0:x1].astype(np.float32)
                - frames[i-1][y0:y1, x0:x1].astype(np.float32)).mean()
         for i in range(1, len(frames))]
    v = np.array(v)
    print(f"  {nm:8s} mean={v.mean():6.3f} max={v.max():6.3f} p99={np.percentile(v,99):6.3f}")

# 找全图变化最大的那一帧，存下来看割裂
d = [np.abs(frames[i].astype(np.float32) - frames[i-1].astype(np.float32)).mean()
     for i in range(1, len(frames))]
k = int(np.argmax(d)) + 1
cv2.imwrite(r"D:\local-multimodal-src\_qa_out\worst.png", frames[k])
cv2.imwrite(r"D:\local-multimodal-src\_qa_out\worst_prev.png", frames[k-1])
print(f"\n最大变化帧 #{k} diff={d[k-1]:.3f}，已存 worst.png / worst_prev.png")
