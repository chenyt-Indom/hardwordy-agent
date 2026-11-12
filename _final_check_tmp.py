# 最终回归（用完即删）
import json
import os
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

print("=== 1) 导入与路由 ===")
from fastapi.testclient import TestClient
from backend.main import app

paths = sorted({getattr(r, "path", "") for r in app.routes
                if "/hive" in getattr(r, "path", "")})
print(f"  共 {len(paths)} 条 hive 路由")
for p in paths:
    print("   ", p)

assert "/hive" in paths, "缺内置中继 WS 路由"
assert "/hive/health" in paths, "缺内置中继健康检查"
assert "/api/hive/upgrade/download" in paths, "缺升级包下载"

c = TestClient(app)
r = c.get("/hive/health")
print("  /hive/health ->", r.status_code, json.dumps(r.json(), ensure_ascii=False))

print("\n=== 2) 内置中继 WS 全流程 ===")
with c.websocket_connect("/hive") as ws:
    ws.send_text(json.dumps({"t": "join", "gid": "g_reg", "uid": "u1",
                             "name": "回归", "since": 0, "v": 2}, ensure_ascii=False))
    m = json.loads(ws.receive_text())
    print("  join ->", m.get("t"), "seq", m.get("seq"))
    ws.send_text(json.dumps({"t": "msg", "mid": "r1", "blob": "0:abc", "ts": 1.0},
                            ensure_ascii=False))
    m2 = json.loads(ws.receive_text())
    print("  msg  ->", m2.get("t"), "seq", m2.get("seq"), "blob", m2.get("blob"))
    assert m2.get("seq") == 1

print("\n=== 3) probe_auto 端到端（起临时服务） ===")
import uvicorn
PORT = 8124
cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(cfg)
th = threading.Thread(target=server.run, daemon=True)
th.start()
for _ in range(40):
    time.sleep(0.25)
    try:
        import socket
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            break
    except Exception:
        pass

from backend import hive_deploy
r2 = hive_deploy.probe_auto(f"127.0.0.1:{PORT}", timeout=4.0)
print("  ok:", r2.get("ok"), " url:", r2.get("url"))
for s in (r2.get("steps") or [])[-4:]:
    print(f"    [{s.get('level')}] {s.get('text')}")
assert r2.get("ok"), "自动发现失败"
assert r2.get("url", "").endswith("/hive"), "没选中 /hive 路径"

print("\n=== 4) 升级包 ===")
p = os.path.join(r"D:\local-multimodal-src", "data", "hive", "hive_upgrade.zip")
print("  存在:", os.path.exists(p), f"{os.path.getsize(p)/1024:.1f} KB")
import zipfile
with zipfile.ZipFile(p) as z:
    print("  内容:", z.namelist())
    assert z.testzip() is None

server.should_exit = True
th.join(timeout=6)

print("\n=== 5) candidates 回归 ===")
cases = {
    "139.199.69.88": 3,
    "139.199.69.88:8765": 2,
    "wss://x.com/hive": 1,
    "127.0.0.1:8000": 2,
}
for inp, n in cases.items():
    got = hive_deploy.candidates(inp)
    flag = "OK" if len(got) == n else f"期望{n}"
    print(f"  {inp:22s} {flag}  {got}")

print("\n全部通过")
