"""下载 Wav2Lip-HD 的超分组件"""
import urllib.request
import os
import threading
import time

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
SEGDIR = os.path.join(BASE, "_seg_bf")
os.makedirs(SEGDIR, exist_ok=True)

REPO = "https://hf-mirror.com/commanderx/Wav2Lip-HD/resolve/main/"
FILES = [
    ("experiments/001_ESRGAN_x4_f64b23_custom16k_500k_B16G1_wandb/models/net_g_67500.pth",
     "4x_BigFace.pth", 66920000),
    ("checkpoints/face_segmentation.pth", "face_segmentation.pth", 53290000),
]
NTHREAD = 8


def dl(url, local, total):
    dst = os.path.join(BASE, local)
    if os.path.exists(dst) and os.path.getsize(dst) >= total * 0.95:
        print("skip", local)
        return True
    segdir = os.path.join(SEGDIR, local.replace(".", "_"))
    os.makedirs(segdir, exist_ok=True)
    seg_size = total // NTHREAD + 1

    def worker(i):
        start = i * seg_size
        end = min(start + seg_size - 1, total - 1)
        if start > total - 1:
            return
        sp = os.path.join(segdir, "s%02d" % i)
        need = end - start + 1
        for _ in range(15):
            have = os.path.getsize(sp) if os.path.exists(sp) else 0
            if have >= need:
                return
            try:
                h = {"User-Agent": "Mozilla/5.0", "Range": "bytes=%d-%d" % (start + have, end)}
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r, \
                        open(sp, "ab") as f:
                    while True:
                        c = r.read(1024 * 64)
                        if not c:
                            break
                        f.write(c)
                        if os.path.getsize(sp) >= need:
                            break
            except Exception:
                time.sleep(1.5)

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(NTHREAD)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    ok = all(os.path.exists(os.path.join(segdir, "s%02d" % i)) and
             os.path.getsize(os.path.join(segdir, "s%02d" % i)) >=
             (min((i + 1) * seg_size, total) - i * seg_size) for i in range(NTHREAD))
    if not ok:
        print("FAIL", local)
        return False
    with open(dst + ".tmp", "wb") as out:
        for i in range(NTHREAD):
            with open(os.path.join(segdir, "s%02d" % i), "rb") as f:
                out.write(f.read())
    os.replace(dst + ".tmp", dst)
    print("OK", local, "%.1f MB" % (os.path.getsize(dst) / 1e6))
    return True


for f, local, total in FILES:
    dl(REPO + f, local, total)
open(os.path.join(BASE, "_bf.done"), "w").write("done")
