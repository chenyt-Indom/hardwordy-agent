# -*- coding: utf-8 -*-
"""本机（NAT 后）用产品代码 PunchNode 向腾讯云服务器（公网）真实打洞，
建立可靠 UDP 隧道并双向传数据。"""
import sys
import time

sys.path.insert(0, ".")
from backend.hive_punch import PunchNode

SRV = "139.199.69.88"
PORT = 34789

node = PunchNode()
r = node.start(0)
print("local punch node:", r)

m = node.mapped(timeout=3.0)
print("mapped:", m)

t0 = time.time()
res = node.connect(SRV, PORT, timeout=12.0)
dt = time.time() - t0
print("connect ok=%s tries=%s in %.2fs" % (res.get("ok"), res.get("tries"), dt))
if not res.get("ok"):
    print("ERROR:", res.get("error"))
    node.stop()
    sys.exit(1)

sock = res["sock"]
print("tunnel established to", SRV, PORT)

# 发数据（含中文和大包，验证可靠层）
msg = "HELLO-XNET-跨网直连-" + "X" * 3000
payload = msg.encode("utf-8")
t1 = time.time()
sock.sendall(payload)
print("sent %d bytes" % len(payload))

sock.settimeout(12.0)
try:
    back = sock.recv(4096)
    print("recv %d bytes in %.0fms" % (len(back), (time.time() - t1) * 1000))
    print("reply head:", back[:40])
    ok = back.startswith(b"PONG-XNET-") and payload in back
    print("ROUNDTRIP:", "OK" if ok else "MISMATCH")
except Exception as e:
    print("recv failed:", repr(e))

print("status:", node.status())
node.stop()
