# -*- coding: utf-8 -*-
"""通过真实 HTTP API 执行一个已批准的计划，验证「批准→执行」链路。

用法：python _exec1.py <plan_id>
"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)

pid = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "http://127.0.0.1:8000"


def post(path, body, timeout=900):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


print("执行计划:", pid, flush=True)
t0 = time.time()
try:
    r = post("/api/selfiter/execute", {"id": pid, "source": "manual"})
    print("用时 %.1fs" % (time.time() - t0), flush=True)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:4000], flush=True)
except Exception as e:
    print("调用失败:", type(e).__name__, e, flush=True)
