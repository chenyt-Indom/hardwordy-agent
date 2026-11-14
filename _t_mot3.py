"""用较长音频测动作幅度，并输出对比帧"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import cv2
from backend import avatar2 as A2
from backend import avatar_lipsync as AL

root = A2._root()
# 找 5~15 秒的 tts
cands = []
for c in glob.glob(os.path.join(root, "data", "**", "tts_*.mp3"), recursive=True):
    sz = os.path.getsize(c)
    if 150000 <= sz <= 500000:
        cands.append(c)
cands.sort(key=lambda p: os.path.getsize(p))
print("候选:", [(os.path.basename(c), os.path.getsize(c)) for c in cands[:4]])
mp3 = cands[0]
wav = A2._to_wav16k(mp3)
sr, y = A2._read_wav(wav)
print("音频时长: %.2fs" % (len(y) / float(sr)))

img = A2._img_path()
os.makedirs("_t_out", exist_ok=True)
paths = {}
for lvl in (1.0, 1.6):
    out = os.path.join(root, "_t_out", "mot2_%.1f.mp4" % lvl)
    t0 = time.time()
    r = AL.render(img, wav, out, blink=True, head=True, max_side=1024, motion=lvl)
    if not r.get("ok"):
        print("motion=%.1f FAIL: %s" % (lvl, r.get("error"))); continue
    print("motion=%.1f  %5.1fs  frames=%d  mode=%s  motion_sec=%.2f"
          % (lvl, time.time() - t0, r["frames"], r["motion"], r["motion_sec"]))
    paths[lvl] = out

def analyze(path):
    cap = cv2.VideoCapture(path)
    gs = []
    while True:
        ok, f = cap.read()
        if not ok: break
        gs.append(f)
    cap.release()
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in gs]
    ref = gray[0].astype(np.float32)
    h, w = ref.shape
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    sh = []
    for i in range(0, len(gray), 3):
        (dx, dy), _ = cv2.phaseCorrelate(ref, gray[i].astype(np.float32), win)
        sh.append((dx, dy))
    a = np.array(sh)
    # 帧间差（动态感）
    diffs = [np.abs(gray[i].astype(np.int16) - gray[i-1].astype(np.int16)).mean()
             for i in range(1, len(gray))]
    return {"n": len(gs), "dx": (a[:, 0].min(), a[:, 0].max()),
            "dy": (a[:, 1].min(), a[:, 1].max()),
            "uniq": len({(round(x, 1), round(y_, 1)) for x, y_ in sh}),
            "inter": float(np.mean(diffs)) if diffs else 0.0,
            "frames": gs}

for lvl, p in paths.items():
    s = analyze(p)
    print("motion=%.1f  frames=%d  dx %+.1f~%+.1f  dy %+.1f~%+.1f  不同姿态=%d  帧间差=%.2f"
          % (lvl, s["n"], s["dx"][0], s["dx"][1], s["dy"][0], s["dy"][1], s["uniq"], s["inter"]))
    if lvl == 1.0:
        fr = s["frames"]
        idx = [0, len(fr)//4, len(fr)//2, 3*len(fr)//4, len(fr)-1]
        row = []
        for i in idx:
            im = fr[i].copy()
            cv2.putText(im, "#%d" % i, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            row.append(im)
        grid = np.hstack(row)
        grid = cv2.resize(grid, (grid.shape[1]//3, grid.shape[0]//3))
        cv2.imwrite(os.path.join(root, "_t_out", "motion_seq.png"), grid)
        print("序列图:", grid.shape)
