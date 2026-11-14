import sys, os, json, time
sys.path.insert(0, r"D:\local-multimodal-src")
from fastapi.testclient import TestClient
from backend.main import app

c = TestClient(app)

r = c.get("/api/avatar_lipsync/info")
print("[info]", r.status_code, {k: r.json().get(k) for k in ("available", "engine", "fps")})

for ms in [720, 1024, 9999]:
    t0 = time.time()
    r = c.post("/api/avatar_lipsync/speak", json={
        "text": "你好，这是一次分辨率测试。",
        "max_side": ms,
        "blink": False, "head": False,
    })
    dt = time.time() - t0
    if r.status_code != 200:
        print("[speak %s] FAIL %s %s" % (ms, r.status_code, str(r.text)[:200]))
        continue
    d = r.json()
    print("[speak %s] -> size=%s max_side=%s dur=%s frames=%s %.1fs" % (
        ms, d.get("size"), d.get("max_side"), d.get("duration"),
        d.get("frames"), dt))
