# -*- coding: utf-8 -*-
"""诊断：第二次打洞时，A 有没有收到 Q？A 有没有回 R？B 有没有收到 R？"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.getcwd())

from backend import hive_mesh as HM  # noqa: E402

a = HM.MeshNode()
a.start(0, [])
b = HM.MeshNode()
b.start(0, [])
time.sleep(0.5)
pa = a.punch.port
peer = ("127.0.0.1", pa)

print("A udp=%s  B udp=%s" % (pa, b.punch.port))


def dump(tag):
    print("  [%s] A._punch_seen=%s A.tunnels=%s(alive=%s)"
          % (tag, list(a.punch._punch_seen.keys()),
             len(a.punch.tunnels),
             [t.alive for t in a.punch.tunnels.values()]))
    print("  [%s] B._punch_ok=%s B.tunnels=%s(alive=%s)"
          % (tag, list(b.punch._punch_ok.keys()),
             len(b.punch.tunnels),
             [t.alive for t in b.punch.tunnels.values()]))


dump("before")
r = b.punch.connect("127.0.0.1", pa, timeout=5.0)
print("1st: ok=%s" % r.get("ok"))
dump("after1")
if r.get("ok"):
    r["sock"].close()
time.sleep(0.4)
dump("after close1")

# 手工发一次 Q，看 A 回不回
import backend.hive_punch as HP  # noqa: E402
token = os.urandom(4)
b.punch.udp.sendto(HP.PUNCH_MAGIC + b"Q" + token, peer)
time.sleep(0.5)
dump("after manual Q")
print("  B._punch_ok 里有没有 %s -> %s"
      % (peer, peer in b.punch._punch_ok))

r2 = b.punch.connect("127.0.0.1", pa, timeout=5.0)
print("2nd: ok=%s tries=%s err=%s"
      % (r2.get("ok"), r2.get("tries"), (r2.get("error") or "")[:50]))
dump("after2")

a.stop()
b.stop()
