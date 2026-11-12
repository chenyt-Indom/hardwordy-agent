# -*- coding: utf-8 -*-
"""临时自测 3：用 TestClient 真发 HTTP 请求打 /api/tts/*。跑完即删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import app  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception as e:
    print("TestClient 不可用:", e)
    raise SystemExit(0)

c = TestClient(app)

print("=== 1. GET /api/tts/voices ===")
r = c.get("/api/tts/voices")
j = r.json()
print("status:", r.status_code, "| 音色数:", len(j.get("voices", [])), "| 默认:", j.get("default"))
print("可用性:", [(v["name"], v["available"]) for v in j.get("voices", [])[:3]])

print("=== 2. POST /api/tts/synth（在线）===")
r = c.post("/api/tts/synth", json={"text": "接口自测：这一句用来验证语音合成。",
                                  "voice": "zh-CN-XiaoxiaoNeural", "rate": 5})
d = r.json()
print("status:", r.status_code, "| ok:", d.get("ok"), "| engine:", d.get("engine"),
      "| bytes:", d.get("bytes"), "| url:", d.get("url"), "| warn:", d.get("warnings"))

print("=== 3. GET 音频文件 ===")
if d.get("name"):
    r2 = c.get(d["url"])
    print("status:", r2.status_code, "| type:", r2.headers.get("content-type"),
          "| 长度:", len(r2.content))

print("=== 4. 空文本应 400 ===")
r3 = c.post("/api/tts/synth", json={"text": ""})
print("status:", r3.status_code, "|", str(r3.json())[:80])

print("=== 5. 离线引擎 ===")
r4 = c.post("/api/tts/synth", json={"text": "离线语音测试。",
                                   "voice": "sapi:Microsoft Huihui Desktop", "engine": "sapi"})
d4 = r4.json()
print("status:", r4.status_code, "| ok:", d4.get("ok"), "| engine:", d4.get("engine"),
      "| name:", d4.get("name"), "| bytes:", d4.get("bytes"))

print("=== 6. 不存在的音频 404 ===")
print(c.get("/api/tts/audio/nope.mp3").status_code)
