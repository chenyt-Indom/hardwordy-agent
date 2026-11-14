"""复测：优化后（iters=1）的速度与画质"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar2 as A2
from backend import avatar_lipsync as AL

root = A2._root()
mp3 = None
for c in sorted(glob.glob(os.path.join(root, "data", "**", "tts_*.mp3"), recursive=True)):
    if 150000 <= os.path.getsize(c) <= 160000:
        mp3 = c; break
mp3 = mp3 or [c for c in glob.glob(os.path.join(root, "data", "**", "tts_*.mp3"), recursive=True)
              if 150000 <= os.path.getsize(c) <= 500000][0]
wav = A2._to_wav16k(mp3)
sr, y = A2._read_wav(wav)
print("音频: %s  %.2fs" % (os.path.basename(mp3), len(y) / float(sr)))

img = A2._img_path()
out = os.path.join(root, "_t_out", "opt_1.0.mp4")
t0 = time.time()
r = AL.render(img, wav, out, blink=True, head=True, max_side=1024, motion=1.0)
dt = time.time() - t0
print("耗时 %.1fs  frames=%d  motion_sec=%.2f  (优化前 56.5s / 26.8s)"
      % (dt, r["frames"], r["motion_sec"]))
print("实时率: %.2fx" % (dt / (len(y) / float(sr))))

# 动作幅度是否还在
cap = cv2.VideoCapture(out)
gs = []
while True:
    ok, f = cap.read()
    if not ok: break
    gs.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
cap.release()
ref = gs[0].astype(np.float32)
h, w = ref.shape
win = cv2.createHanningWindow((w, h), cv2.CV_32F)
sh = []
for i in range(0, len(gs), 3):
    (dx, dy), _ = cv2.phaseCorrelate(ref, gs[i].astype(np.float32), win)
    sh.append((dx, dy))
a = np.array(sh)
print("头部位移 dx %+.1f~%+.1f  dy %+.1f~%+.1f  不同姿态=%d"
      % (a[:, 0].min(), a[:, 0].max(), a[:, 1].min(), a[:, 1].max(),
         len({(round(x, 1), round(y_, 1)) for x, y_ in sh})))
