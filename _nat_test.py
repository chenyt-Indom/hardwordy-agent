# -*- coding: utf-8 -*-
"""NAT 类型实测：同一个 UDP socket 向多个 STUN 服务器问映射地址。
端口一致 => 锥形 NAT（打洞可行）；端口不同 => 对称型 NAT（打洞必失败）。"""
import socket
import time
import sys

sys.path.insert(0, ".")
from backend.hive_punch import STUN_SERVERS, parse_stun, stun_request

# 补充几个国内可达的 STUN
EXTRA = [
    ("stun.qq.com", 3478),
    ("stun.thinkrosystem.com", 3478),
    ("stun.syncthing.net", 3478),
]

ALL = list(STUN_SERVERS) + EXTRA

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 0))
print("local udp : %s:%d" % s.getsockname())
s.settimeout(3.0)

results = []
for host, port in ALL:
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        print("%-28s DNS-FAIL %s" % (host, e))
        continue
    txid = stun_request()[20:32]
    t0 = time.time()
    try:
        s.sendto(stun_request(txid), (ip, port))
        data, addr = s.recvfrom(2048)
        dt = (time.time() - t0) * 1000
        r = parse_stun(data)
        if r:
            print("%-28s -> %s:%d   %.1fms" % (host, r[0], r[1], dt))
            results.append((host, r[0], r[1]))
        else:
            print("%-28s -> PARSE-FAIL len=%d magic=%s first=%s"
                  % (host, len(data), data[4:8].hex() if len(data) >= 8 else "-",
                     data[:2].hex() if len(data) >= 2 else "-"))
    except socket.timeout:
        print("%-28s -> TIMEOUT" % host)
    except Exception as e:
        print("%-28s -> ERR %s" % (host, e))

print("")
ports = sorted(set(p for _, _, p in results))
ips = sorted(set(i for _, i, _ in results))
print("mapped ips   :", ips)
print("mapped ports :", ports)

if len(results) >= 2:
    if len(ports) == 1:
        print("RESULT: CONE NAT -> hole punching LIKELY WORKS")
    else:
        print("RESULT: PORT CHANGES PER TARGET -> SYMMETRIC-ish, punching HARD")
else:
    print("RESULT: not enough STUN replies (%d)" % len(results))

s.close()
