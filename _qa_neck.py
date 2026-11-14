"""定量找割裂：时间方差图的阶跃 = 运动不一致的硬边。"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
cap = cv2.VideoCapture(vs[0])
g = []
while True:
    ok, f = cap.read()
    if not ok: break
    g.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32))
cap.release()
V = np.stack(g)                      # (T,H,W)
print("视频", V.shape)
var = V.var(axis=0)                  # 每像素时间方差
print(f"时间方差: mean={var.mean():.2f} p50={np.percentile(var,50):.2f} "
      f"p99={np.percentile(var,99):.2f} max={var.max():.1f}")

# 阶跃检测：对时间方差图求梯度，找异常大的边（=运动边界突变）
sm = cv2.GaussianBlur(var, (0, 0), 2.0)
gx = np.abs(cv2.Sobel(sm, cv2.CV_32F, 1, 0, ksize=5))
gy = np.abs(cv2.Sobel(sm, cv2.CV_32F, 0, 1, ksize=5))
gm = np.hypot(gx, gy)
print(f"方差梯度: p99={np.percentile(gm,99):.2f} max={gm.max():.1f}")

# 找梯度最强的若干点（这些就是最可能的割裂线）
thr = np.percentile(gm, 99.9)
ys, xs = np.where(gm > thr)
print(f"\n梯度 > p99.9({thr:.2f}) 的像素: {len(ys)} 个")
if len(ys):
    # 按行聚类，看是不是成线
    rows = np.bincount(ys, minlength=V.shape[1])
    top = np.argsort(rows)[-8:][::-1]
    print("最集中的行:", [(int(r), int(rows[r])) for r in top if rows[r] > 0])
    cols = np.bincount(xs, minlength=V.shape[2])
    topc = np.argsort(cols)[-8:][::-1]
    print("最集中的列:", [(int(c), int(cols[c])) for c in topc if cols[c] > 0])

# 分区域统计时间方差（看运动是否分区跳变）
H, W = V.shape[1:]
regions = {
    "背景左上": (20, 200, 20, 200), "背景右上": (20, 200, W-200, W-20),
    "头发": (30, 180, int(W*0.35), int(W*0.65)),
    "额头": (250, 330, int(W*0.38), int(W*0.62)),
    "眼睛": (380, 440, int(W*0.38), int(W*0.62)),
    "嘴": (int(H*0.55), int(H*0.63), int(W*0.40), int(W*0.60)),
    "下巴": (int(H*0.64), int(H*0.68), int(W*0.42), int(W*0.58)),
    "脖子": (int(H*0.70), int(H*0.76), int(W*0.44), int(W*0.56)),
    "衣领": (int(H*0.80), int(H*0.86), int(W*0.40), int(W*0.60)),
    "衣服": (int(H*0.90), int(H*0.97), int(W*0.40), int(W*0.60)),
}
print("\n各区域时间方差（运动量）：")
for nm, (y0, y1, x0, x1) in regions.items():
    v = var[y0:y1, x0:x1]
    print(f"  {nm:8s} mean={v.mean():7.3f}  p99={np.percentile(v,99):7.3f}")

# 存方差热力图
hm = np.clip(var / max(1e-6, np.percentile(var, 99.5)) * 255, 0, 255).astype(np.uint8)
hm = cv2.applyColorMap(hm, cv2.COLORMAP_JET)
cv2.imwrite(r"D:\local-multimodal-src\_qa_out\variance_heat.png", hm)
print("\n热力图: _qa_out/variance_heat.png")
