# -*- coding: utf-8 -*-
"""端到端验证「割裂」修复：重新出片，分三区量化"""
import sys, os, glob, subprocess, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar_lipsync as AL, avatar2 as A2

OUT = "_qa_out"
os.makedirs(OUT, exist_ok=True)

# 1) 造测试语音
mp3 = os.path.join(OUT, "t.mp3")
TEXT = "这是修复后的效果，请看嘴部。"
if not os.path.isfile(mp3):
    async def gen():
        import edge_tts
        c = edge_tts.Communicate(TEXT, "zh-CN-XiaoxiaoNeural")
        await c.save(mp3)
    asyncio.run(gen())
print("语音 %.1fKB" % (os.path.getsize(mp3)/1024))
wav16 = os.path.join(OUT, "t.wav")
subprocess.run([A2.ffmpeg(), "-y", "-loglevel", "error", "-i", mp3,
                "-ar", "16000", "-ac", "1", wav16], check=False)
print("wav %.1fKB" % (os.path.getsize(wav16)/1024))

# 2) 出片
t0 = time.time()
res = AL.render("data/avatar2/avatar.png", wav16, os.path.join(OUT, "fixed.mp4"),
                blink=False, head=True, max_side=1024, motion=1.0)
print("render:", {k: res.get(k) for k in ("ok", "frames", "duration", "size", "motion", "error")})
print("耗时 %.1fs" % (time.time()-t0))
if not res.get("ok"):
    raise SystemExit("出片失败")

# 3) 抽帧
exe = A2.ffmpeg()
fd = os.path.join(OUT, "fr")
os.makedirs(fd, exist_ok=True)
for f in glob.glob(fd + "/*.png"):
    os.remove(f)
subprocess.run([exe, "-y", "-loglevel", "error", "-i", res["path"], "-vf", "fps=4", fd + "/f%03d.png"], check=False)
fs = sorted(glob.glob(fd + "/*.png"))
print("抽帧 %d" % len(fs))

img0 = cv2.imread("data/avatar2/avatar.png")
H, W = img0.shape[:2]

from backend.face_detect_scrfd import SCRFD
det = SCRFD(AL._paths()["det"])
dets, _ = det.detect(img0, thresh=0.5)
d = max(dets, key=lambda t: (t[2]-t[0])*(t[3]-t[1]))
bx = [max(0, int(d[0])), max(0, int(d[1])), min(W, int(d[2])), min(H, int(d[3]))]
bw, bh = bx[2]-bx[0], bx[3]-bx[1]
cx = bx[0] + int(bw*0.50); cy = bx[1] + int(bh*0.76)
ax = int(bw*0.31); ay = int(bh*0.22)
print("脸框 %s  椭圆中心(%d,%d) 半径(%d,%d)" % (bx, cx, cy, ax, ay))

# 三区：嘴核心（本来就该变）/ 椭圆内非嘴 / 椭圆外脸区
m_mouth = np.zeros((H, W), np.uint8)
cv2.ellipse(m_mouth, (cx, cy), (int(ax*0.62), int(ay*0.45)), 0, 0, 360, 1, -1)
m_ell = np.zeros((H, W), np.uint8)
cv2.ellipse(m_ell, (cx, cy), (ax, ay), 0, 0, 360, 1, -1)
m_rest = (m_ell > 0) & (m_mouth == 0)          # 椭圆内非嘴（不该变太多）
m_out = np.zeros((H, W), bool)
cv2.ellipse(m_out, (cx, cy), (int(ax*1.9), int(ay*1.9)), 0, 0, 360, 1, -1)
m_out = (m_out > 0) & (m_ell == 0)             # 椭圆外脸区

def stats(p, m):
    g = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    da = np.abs(p[m].astype(np.float32) - img0[m].astype(np.float32)).mean()
    return float(lap[m].var()), float(da)

rows = []
for f in fs[::max(1, len(fs)//8)][:8]:
    p = cv2.imread(f)
    if p is None:
        continue
    if p.shape[:2] != (H, W):
        p = cv2.resize(p, (W, H), interpolation=cv2.INTER_AREA)
    sm, dm = stats(p, m_mouth)
    sr, dr = stats(p, m_rest)
    so, do = stats(p, m_out)
    rows.append((os.path.basename(f), dm, dr, do, sr, so))
    print("  %s  色差 嘴%.1f 椭圆内%.1f 椭圆外%.1f | 清晰度 椭圆内%.0f 外%.0f" % rows[-1])

if rows:
    print("\n=== 汇总（平均） ===")
    print("  嘴核心色差   %.2f  （该变，越大越说明嘴真在动）" % np.mean([r[1] for r in rows]))
    print("  椭圆内非嘴   %.2f  （不该太大 → 补丁感）" % np.mean([r[2] for r in rows]))
    print("  椭圆外脸区   %.2f" % np.mean([r[3] for r in rows]))
    print("  清晰度比 内/外 = %.2f （1.0 最好）" % (np.mean([r[4] for r in rows])/max(1e-6, np.mean([r[5] for r in rows]))))

# 4) ROI 边界：背景是否被动
from backend import avatar25d as A25
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
x0, y0, x1, y1 = prep["roi"]
print("\nROI %s" % (prep["roi"],))
for yaw in (4.0, 7.5):
    out = A25._warp(img0, prep, yaw=yaw, iters=1)
    diff = np.abs(out.astype(np.int16) - img0.astype(np.int16)).max(axis=2)
    wg = prep["wgt"]
    inside = np.zeros((H, W), bool); inside[y0:y1, x0:x1] = True
    bg = (wg < 0.02) & inside
    print("  yaw=%.1f°  背景(权重<0.02,ROI内) 改动像素 %.1f%%  最大 %d" % (
        yaw, 100.0*(diff[bg] > 0).mean() if bg.any() else 0, diff[bg].max() if bg.any() else 0))
