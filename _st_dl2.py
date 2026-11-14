"""SadTalker 核心模型下载（修正路径）"""
import urllib.request
import os
import threading
import time

BASE = r"D:\local-multimodal-src\data\models\sadtalker"
os.makedirs(BASE, exist_ok=True)
SEGDIR = os.path.join(BASE, "_seg2")
os.makedirs(SEGDIR, exist_ok=True)

VINTHONY = "https://hf-mirror.com/vinthony/SadTalker/resolve/main/"
RIPPER = "https://hf-mirror.com/rippertnt/wav2lip/resolve/main/"

FILES = [
    (RIPPER + "checkpoints/SadTalker_V0.0.2_256.safetensors", "SadTalker_V0.0.2_256.safetensors", 725070000),
    (VINTHONY + "auido2exp_00300-model.pth", "auido2exp_00300-model.pth", 34280000),
    (VINTHONY + "auido2pose_00140-model.pth", "auido2pose_00140-model.pth", 95920000),
    (VINTHONY + "mapping_00109-model.pth.tar", "mapping_00109-model.pth.tar", 155780000),
    (VINTHONY + "mapping_00229-model.pth.tar", "mapping_00229-model.pth.tar", 155520000),
    (VINTHONY + "epoch_20.pth", "epoch_20.pth", 288860000),
]
NTHREAD = 10


def dl_one(url, local, total):
    dst = os.path.join(BASE, local)
    if os.path.exists(dst) and os.path.getsize(dst) >= total * 0.98:
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
                h = {"User-Agent": "Mozilla/5.0",
                     "Range": "bytes=%d-%d" % (start + have, end)}
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
             (min((i + 1) * seg_size, total) - i * seg_size)
             for i in range(NTHREAD))
    if not ok:
        return False
    with open(dst + ".tmp", "wb") as out:
        for i in range(NTHREAD):
            with open(os.path.join(segdir, "s%02d" % i), "rb") as f:
                out.write(f.read())
    os.replace(dst + ".tmp", dst)
    return True


for url, local, total in FILES:
    ok = dl_one(url, local, total)
    open(os.path.join(BASE, "_log.txt"), "a").write("%s %s\n" % (local, "OK" if ok else "FAIL"))

open(os.path.join(BASE, "_all2.done"), "w").write("done")
