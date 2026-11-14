# -*- coding: utf-8 -*-
"""隔离：同一个 peer 连续打洞两次，第二次为什么会立刻失败。"""
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

print("A udp=%s  B udp=%s" % (pa, b.punch.port))

for i in (1, 2, 3):
    t0 = time.time()
    r = b.punch.connect("127.0.0.1", pa, timeout=6.0)
    dt = time.time() - t0
    print("第%d次 connect: ok=%s %.2fs tries=%s err=%s"
          % (i, r.get("ok"), dt, r.get("tries"),
             (r.get("error") or "")[:60]))
    if r.get("ok"):
        s = r["sock"]
        print("   alive=%s" % getattr(s, "alive", "?"))
        try:
            s.close()
        except Exception as e:
            print("   close err %s" % e)
        time.sleep(0.3)
        print("   after close: tunnels=%d b._stop=%s"
              % (len(b.punch.tunnels), b.punch._stop))

a.stop()
b.stop()
