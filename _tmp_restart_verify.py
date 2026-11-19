
# -*- coding: utf-8 -*-
import json, time, urllib.request, os, sys
BASE = "http://127.0.0.1:8000"
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_restart_verify.json")

def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def post(path, body=None, timeout=15):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

res = {"steps": []}

# 1) 触发重启
try:
    r = post("/api/selfdev/apply_restart", {})
    res["steps"].append({"step": "apply_restart", "resp": r})
except Exception as e:
    res["steps"].append({"step": "apply_restart", "error": repr(e)[:300]})

# 2) 等新进程起来
time.sleep(3)
healthy = False
t0 = time.time()
while time.time() - t0 < 100:
    try:
        h = get("/api/health", timeout=5)
        if isinstance(h, dict):
            healthy = True
            res["health"] = h
            break
    except Exception:
        pass
    time.sleep(2)
res["healthy"] = healthy
res["wait_sec"] = round(time.time() - t0, 1)

# 3) 验证改动生效
if healthy:
    try:
        st = get("/api/selfiter/status", timeout=15)
        res["status"] = st
    except Exception as e:
        res["status_error"] = repr(e)[:300]

with open(out, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
