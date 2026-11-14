"""下载 wav2lip.pth（口型比 gan 版更准）"""
import urllib.request
import os
import threading
import time

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
URL = "https://hf-mirror.com/rippertnt/wav2lip/resolve/main/checkpoints/wav2lip.pth"
TOTAL = 435805000
DST = os.path.join(BASE, "wav2lip.pth")
SEGDIR = os.path.join(BASE, "_seg_w2l")
os.makedirs(SEGDIR, exist_ok=True)
NTHREAD = 12
seg_size = TOTAL // NTHREAD + 1


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
            with urllib.request.urlopen(urllib.request.Request(URL, headers=h), timeout=60) as r, \
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

ok = all(os.path.exists(os.path.join(SEGDIR, "s%02d" % i)) and
         os.path.getsize(os.path.join(SEGDIR, "s%02d" % i)) >=
         (min((i + 1) * seg_size, TOTAL) - i * seg_size) for i in range(NTHREAD))
if ok:
    with open(DST + ".tmp", "wb") as out:
        for i in range(NTHREAD):
            with open(os.path.join(SEGDIR, "s%02d" % i), "rb") as f:
                out.write(f.read())
    os.replace(DST + ".tmp", DST)
    open(os.path.join(BASE, "_w2l_done.txt"), "w").write("OK %d" % os.path.getsize(DST))
else:
    open(os.path.join(BASE, "_w2l_done.txt"), "w").write("FAIL")
