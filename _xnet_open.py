# -*- coding: utf-8 -*-
"""临时放行 UDP 34789 并启动服务器端打洞监听（后台）。"""
import paramiko
import time

HOST, USER, KEY = "139.199.69.88", "root", r"C:\Users\19853\.ssh\id_rsa"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, 22, USER, key_filename=KEY, timeout=10)

def run(c, t=20):
    si, so, se = cli.exec_command(c, timeout=t)
    return so.read().decode("utf-8", "replace").strip(), se.read().decode("utf-8", "replace").strip()

# 1) 临时放行 UDP 34789（记下来，测完必须删）
o, e = run("ufw allow 34789/udp comment 'hive-punch-test-temp' 2>&1")
print("ufw allow:", o or e)

# 2) 起监听端（后台，60 秒后自动退出）
run("pkill -f _punch_listen.py 2>/dev/null; sleep 0.3")
o, e = run("cd /opt/hive && nohup python3 _punch_listen.py > /tmp/punch.log 2>&1 & echo started pid=$!")
print("start:", o or e)
time.sleep(1.5)

o, e = run("cat /tmp/punch.log")
print("--- server log ---")
print(o or "(empty)")
o, e = run("ss -lunp | grep 34789 || echo 'NOT-LISTENING'")
print("--- listen state ---")
print(o)

cli.close()
print("\n=== UDP 34789 temporarily opened for test ===")
