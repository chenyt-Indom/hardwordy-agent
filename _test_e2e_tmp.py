# 临时端到端测试：起临时服务 → probe_auto 自动发现 → WS 真连（用完即删）
import json
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import uvicorn
from backend.main import app

PORT = 8123
cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(cfg)
th = threading.Thread(target=server.run, daemon=True)
th.start()

# 等端口起来
ok_up = False
for _ in range(40):
    time.sleep(0.25)
    try:
        import socket
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            ok_up = True
            break
    except Exception:
        pass
print("临时服务已启动:", ok_up)

from backend import hive_deploy

print("\n=== probe_auto('127.0.0.1:%d') ===" % PORT)
r = hive_deploy.probe_auto(f"127.0.0.1:{PORT}", timeout=4.0)
print("ok:", r.get("ok"))
print("url:", r.get("url"))
print("tried:", r.get("tried"))
for s in r.get("steps") or []:
    print(f"  [{s.get('level'):4s}] {s.get('text')}")

print("\n=== 真连一次内置中继 ===")
import asyncio
import websockets

async def real_conn():
    url = r.get("url") or f"ws://127.0.0.1:{PORT}/hive"
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(json.dumps({
            "t": "join", "gid": "g_e2e", "uid": "u_e2e",
            "name": "端到端", "since": 0, "v": 2,
        }, ensure_ascii=False))
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        await ws.send(json.dumps({
            "t": "msg", "mid": "e1", "blob": "0:e2e-blob", "ts": 1.0,
        }, ensure_ascii=False))
        m2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        return m, m2

try:
    m, m2 = asyncio.run(real_conn())
    print("  join 回复:", m.get("t"), " proto:", m.get("proto"))
    print("  msg  回复:", m2.get("t"), " seq:", m2.get("seq"),
          " blob:", m2.get("blob"))
    print("  结论: 内置中继端到端可用 ✓")
except Exception as e:
    print("  FAILED:", type(e).__name__, e)

print("\n=== candidates 回归 ===")
for inp in ["127.0.0.1:8000", "192.168.1.5:8000", "139.199.69.88",
            "139.199.69.88:8765", "wss://x.com/hive"]:
    print(f"  {inp:22s} -> {hive_deploy.candidates(inp)}")

server.should_exit = True
th.join(timeout=6)
print("\n临时服务已关闭")
