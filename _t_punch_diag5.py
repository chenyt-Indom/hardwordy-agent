# -*- coding: utf-8 -*-
"""抓凶手：谁在 0.8 秒内把新隧道关掉了。"""
import io
import os
import sys
import time
import traceback

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

closes = []
_orig_close = HP.ReliableUDPSock.close


def close_wrap(self):
    if self.alive:
        st = "".join(traceback.format_stack(limit=6)[:-1])
        closes.append((self.peer, st))
    return _orig_close(self)


HP.ReliableUDPSock.close = close_wrap

r = b.punch.connect("127.0.0.1", peer[1], timeout=5.0)
print("1st ok=%s" % r.get("ok"))
r["sock"].close()
time.sleep(0.5)
print("--- 第一次关闭的调用栈数量: %d" % len(closes))
for p, st in closes[-2:]:
    print("peer=%s\n%s" % (p, st))

closes.clear()
b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
time.sleep(0.8)
print("--- 第二次 Q 之后，被关闭 %d 次" % len(closes))
for p, st in closes:
    print("peer=%s\n%s\n" % (p, st))

HP.ReliableUDPSock.close = _orig_close
a.stop()
b.stop()
