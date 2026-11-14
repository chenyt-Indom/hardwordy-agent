# -*- coding: utf-8 -*-
"""测试 B 端：入群 → 起 mesh → 连 A → 收流式帧。用完删除。"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import hive, hive_mesh, hive_client

SHARE = os.path.join(ROOT, "_qa_stream_share.json")
PORT = 34891

# 等 A 建好群
for _ in range(40):
    if os.path.exists(SHARE):
        try:
            d = json.load(open(SHARE, encoding="utf-8"))
            if d.get("gid"):
                break
        except Exception:
            pass
    time.sleep(0.25)
else:
    print("B 没等到群信息", flush=True)
    sys.exit(1)

gid = d["gid"]
key = hive._key_from_b64(d["key"])
hive.join_with_key(gid, key, "成员B")
print("B 已入群:", gid, flush=True)

got = []


def on_stream(g, obj):
    got.append(obj)
    print("B 收到 phase=%s chars=%s full=%d字 delta=%r"
          % (obj.get("phase"), (obj.get("task") or {}).get("chars"),
             len(obj.get("full") or ""), (obj.get("delta") or "")[:12]), flush=True)


cli = hive_client.client()
cli.on_stream = on_stream
m = hive_mesh.node()
m.on_peer = lambda *a, **k: print("B on_peer:", a, k, flush=True)

print("B mesh.start:", m.start(port=0, groups=[gid]), flush=True)
# start 之后**强制重挂**：确认回调真的在 mesh 上（_hooked 缓存会跳过重复挂载）
cli._hooked = False
cli._hook()
print("B on_stream 已挂:", getattr(m, "on_stream", None) is not None, flush=True)
time.sleep(0.8)
print("B connect:", m.connect("127.0.0.1", PORT, gid), flush=True)
time.sleep(1.5)
print("B 此刻 peers:", getattr(m, "peer_count", lambda: "?")() if hasattr(m, "peer_count") else "?", flush=True)

time.sleep(7)
print("B 总共收到:", len(got), "帧", flush=True)
fulls = [len(o.get("full") or "") for o in got]
print("B 收到的 full 长度序列:", fulls, flush=True)
print("B 单调递增(无残缺):", all(b >= a for a, b in zip(fulls, fulls[1:])) if fulls else None, flush=True)
print("B DONE", flush=True)
