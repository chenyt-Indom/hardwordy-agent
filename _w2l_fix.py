import urllib.request
import os
import time

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
URL = "https://hf-mirror.com/numz/wav2lip_studio/resolve/main/Wav2lip/wav2lip_gan.pth"
TOTAL = 435801865
N = 16
seg_size = TOTAL // N + 1
SEGDIR = os.path.join(BASE, "_seg")
DEST = os.path.join(BASE, "wav2lip_gan.pth")

i = 11
start = i * seg_size
end = min(start + seg_size - 1, TOTAL - 1)
need = end - start + 1
sp = os.path.join(SEGDIR, "seg_%02d.bin" % i)

for attempt in range(12):
    have = os.path.getsize(sp) if os.path.exists(sp) else 0
    if have >= need:
        print("seg_11 complete", have)
        break
    try:
        h = {"User-Agent": "Mozilla/5.0",
             "Range": "bytes=%d-%d" % (start + have, end)}
        with urllib.request.urlopen(urllib.request.Request(URL, headers=h), timeout=60) as r, \
                open(sp, "ab") as f:
            while True:
                c = r.read(1024 * 64)
                if not c:
                    break
                f.write(c)
                if os.path.getsize(sp) >= need:
                    break
    except Exception as e:
        print("attempt", attempt, "err", type(e).__name__, str(e)[:60])
        time.sleep(2)
    have = os.path.getsize(sp) if os.path.exists(sp) else 0
    print("  have %.1f / %.1f MB" % (have / 1e6, need / 1e6))

# 合并
allok = True
for j in range(N):
    s = j * seg_size
    e = min(s + seg_size - 1, TOTAL - 1)
    needj = e - s + 1
    p = os.path.join(SEGDIR, "seg_%02d.bin" % j)
    if not os.path.exists(p) or os.path.getsize(p) < needj:
        allok = False
        print("INCOMPLETE seg", j, os.path.getsize(p) if os.path.exists(p) else 0, needj)

if allok:
    with open(DEST + ".tmp", "wb") as out:
        for j in range(N):
            with open(os.path.join(SEGDIR, "seg_%02d.bin" % j), "rb") as f:
                out.write(f.read())
    sz = os.path.getsize(DEST + ".tmp")
    if sz == TOTAL:
        os.replace(DEST + ".tmp", DEST)
        open(os.path.join(BASE, "_dl.done"), "w").write("OK %d" % sz)
        print("MERGED OK", sz)
    else:
        print("SIZE MISMATCH", sz, TOTAL)
else:
    print("not merged")
