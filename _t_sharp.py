import sys, os
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2
from backend import avatar2 as A2
from backend.face_detect_scrfd import SCRFD

im = cv2.imread(A2._img_path())
print("img size:", im.shape)

det = SCRFD(os.path.join(A2._root(), "data", "models", "wav2lip", "scrfd_2.5g_bnkps.onnx"))
boxes, scores = det.detect(im)
print("face box:", [round(float(v)) for v in boxes[0][:4]], "score:", round(float(scores[0]), 3))

vd = os.path.join(A2._root(), "data", "avatar2", "videos")

def mouth_crop(mp4):
    cap = cv2.VideoCapture(mp4)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n // 2))
    ok, fr = cap.read()
    cap.release()
    if not ok:
        return None, None, None
    bx, sc = det.detect(fr)
    if not bx:
        return fr, None, None
    x1, y1, x2, y2 = [float(v) for v in bx[0][:4]]
    w, h = x2 - x1, y2 - y1
    mx1 = int(x1 + w * 0.15); mx2 = int(x1 + w * 0.85)
    my1 = int(y1 + h * 0.62); my2 = int(y1 + h * 0.92)
    return fr, fr[my1:my2, mx1:mx2], (mx2 - mx1, my2 - my1)

for ms in [768, 1024]:
    p = os.path.join(vd, "_res_%d.mp4" % ms)
    if not os.path.isfile(p):
        print(ms, "no file"); continue
    fr, crop, sz = mouth_crop(p)
    if crop is None:
        print(ms, "crop fail"); continue
    k = 400.0 / crop.shape[1]
    norm = cv2.resize(crop, (400, max(2, int(crop.shape[0] * k))), interpolation=cv2.INTER_CUBIC)
    lap = cv2.Laplacian(cv2.cvtColor(norm, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    # 96x96 里嘴部约 40px -> 放大到实际嘴部宽度的倍数
    mag = sz[0] / 40.0
    print("max_side=%d mouth_px=%s mag=%.2fx sharp_norm=%.1f" % (ms, sz, mag, lap))
    cv2.imwrite(os.path.join(A2._root(), "_mouth_%d.png" % ms), norm)
