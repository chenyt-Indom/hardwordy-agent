# -*- coding: utf-8 -*-
"""临时：用 TestClient 实测 3D 接口。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from backend.main import app

if __name__ == "__main__":
    c = TestClient(app)
    r = c.get("/api/avatar3d/info")
    print("info:", r.status_code, r.json())

    t0 = time.time()
    r = c.post("/api/avatar3d/speak", json={"text": "你好，我是三维数字人。", "amp": 1.4, "size": 384})
    print("speak:", r.status_code, "%.1fs" % (time.time() - t0))
    j = r.json()
    if r.status_code == 200:
        print({k: j.get(k) for k in ("ok", "name", "duration", "frames", "size", "lipsync_label", "align")})
    else:
        print(j)
