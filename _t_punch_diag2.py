# -*- coding: utf-8 -*-
"""确认接收循环是否还活着 + A 有没有收到第二次 Q（看时间戳）。"""
import io
import os
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.getcwd())

import backend.hive_punch as HP  # noqa: E402
from backend import hive_mesh as HM  # noqa: E402


def loops():
    n = 0
    for t in threading.enumerate():
        tgt = getattr(t, "_target", None)
        if tgt is not None and getattr(tgt, "__name__", "") == "_recv_loop":
            n += 1
    return n


a = HM.MeshNode()
a.start(0, [])
b = HM.MeshNode()
b.start(0, [])
time.sleep(0.5)
peer = ("127.0.0.1", a.punch.port)
print("udp A=%s B=%s  _recv_loop 线程数=%d"
      % (a.punch.port, b.punch.port, loops()))

r = b.punch.connect("127.0.0.1", peer[1], timeout=5.0)
print("1st ok=%s" % r.get("ok"))
ts1 = a.punch._punch_seen.get(peer)
r["sock"].close()
time.sleep(0.4)
print("A.udp 是否已关闭: %s" % (a.punch.udp._closed if hasattr(a.punch.udp, "_closed") else "?"))
print("A._stop=%s  线程数=%d" % (a.punch._stop, loops()))

time.sleep(0.3)
b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
time.sleep(0.6)
ts2 = a.punch._punch_seen.get(peer)
print("A 收到 Q 的时间戳: %s -> %s  (%s)"
      % (ts1, ts2, "更新了=收到了" if ts1 != ts2 else "没变=没收到"))
print("A.tunnels alive=%s" % [t.alive for t in a.punch.tunnels.values()])

# 再直接试一次 sendto，看是否报错
try:
    n = b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
    print("B sendto 返回 %s（字节数）" % n)
except Exception as e:
    print("B sendto 报错: %s" % e)

a.stop()
b.stop()
