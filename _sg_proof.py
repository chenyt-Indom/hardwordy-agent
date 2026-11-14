# -*- coding: utf-8 -*-
"""对照实验：证明是腾讯云安全组在拦，而非本机 iptables"""
import sys, io, socket
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("139.199.69.88", 22, "root", key_filename=r"C:\Users\19853\.ssh\id_rsa", timeout=15)

def run(cmd, timeout=20):
    try:
        i, o, e = cli.exec_command(cmd, timeout=timeout)
        return (o.read().decode('utf-8','replace') + e.read().decode('utf-8','replace')).strip()
    except Exception as ex:
        return f"<ERR {ex}>"

print("=== 1. 网卡上的真实 IP（公网 IP 是否在网卡上）===")
print(run("ip -4 addr show | grep -E 'inet ' | grep -v '127.0.0.1'"))

print("\n=== 2. 公网 IP 归属 ===")
print(run("curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/public-ipv4"))

print("\n=== 3. 对照实验 ===")
print("  5000: iptables 有 ACCEPT 规则 → 外部是否可达?")
print("  8000: iptables 有 ACCEPT 规则 → 外部是否可达?")

def probe(port):
    try:
        s = socket.create_connection(("139.199.69.88", port), timeout=5); s.close()
        return "OPEN"
    except Exception as ex:
        return type(ex).__name__

for p in (5000, 8000, 18765):
    print(f"    TCP {p}: {probe(p)}")

cli.close()
print("\nDONE")
