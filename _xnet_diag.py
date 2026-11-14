# -*- coding: utf-8 -*-
import paramiko, time
HOST, USER, KEY = "139.199.69.88", "root", r"C:\Users\19853\.ssh\id_rsa"
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, 22, USER, key_filename=KEY, timeout=10)
def run(c, t=20):
    si, so, se = cli.exec_command(c, timeout=t)
    return so.read().decode("utf-8", "replace").strip()

print("=== punch.log ===")
print(run("cat /tmp/punch.log") or "(empty)")
print("\n=== ufw ===")
print(run("ufw status | grep 34789 || echo none"))
print("\n=== process alive? ===")
print(run("ps aux | grep _punch_listen | grep -v grep || echo 'NOT RUNNING'"))
print("\n=== iptables INPUT first rules ===")
print(run("iptables -L INPUT -n -v --line-numbers 2>/dev/null | head -8"))
print("\n=== tcpdump 3s on udp 34789 (does packet arrive?) ===")
print(run("timeout 3 tcpdump -n -i any udp port 34789 -c 5 2>&1 | tail -8 || echo 'no-tcpdump'", t=15))
cli.close()
