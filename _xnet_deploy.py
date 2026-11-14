# -*- coding: utf-8 -*-
"""上传 hive_punch.py 到服务器，并在服务器上启动打洞监听端。"""
import paramiko

HOST = "139.199.69.88"
USER = "root"
KEY = r"C:\Users\19853\.ssh\id_rsa"

SERVER_SIDE = r'''# -*- coding: utf-8 -*-
import sys, time, threading
sys.path.insert(0, "/opt/hive")
from hive_punch import PunchNode

node = PunchNode()
r = node.start(34789)
print("START", r, flush=True)

def on_tunnel(sock, addr):
    print("TUNNEL-FROM %s:%s" % addr, flush=True)
    try:
        sock.settimeout(25)
        data = sock.recv(4096)
        print("RECV %d bytes: %s" % (len(data), data[:80]), flush=True)
        sock.sendall(b"PONG-XNET-" + data)
        print("ECHOED %d bytes" % (len(b"PONG-XNET-") + len(data)), flush=True)
    except Exception as e:
        print("TUNNEL-ERR %r" % (e,), flush=True)

node.on_tunnel = on_tunnel
print("LISTENING 34789", flush=True)
time.sleep(60)
node.stop()
print("DONE", flush=True)
'''

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, 22, USER, key_filename=KEY, timeout=10)
print("ssh connected")

sftp = cli.open_sftp()
sftp.put(r"D:\local-multimodal-src\backend\hive_punch.py", "/opt/hive/hive_punch.py")
print("uploaded hive_punch.py")
with sftp.open("/opt/hive/_punch_listen.py", "w") as f:
    f.write(SERVER_SIDE)
print("uploaded listener script")
sftp.close()

# 检查 UDP 34789 是否被防火墙拦
cmds = [
    "ss -lunp | grep 34789 || echo 'not-listening-yet'",
    "iptables -L INPUT -n --line-numbers 2>/dev/null | head -20",
    "which ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -10 || echo 'no-ufw'",
]
for c in cmds:
    si, so, se = cli.exec_command(c, timeout=15)
    out = so.read().decode("utf-8", "replace").strip()
    print("--- %s\n%s" % (c, out))

cli.close()
