"""单眼特写 + 闭合度量化：判断眨眼到底是"位置错"还是"糊"。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

from backend import avatar25d as A25
from backend import avatar2 as A2

prep = A25._prep3d(A2._img_path(), max_side=1024)
W, H = prep["wh"]
frame = prep["img"].copy()
rows = []
for a in (0.0, 0.4, 0.7, 1.0):
    rows.append(A25._blink(frame.copy(), prep, a, 0.0, 0.0, 0.0, 0.0))

gap = prep["eye_gap"]
ew = max(6.0, gap * 0.17)
eh = max(5.0, gap * 0.085)
print(f"眼距={gap:.1f} 眼裂半宽 ew={ew:.1f} 半高 eh={eh:.1f}")

tiles = []
for i, (ex, ey) in enumerate(prep["eyes"]):
    x0 = int(max(0, ex - ew * 2.2)); x1 = int(min(W, ex + ew * 2.2))
    y0 = int(max(0, ey - eh * 3.8)); y1 = int(min(H, ey + eh * 2.0))
    strip = []
    for k, a in enumerate((0.0, 0.4, 0.7, 1.0)):
        crop = rows[k][y0:y1, x0:x1]
        strip.append(cv2.resize(crop, None, fx=3.0, fy=3.0,
                                interpolation=cv2.INTER_NEAREST))
    sep = np.full((strip[0].shape[0], 5, 3), 255, np.uint8)
    line = np.hstack([x for s in strip for x in (s, sep)][:-1])
    tiles.append(line)
    # 量化：闭眼后眼裂中心带的颜色方差（糊 = 方差低）与梯度（硬边 = 梯度尖峰）
    for k, a in enumerate((0.0, 0.4, 0.7, 1.0)):
        c = rows[k][int(ey - eh * 0.8):int(ey + eh * 0.8),
                    int(ex - ew * 0.9):int(ex + ew * 0.9)].astype(np.float32)
        if c.size == 0:
            continue
        g = cv2.cvtColor(c.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
        gy = np.abs(np.diff(g, axis=0))
        print(f"  眼{i} a={a:.1f}  亮度std={g.std():5.1f}  纵向梯度max={gy.max():5.1f} p99={np.percentile(gy,99):5.1f}")
    print()

out = np.vstack([t for t in tiles])
cv2.imwrite(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_blink_eyes.png"),
            cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
print("图: _blink_eyes.png（上=左眼 下=右眼；每行 4 档：睁 / 0.4 / 0.7 / 全闭，3 倍放大）")
