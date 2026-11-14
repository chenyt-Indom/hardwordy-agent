# -*- coding: utf-8 -*-
"""在 A 的 _on_punch 上挂计数器，确认第二次 Q 到底有没有进到 A。"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.getcwd())

import backend.hive_punch as HP  # noqa: E402
from backend import hive_mesh as HM  # noqa: E402

a = HM.MeshNode()
a.start(0, [])
b = HM.MeshNode()
b.start(0, [])
time.sleep(0.5)
peer = ("127.0.0.1", a.punch.port)

calls = []
orig = a.punch._on_punch


def wrap(data, key):
    kind = data[len(HP.PUNCH_MAGIC):len(HP.PUNCH_MAGIC) + 1]
    calls.append((kind, key))
    return orig(data, key)


a.punch._on_punch = wrap

r = b.punch.connect("127.0.0.1", peer[1], timeout=5.0)
print("1st ok=%s   A 收到的握手=%s" % (r.get("ok"), calls))
r["sock"].close()
time.sleep(0.5)
print("关掉后 A.tunnels alive=%s  A 收到的握手=%s"
      % ([t.alive for t in a.punch.tunnels.values()], calls))

before = len(calls)
b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
time.sleep(0.8)
print("手工 Q 之后：A 收到握手新增 %d 条 -> %s"
      % (len(calls) - before, calls[before:]))
print("A.tunnels alive=%s" % [t.alive for t in a.punch.tunnels.values()])

# 换个全新端口再打一次（模拟「另一台机器」）
import socket  # noqa: E402
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.bind(("0.0.0.0", 0))
before = len(calls)
u.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
time.sleep(0.6)
print("从全新端口发 Q：A 收到握手新增 %d 条 -> %s"
      % (len(calls) - before, calls[before:]))
print("A.tunnels alive=%s" % [t.alive for t in a.punch.tunnels.values()])
u.close()

a.stop()
b.stop()
