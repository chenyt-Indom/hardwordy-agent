# -*- coding: utf-8 -*-
"""只出片，逐步打印耗时"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
print("import 完成", flush=True)
from backend import avatar_lipsync as AL, avatar2 as A2, wav2lip_engine as W2L
print("模块加载完成", flush=True)

wav = "_qa_out/s.wav"
sr, y = A2._read_wav(wav)
print("音频 %.2fs" % (len(y)/sr), flush=True)

img = cv2.imread("data/avatar2/avatar.png")
H0, W0 = img.shape[:2]
print("原图 %dx%d" % (W0, H0), flush=True)
t = time.time()
eng = AL._engine()
print("引擎 %.1fs" % (time.time()-t), flush=True)

t = time.time()
frames, meta = eng.render(img, y.astype(np.float32))
print("Wav2Lip %d 帧 %.1fs" % (len(frames), time.time()-t), flush=True)

from backend import avatar25d as A25
t = time.time()
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
print("3DMM 拟合 %.1fs" % (time.time()-t), flush=True)

poses = A25._pose_curve(len(frames), W2L.FPS, AL._energy_curve(y, sr, len(frames), W2L.FPS))
t = time.time()
out = []
for i, fr in enumerate(frames):
    yaw, pit, rol, tx, ty, scl, bl = poses[i]
    out.append(A25._warp(fr, prep, yaw=max(-7.5, min(7.5, yaw*1.3)),
                         pitch=pit*2.0, roll=max(-5, min(5, rol*1.3)),
                         tx=tx*1.3, ty=ty*1.3, scl=scl, iters=1))
print("动作层 %d 帧 %.1fs" % (len(out), time.time()-t), flush=True)

# ---- 三区量化 ----
from backend.face_detect_scrfd import SCRFD
det = SCRFD(AL._paths()["det"])
dets, _ = det.detect(img, thresh=0.5)
d = max(dets, key=lambda t: (t[2]-t[0])*(t[3]-t[1]))
bx = [max(0, int(d[0])), max(0, int(d[1])), min(W0, int(d[2])), min(H0, int(d[3]))]
bw, bh = bx[2]-bx[0], bx[3]-bx[1]
ccx = bx[0] + int(bw*0.50); ccy = bx[1] + int(bh*0.76)
aax = int(bw*0.31); aay = int(bh*0.22)
print("脸框 %s 椭圆中心(%d,%d) 半径(%d,%d)" % (bx, ccx, ccy, aax, aay), flush=True)

m_mouth = np.zeros((H0, W0), np.uint8)
cv2.ellipse(m_mouth, (ccx, ccy), (int(aax*0.60), int(aay*0.42)), 0, 0, 360, 1, -1)
m_ell = np.zeros((H0, W0), np.uint8)
cv2.ellipse(m_ell, (ccx, ccy), (aax, aay), 0, 0, 360, 1, -1)
m_rest = (m_ell > 0) & (m_mouth == 0)
m_out = np.zeros((H0, W0), np.uint8)
cv2.ellipse(m_out, (ccx, ccy), (int(aax*1.9), int(aay*1.9)), 0, 0, 360, 1, -1)
m_out = (m_out > 0) & (m_ell == 0)

def st(p, m):
    g = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    return float(lap[m].var()), float(np.abs(p[m].astype(np.float32) - img[m].astype(np.float32)).mean())

print("\n=== 逐帧（色差 / 锐度） ===", flush=True)
rows = []
for i in range(0, len(out), max(1, len(out)//8)):
    p = out[i]
    sm, dm = st(p, m_mouth)
    sr, dr = st(p, m_rest)
    so, do = st(p, m_out)
    rows.append((dm, dr, do, sr, so))
    print("  f%02d  色差 嘴%.1f 椭内%.1f 椭外%.1f | 锐度 内%.0f 外%.0f" % (i, dm, dr, do, sr, so), flush=True)

print("\n=== 汇总 ===", flush=True)
print("  嘴核心色差   %.2f  （该变）" % np.mean([r[0] for r in rows]))
print("  椭圆内非嘴   %.2f  （补丁感指标，越小越好）" % np.mean([r[1] for r in rows]))
print("  椭圆外脸区   %.2f" % np.mean([r[2] for r in rows]))
print("  清晰度比 内/外 = %.2f （1.0 最好）" % (np.mean([r[3] for r in rows])/max(1e-6, np.mean([r[4] for r in rows]))))

# ---- ROI 背景位移 ----
x0, y0, x1, y1 = prep["roi"]
print("\nROI %s" % (prep["roi"],), flush=True)
for yw in (4.0, 7.5):
    o = A25._warp(img, prep, yaw=yw, iters=1)
    diff = np.abs(o.astype(np.int16) - img.astype(np.int16)).max(axis=2)
    wg = prep["wgt"]
    ins = np.zeros((H0, W0), bool); ins[y0:y1, x0:x1] = True
    bg = (wg < 0.02) & ins
    print("  yaw=%.1f° 背景(权重<0.02,ROI内) 改动 %.1f%% 最大 %d" % (
        yw, 100.0*(diff[bg] > 0).mean() if bg.any() else 0, diff[bg].max() if bg.any() else 0), flush=True)

cv2.imwrite("_qa_out/before_after.png", np.vstack([
    cv2.resize(img, (512, 512)),
    cv2.resize(out[len(out)//3], (512, 512))]))
print("已存对比图 _qa_out/before_after.png", flush=True)
