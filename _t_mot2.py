"""测：Wav2Lip 帧 + 25D 动作层，三档 motion 的实际效果"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2
from backend import avatar2 as A2
from backend import avatar_lipsync as AL

# 找一段短的 tts 音频
cands = glob.glob(os.path.join(A2._root(), "data", "**", "tts_*.mp3"), recursive=True)
cands = sorted([c for c in cands if os.path.getsize(c) < 200000], key=os.path.getsize)
print("候选音频:", [os.path.basename(c) for c in cands[:3]])
mp3 = cands[0]
wav = A2._to_wav16k(mp3)
print("wav:", wav, os.path.getsize(wav))

img = A2._img_path()
os.makedirs("_t_out", exist_ok=True)
results = {}
for lvl in (0.0, 1.0, 1.6):
    out = os.path.join(A2._root(), "_t_out", "mot_%.1f.mp4" % lvl)
    t0 = time.time()
    r = AL.render(img, wav, out, blink=True, head=True, max_side=1024, motion=lvl)
    dt = time.time() - t0
    if not r.get("ok"):
        print("motion=%.1f FAIL: %s" % (lvl, r.get("error")))
        continue
    print("motion=%.1f  %5.1fs  frames=%d  mode=%s  motion_sec=%.2f  size=%s"
          % (lvl, dt, r["frames"], r["motion"], r["motion_sec"], r["size"]))
    results[lvl] = out

# 抽帧测：头部位移（用整帧与中位帧的互相关位移）
def frame_shift(path):
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok: break
        frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
    cap.release()
    if len(frames) < 6:
        return None
    # 取每 5 帧，和第一帧做相位相关求位移
    ref = frames[0].astype(np.float32)
    h, w = ref.shape
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    shifts = []
    for i in range(0, len(frames), 5):
        f2 = frames[i].astype(np.float32)
        (dx, dy), _ = cv2.phaseCorrelate(ref, f2, win)
        shifts.append((dx, dy))
    a = np.array(shifts)
    return {"n": len(frames), "dx": (a[:, 0].min(), a[:, 0].max()),
            "dy": (a[:, 1].min(), a[:, 1].max()),
            "std": float(np.hypot(a[:, 0], a[:, 1]).std()),
            "uniq": len({(round(x, 1), round(y, 1)) for x, y in shifts})}

for lvl, p in results.items():
    s = frame_shift(p)
    if s:
        print("motion=%.1f  frames=%d  dx %+.1f~%+.1f  dy %+.1f~%+.1f  std=%.2f  不同姿态=%d"
              % (lvl, s["n"], s["dx"][0], s["dx"][1], s["dy"][0], s["dy"][1], s["std"], s["uniq"]))
