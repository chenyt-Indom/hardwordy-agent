# -*- coding: utf-8 -*-
"""用 TestClient 走新代码，真实跑一次 3D 生成（不碰正在运行的 8000 服务）。"""
import json
import time
import traceback

try:
    from fastapi.testclient import TestClient
    from backend.main import app

    c = TestClient(app)
    r = c.get("/api/avatar3d/info")
    print("info:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])

    t0 = time.time()
    r = c.post("/api/avatar3d/speak", json={"text": "你好，这是三D验证", "amp": 1.2})
    dt = time.time() - t0
    print("speak:", r.status_code, "%.1fs" % dt)
    body = r.json()
    print(json.dumps(body, ensure_ascii=False)[:600])
except Exception:
    traceback.print_exc()
