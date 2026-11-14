import sys, os
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2
from backend import avatar2 as A2
from backend.face_detect_scrfd import SCRFD

vd = os.path.join(A2._root(), "data", "avatar2", "videos")
det = SCRFD(os.path.join(A2._root(), "data", "models", "wav2lip", "scrfd_2.5g_bnkps.onnx"))

def grab(mp4, out_png):
    cap = cv2.VideoCapture(mp4)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n // 2))
    ok, fr = cap.read()
    cap.release()
    if not ok:
        print("fail", mp4); return
    h, w = fr.shape[:2]
    bx, sc = det.detect(fr)
    bx = np.asarray(bx)
    if bx.size:
        x1, y1, x2, y2 = [float(v) for v in bx[0][:4]]
        fw = x2 - x1
        cx1 = int(x1 + fw * 0.10); cx2 = int(x1 + fw * 0.90)
        cy1 = int(y1 + (y2 - y1) * 0.55); cy2 = int(y1 + (y2 - y1) * 0.98)
        crop = fr[max(0, cy1):min(h, cy2), max(0, cx1):min(w, cx2)]
        crop = cv2.resize(crop, (480, int(480 * crop.shape[0] / crop.shape[1])))
        cv2.imwrite(out_png, crop)
        print("%s frame=%dx%d face=%dx%d crop=%s" % (os.path.basename(mp4), w, h, int(fw), int(y2 - y1), crop.shape[:2]))
    else:
        print(os.path.basename(mp4), "no face")

for ms in [1024, 1920]:
    p = os.path.join(vd, "_res_%d.mp4" % ms)
    if os.path.isfile(p):
        grab(p, os.path.join(A2._root(), "_q_%d.png" % ms))
