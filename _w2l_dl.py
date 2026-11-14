import urllib.request, os, sys

base = r"D:\local-multimodal-src\data\models\wav2lip"
dst = os.path.join(base, "wav2lip_gan.pth")
tmp = dst + ".part"
done = os.path.join(base, "_dl.done")
url = "https://hf-mirror.com/numz/wav2lip_studio/resolve/main/Wav2lip/wav2lip_gan.pth"
TOTAL = 435_800_000

try:
    got = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    h = {"User-Agent": "Mozilla/5.0"}
    if got:
        h["Range"] = "bytes=%d-" % got
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=300) as r, \
         open(tmp, "ab" if got else "wb") as f:
        while True:
            c = r.read(1024 * 512)
            if not c:
                break
            f.write(c)
    sz = os.path.getsize(tmp)
    if sz > TOTAL * 0.95:
        os.replace(tmp, dst)
        open(done, "w").write("OK %d" % sz)
    else:
        open(done, "w").write("PARTIAL %d" % sz)
except Exception as e:
    open(done, "w").write("FAIL %s: %s" % (type(e).__name__, e))
