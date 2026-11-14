"""SadTalker 模型多线程下载器"""
import urllib.request
import os
import sys
import threading
import time
import json

BASE = r"D:\local-multimodal-src\data\models\sadtalker"
os.makedirs(BASE, exist_ok=True)
SEGDIR = os.path.join(BASE, "_seg")
os.makedirs(SEGDIR, exist_ok=True)

REPO = "https://hf-mirror.com/vinthony/SadTalker/resolve/main/"

# (远端路径, 本地相对路径, 大小)
FILES = [
    ("checkpoints/auido2exp_00300-model.pth", "auido2exp_00300-model.pth", 34280000),
    ("checkpoints/auido2pose_00140-model.pth", "auido2pose_00140-model.pth", 95920000),
    ("checkpoints/mapping_00109-model.pth.tar", "mapping_00109-model.pth.tar", 155780000),
    ("checkpoints/mapping_00229-model.pth.tar", "mapping_00229-model.pth.tar", 155520000),
    ("checkpoints/SadTalker_V0.0.2_256.safetensors", "SadTalker_V0.0.2_256.safetensors", 725070000),
    ("checkpoints/epoch_20.pth", "epoch_20.pth", 288860000),
]
NTHREAD = 12


def dl_one(remote, local, total):
    dst = os.path.join(BASE, local)
    if os.path.exists(dst) and os.path.getsize(dst) >= total * 0.98:
        print("[skip]", local)
        return True
    segdir = os.path.join(SEGDIR, local.replace("/", "_").replace(".", "_"))
    os.makedirs(segdir, exist_ok=True)
    seg_size = total // NTHREAD + 1
    url = REPO + remote
    lock = threading.Lock()

    def worker(i):
        start = i * seg_size
        end = min(start + seg_size - 1, total - 1)
        if start > total - 1:
            return
        sp = os.path.join(segdir, "s%02d" % i)
        need = end - start + 1
        for attempt in range(10):
            have = os.path.getsize(sp) if os.path.exists(sp) else 0
            if have >= need:
                return
            try:
                h = {"User-Agent": "Mozilla/5.0",
                     "Range": "bytes=%d-%d" % (start + have, end)}
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90) as r, \
                        open(sp, "ab") as f:
                    while True:
                        c = r.read(1024 * 64)
                        if not c:
                            break
                        f.write(c)
                        if os.path.getsize(sp) >= need:
                            break
            except Exception:
                time.sleep(2)
        return

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(NTHREAD)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    # 合并
    ok = True
    for i in range(NTHREAD):
        s = i * seg_size
        e = min(s + seg_size - 1, total - 1)
        sp = os.path.join(segdir, "s%02d" % i)
        if not os.path.exists(sp) or os.path.getsize(sp) < (e - s + 1):
            ok = False
    if not ok:
        print("[fail]", local)
        return False
    with open(dst + ".tmp", "wb") as out:
        for i in range(NTHREAD):
            with open(os.path.join(segdir, "s%02d" % i), "rb") as f:
                out.write(f.read())
    sz = os.path.getsize(dst + ".tmp")
    os.replace(dst + ".tmp", dst)
    print("[ok]", local, "%.1f MB" % (sz / 1e6))
    return True


for remote, local, total in FILES:
    dl_one(remote, local, total)

open(os.path.join(BASE, "_all.done"), "w").write("done")
print("ALL DONE")
