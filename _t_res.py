import sys, os, time, json
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np, cv2
from backend import avatar_lipsync as AL
from backend import avatar2 as A2
from backend import tts as T

img = A2._img_path()
a = T.synth("大家好，今天我们来聊一聊数字人的口型效果。", voice="", rate=0,
            engine="auto", style="")
wav = A2._to_wav16k(a["path"])

out_dir = os.path.join(A2._root(), "data", "avatar2", "videos")
res = []
for ms in [720, 1024, 1536, 1920]:
    out = os.path.join(out_dir, "_res_%d.mp4" % ms)
    t0 = time.time()
    r = AL.render(img, wav, out, blink=False, head=False, max_side=ms)
    dt = time.time() - t0
    if not r.get("ok"):
        print(ms, "FAIL", r.get("error")); continue
    res.append({"ms": ms, "sec": round(dt, 2), "size": r["size"],
                "frames": r["frames"], "bytes": r["bytes"]})
    print("request=%d -> size=%s  %.2fs  %dKB" % (ms, r["size"], dt, r["bytes"] // 1024))

print("RESULT", json.dumps(res, ensure_ascii=False))
