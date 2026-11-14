# -*- coding: utf-8 -*-
"""真实跨网 UDP 直连验证：
本机（NAT 后，公网映射 219.136.70.51:xxxx）
  <- 腾讯云服务器（139.199.69.88，另一网络）主动打过来
若本机能收到，说明入向映射真实有效，打洞机制在真实链路上成立。"""
import socket
import sys
import time

sys.path.insert(0, ".")
from backend.hive_punch import STUN_SERVERS, parse_stun, stun_request

import paramiko

SRV_HOST = "139.199.69.88"
SRV_USER = "root"
SRV_KEY = r"C:\Users\19853\.ssh\id_rsa"
SRV_KEY2 = r"C:\Users\19853\.ssh\id_ed25519"

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 0))
s.settimeout(3.0)
print("local bind : %s:%d" % s.getsockname())

mapped = None
for host, port in STUN_SERVERS:
    try:
        ip = socket.gethostbyname(host)
        s.sendto(stun_request(), (ip, port))
        data, _ = s.recvfrom(2048)
        mapped = parse_stun(data)
        if mapped:
            print("STUN mapped: %s:%d  (via %s)" % (mapped[0], mapped[1], host))
            break
    except Exception:
        continue

if not mapped:
    print("FAIL: no STUN mapping")
    sys.exit(1)

mip, mport = mapped
s.settimeout(6.0)

# 通过 SSH 让服务器往本机映射地址发 UDP
remote_code = (
    "import socket;"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "s.sendto(b'HIVE-XNET-PROBE-%d',('%s',%d));"
    "print('server-sent-to %s:%d')"
) % (int(time.time()), mip, mport, mip, mport)

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
keyfile = None
for k in (SRV_KEY, SRV_KEY2):
    try:
        open(k, "rb").read(16)
        keyfile = k
        break
    except Exception:
        pass
if not keyfile:
    print("FAIL: no ssh key")
    sys.exit(1)

t0 = time.time()
cli.connect(SRV_HOST, 22, SRV_USER, key_filename=keyfile, timeout=10)
print("ssh ok %.2fs" % (time.time() - t0))

si, so, se = cli.exec_command("python3 -c \"%s\"" % remote_code.replace('"', '\\"'), timeout=15)
print("remote stdout:", so.read().decode("utf-8", "replace").strip())
err = se.read().decode("utf-8", "replace").strip()
if err:
    print("remote stderr:", err[:300])
cli.close()

# 本机收包
try:
    data, addr = s.recvfrom(2048)
    print("")
    print("*** RECEIVED from %s:%d ***" % (addr[0], addr[1]))
    print("payload:", data.decode("utf-8", "replace"))
    print("RESULT: CROSS-NETWORK UDP REACHABLE - hole punching mechanism VALID")
    # 回一个包，验证双向
    s.sendto(b"HIVE-XNET-PONG", addr)
    print("sent pong back to server")
except socket.timeout:
    print("")
    print("RESULT: TIMEOUT - no packet from server (inbound blocked?)")

s.close()
