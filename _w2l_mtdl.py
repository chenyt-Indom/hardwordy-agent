"""16 线程分段下载 wav2lip_gan.pth，支持断点续传"""
import urllib.request
import os
import sys
import threading
import time
import json

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
URL = "https://hf-mirror.com/numz/wav2lip_studio/resolve/main/Wav2lip/wav2lip_gan.pth"
TOTAL = 435801865
DEST = os.path.join(BASE, "wav2lip_gan.pth")
SEGDIR = os.path.join(BASE, "_seg")
NTHREAD = 16
CHUNK = 1024 * 128

os.makedirs(SEGDIR, exist_ok=True)
meta = os.path.join(SEGDIR, "meta.json")
if not os.path.exists(meta):
    json.dump({"total": TOTAL, "n": NTHREAD}, open(meta, "w"))

seg_size = TOTAL // NTHREAD + 1
lock = threading.Lock()
progress = {"bytes": 0}


def seg_path(i):
    return os.path.join(SEGDIR, "seg_%02d.bin" % i)


def worker(i):
    start = i * seg_size
    end = min(start + seg_size - 1, TOTAL - 1)
    if start > TOTAL - 1:
        return
    sp = seg_path(i)
    # 已完整则跳过
    if os.path.exists(sp) and os.path.getsize(sp) >= (end - start + 1):
        with lock:
            progress["bytes"] += os.path.getsize(sp)
        return
    for attempt in range(6):
        try:
            have = os.path.getsize(sp) if os.path.exists(sp) else 0
            if have >= (end - start + 1):
                with lock:
                    progress["bytes"] += have
                return
            h = {"User-Agent": "Mozilla/5.0",
                 "Range": "bytes=%d-%d" % (start + have, end)}
            with urllib.request.urlopen(urllib.request.Request(URL, headers=h), timeout=120) as r, \
                    open(sp, "ab" if have else "wb") as f:
                while True:
                    c = r.read(CHUNK)
                    if not c:
                        break
                    f.write(c)
                    with lock:
                        progress["bytes"] += len(c)
            if os.path.getsize(sp) >= (end - start + 1):
                return
        except Exception as e:
            time.sleep(2)
    # 记失败
    open(os.path.join(SEGDIR, "fail_%02d" % i), "w").write("fail")


threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(NTHREAD)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# 合并
ok = all(os.path.exists(seg_path(i)) and
         os.path.getsize(seg_path(i)) >= (min((i + 1) * seg_size, TOTAL) - i * seg_size)
         for i in range(NTHREAD))
if ok:
    with open(DEST + ".tmp", "wb") as out:
        for i in range(NTHREAD):
            with open(seg_path(i), "rb") as f:
                out.write(f.read())
    sz = os.path.getsize(DEST + ".tmp")
    if sz == TOTAL:
        os.replace(DEST + ".tmp", DEST)
        open(os.path.join(BASE, "_dl.done"), "w").write("OK %d" % sz)
    else:
        open(os.path.join(BASE, "_dl.done"), "w").write("SIZE %d != %d" % (sz, TOTAL))
else:
    open(os.path.join(BASE, "_dl.done"), "w").write("INCOMPLETE")
