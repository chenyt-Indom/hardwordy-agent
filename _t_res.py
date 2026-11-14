import sys, os, time, json
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2
from backend import avatar_lipsync as AL
from backend import avatar2 as A2
from backend import tts as T

img = A2._img_path()
print("img:", img, os.path.getsize(img))

a = T.synth("大家好，今天我们来聊一聊数字人的口型效果。", voice="", rate=0,
            engine="auto", style="")
print("tts:", a.get("ok"), a.get("path"), a.get("engine"))
if not a.get("ok"):
    raise SystemExit("tts fail")
wav = A2._to_wav16k(a["path"])
print("wav:", wav)

out_dir = os.path.join(A2._root(), "data", "avatar2", "videos")
res = []
for ms in [768, 1024, 1280, 1536]:
    out = os.path.join(out_dir, "_res_%d.mp4" % ms)
    t0 = time.time()
    r = AL.render(img, wav, out, blink=False, head=False, max_side=ms)
    dt = time.time() - t0
    if not r.get("ok"):
        print(ms, "FAIL", r.get("error"))
        continue
    res.append({"ms": ms, "sec": round(dt, 2), "size": r["size"],
                "frames": r["frames"], "bytes": r["bytes"]})
    print(ms, r["size"], round(dt, 2), "s", r["bytes"])

print("RESULT", json.dumps(res, ensure_ascii=False))
