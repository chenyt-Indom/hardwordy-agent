import sys, os, json, time
sys.path.insert(0, r"D:\local-multimodal-src")
os.chdir(r"D:\local-multimodal-src")
from backend import avatar2, wav2lip_onnx as w2l, tts
import numpy as np, cv2

res = {}
try:
    a = tts.synth("大家好，这是口型幅度测试，一二三四五六七八。", engine="edge")
    wav = avatar2._to_wav16k(a["path"])
    res["tts"] = a.get("ok")
    outs = {}
    for b in (1.0, 1.6):
        out = r"D:\local-multimodal-src\data\avatar2\videos\_boost_%s.mp4" % str(b).replace(".", "_")
        t0 = time.time()
        r = w2l.render(avatar2._img_path(), wav, out, boost=b)
        outs[str(b)] = {"ok": r.get("ok"), "frames": r.get("frames"),
                        "err": r.get("error", ""), "sec": round(time.time() - t0, 1),
                        "path": out if r.get("ok") else ""}
    res["runs"] = outs
    # 测每个视频的唇缝范围
    for b, info in outs.items():
        if not info["ok"]:
            continue
        cap = cv2.VideoCapture(info["path"])
        frames = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
        cap.release()
        H, W = frames[0].shape[:2]
        box = w2l.detect_face_box(frames[0])
        x1, y1, x2, y2 = w2l.crop_rect(box, W, H)
        ch = y2 - y1
        mr = (avatar2.mouth()["y"] * H - y1) / float(ch)
        gs = []
        for f in frames:
            g = cv2.cvtColor(f[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).astype(np.float32)
            c = int(mr * ch)
            prof = g[max(0, c - 20):min(ch, c + 20)].mean(axis=1)
            mn = float(prof.min())
            best = cur = 0
            for v in (prof < mn + 12):
                cur = cur + 1 if v else 0
                best = max(best, cur)
            gs.append(best)
        info["gap_min"] = int(min(gs))
        info["gap_max"] = int(max(gs))
        info["gap_mean"] = round(float(np.mean(gs)), 1)
        info["open_frames"] = int(sum(1 for v in gs if v >= max(gs) * 0.7))
        info["total"] = len(gs)
except Exception as e:
    res["error"] = "%s: %s" % (type(e).__name__, e)

with open(r"D:\local-multimodal-src\_boost_result.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("DONE")
