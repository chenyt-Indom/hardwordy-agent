# 临时测试：内置中继 /hive 是否工作（用完即删）
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient
from backend.main import app

c = TestClient(app)

print("=== 1) /hive/health ===")
r = c.get("/hive/health")
print("  status:", r.status_code)
print("  body:", json.dumps(r.json(), ensure_ascii=False))

print("\n=== 2) WebSocket /hive 握手 + join ===")
try:
    with c.websocket_connect("/hive") as ws:
        ws.send_text(json.dumps({
            "t": "join", "gid": "g_test_embedded", "uid": "u_a",
            "name": "测试甲", "since": 0, "v": 2, "caps": ["text"],
        }, ensure_ascii=False))
        msg = json.loads(ws.receive_text())
        print("  收到类型:", msg.get("t"))
        print("  seq:", msg.get("seq"), " members:", len(msg.get("members") or []))
        print("  proto:", msg.get("proto"), " relay_ver:", msg.get("relay_ver"))

        print("\n=== 3) 发一条消息 ===")
        ws.send_text(json.dumps({
            "t": "msg", "mid": "m1", "blob": "0:hello-embedded", "ts": 1.0, "v": 2,
        }, ensure_ascii=False))
        m2 = json.loads(ws.receive_text())
        print("  收到类型:", m2.get("t"), " seq:", m2.get("seq"),
              " blob:", m2.get("blob"))

        print("\n=== 4) ping/pong ===")
        ws.send_text(json.dumps({"t": "ping", "ts": 123.0}))
        m3 = json.loads(ws.receive_text())
        print("  收到类型:", m3.get("t"), " echo:", m3.get("echo"))

        print("\n=== 5) 未知类型不踢连接 ===")
        ws.send_text(json.dumps({"t": "hologram", "x": 1}))
        m4 = json.loads(ws.receive_text())
        print("  收到类型:", m4.get("t"), " code:", m4.get("code"))
        # 还能继续用
        ws.send_text(json.dumps({"t": "ping", "ts": 456.0}))
        m5 = json.loads(ws.receive_text())
        print("  未知类型之后仍可用:", m5.get("t") == "pong")
except Exception as e:
    print("  FAILED:", type(e).__name__, e)

print("\n=== 6) 路由是否注册 ===")
paths = sorted({getattr(r, "path", "") for r in app.routes
                if getattr(r, "path", "").startswith("/hive")})
print("  ", paths)

print("\n=== 7) candidates() 自动展开 ===")
from backend import hive_deploy
for inp in ["139.199.69.88", "139.199.69.88:8765", "wss://x.com/hive",
            "wss://139.199.69.88", "ws://1.2.3.4"]:
    print(f"  {inp:28s} -> {hive_deploy.candidates(inp)}")
