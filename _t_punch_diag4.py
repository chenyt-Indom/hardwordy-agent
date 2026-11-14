# -*- coding: utf-8 -*-
"""细探：同一个 peer 第二次 Q，_on_punch 内部到底怎么判的。"""
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
log = []

orig = a.punch._on_punch


def wrap(data, key):
    kind = data[len(HP.PUNCH_MAGIC):len(HP.PUNCH_MAGIC) + 1]
    before = [(k, t.alive, id(t)) for k, t in a.punch.tunnels.items()]
    try:
        orig(data, key)
        err = ""
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    after = [(k, t.alive, id(t)) for k, t in a.punch.tunnels.items()]
    log.append((kind, key, before, after, err))


a.punch._on_punch = wrap

r = b.punch.connect("127.0.0.1", peer[1], timeout=5.0)
print("1st ok=%s" % r.get("ok"))
r["sock"].close()
time.sleep(0.5)

for i, e in enumerate(log):
    print("\n[%d] kind=%s key=%s" % (i, e[0], e[1]))
    print("   before=%s" % (e[2],))
    print("   after =%s" % (e[3],))
    if e[4]:
        print("   !! 异常 %s" % e[4])

log.clear()
b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + os.urandom(4), peer)
time.sleep(0.8)
for i, e in enumerate(log):
    print("\n[第二次 Q #%d] key=%s" % (i, e[1]))
    print("   before=%s" % (e[2],))
    print("   after =%s" % (e[3],))
    if e[4]:
        print("   !! 异常 %s" % e[4])

print("\n最终 A.tunnels=%s"
      % [(k, t.alive) for k, t in a.punch.tunnels.items()])
a.stop()
b.stop()
