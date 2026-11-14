import sys, os
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2
from backend import avatar2 as A2
from backend.face_detect_scrfd import SCRFD

img_p = A2._img_path()
im = cv2.imread(img_p)
print("img size:", im.shape)

det = SCRFD(os.path.join(A2._root(), "data", "models", "wav2lip", "scrfd_2.5g_bnkps.onnx"))
r = det.detect(im)
print("detect type:", type(r), "len:", len(r) if r is not None else None)
if r:
    print("first elem len:", len(r[0]))
    b = r[0][0]
    print("box:", [round(float(v)) for v in b[:4]])
    print("score:", round(float(r[0][1]), 3))

vd = os.path.join(A2._root(), "data", "avatar2", "videos")

def mouth_crop(mp4):
    cap = cv2.VideoCapture(mp4)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n // 2))
    ok, fr = cap.read()
    cap.release()
    if not ok:
        return None, None, None
    d = det.detect(fr)
    if not d:
        return fr, None, None
    x1, y1, x2, y2 = [float(v) for v in d[0][0][:4]]
    w = x2 - x1
    h = y2 - y1
    mx1 = int(x1 + w * 0.15); mx2 = int(x1 + w * 0.85)
    my1 = int(y1 + h * 0.62); my2 = int(y1 + h * 0.92)
    crop = fr[my1:my2, mx1:mx2]
    return fr, crop, (mx2 - mx1, my2 - my1)

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
    print("max_side=%d mouth_px=%s scale96=%.2fx sharp=%.1f" % (
        ms, sz, 96.0 / max(1, min(sz[0], sz[1])) * 0 + (max(sz) / 96.0), lap))
    cv2.imwrite(os.path.join(A2._root(), "_mouth_%d.png" % ms), norm)
