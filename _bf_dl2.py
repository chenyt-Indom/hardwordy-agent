import urllib.request
import os
import threading
import time

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
URL = "https://hf-mirror.com/commanderx/Wav2Lip-HD/resolve/main/experiments/001_ESRGAN_x4_f64b23_custom16k_500k_B16G1_wandb/models/net_g_67500.pth"
TOTAL = 66919172
DST = os.path.join(BASE, "4x_BigFace.pth")
SEGDIR = os.path.join(BASE, "_seg_bf3")
os.makedirs(SEGDIR, exist_ok=True)
N = 8
seg_size = TOTAL // N + 1


def worker(i):
    start = i * seg_size
    end = min(start + seg_size - 1, TOTAL - 1)
    if start > TOTAL - 1:
        return
    sp = os.path.join(SEGDIR, "s%02d" % i)
    need = end - start + 1
    for _ in range(15):
        have = os.path.getsize(sp) if os.path.exists(sp) else 0
        if have >= need:
            return
        try:
            h = {"User-Agent": "Mozilla/5.0", "Range": "bytes=%d-%d" % (start + have, end)}
            with urllib.request.urlopen(urllib.request.Request(URL, headers=h), timeout=90) as r, \
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


ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(N)]
for t in ths:
    t.start()
for t in ths:
    t.join()

ok = all(os.path.exists(os.path.join(SEGDIR, "s%02d" % i)) and
         os.path.getsize(os.path.join(SEGDIR, "s%02d" % i)) >=
         (min((i + 1) * seg_size, TOTAL) - i * seg_size) for i in range(N))
if ok:
    with open(DST + ".tmp", "wb") as out:
        for i in range(N):
            with open(os.path.join(SEGDIR, "s%02d" % i), "rb") as f:
                out.write(f.read())
    sz = os.path.getsize(DST + ".tmp")
    if sz == TOTAL:
        os.replace(DST + ".tmp", DST)
        open(os.path.join(BASE, "_bf_ok.txt"), "w").write("OK %d" % sz)
    else:
        open(os.path.join(BASE, "_bf_ok.txt"), "w").write("SIZE %d" % sz)
else:
    open(os.path.join(BASE, "_bf_ok.txt"), "w").write("INCOMPLETE")
