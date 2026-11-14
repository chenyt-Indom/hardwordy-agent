# -*- coding: utf-8 -*-
"""测试 A 端：建群 → 起 mesh → 发一串 thinking 流式帧（带 full）。用完删除。"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import hive, hive_mesh, hive_client

SHARE = os.path.join(ROOT, "_qa_stream_share.json")
PORT = 34891

g = hive.create_group("流式广播测试")
gid = g["gid"]
_full = hive.load_group(gid) or {}      # create_group 的返回值故意不含密钥
with open(SHARE, "w", encoding="utf-8") as f:
    json.dump({"gid": gid, "key": _full.get("key") or ""}, f)
print("A 建群:", gid, flush=True)

m = hive_mesh.node()
r = m.start(port=PORT, groups=[gid])
print("A mesh.start:", r, flush=True)

cli = hive_client.client()
cli.on_stream = lambda g, o: None   # A 自己发的会回环，忽略

time.sleep(4)                       # 等 B 连上
print("A peers:", m.peer_count() if hasattr(m, "peer_count") else "?", flush=True)

sent = 0
for i in range(1, 6):
    txt = "思考片段" * i
    r = cli.send_stream(gid, "mid_test", "thinking", txt[-6:], {"chars": len(txt)}, i, full=txt)
    print("A 发送第%d帧 full=%d字 -> %s" % (i, len(txt), r), flush=True)
    if r.get("peers"):
        sent += 1
    time.sleep(0.4)

print("A 实际广播成功的帧数:", sent, "/5", flush=True)
time.sleep(2)
print("A DONE", flush=True)
