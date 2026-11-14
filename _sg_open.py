# -*- coding: utf-8 -*-
"""在服务器上自动放行 18765/tcp，并验证公网可达"""
import sys, io, time, socket
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("139.199.69.88", 22, "root", key_filename=r"C:\Users\19853\.ssh\id_rsa", timeout=15)

def run(cmd, timeout=25):
    try:
        i, o, e = cli.exec_command(cmd, timeout=timeout)
        return (o.read().decode('utf-8','replace') + e.read().decode('utf-8','replace')).strip()
    except Exception as ex:
        return f"<ERR {ex}>"

print("=== A. YJ-FIREWALL-INPUT 前 12 条（看它是否 DROP 非白名单）===")
print(run("iptables -L YJ-FIREWALL-INPUT -n --line-numbers 2>&1 | head -14"))

print("\n=== B. 放行前 18765 是否在 ufw ===")
print(run("ufw status | grep 18765 || echo 'NOT IN UFW'"))

print("\n=== C. 执行放行 18765/tcp ===")
print(run("ufw allow 18765/tcp comment 'hive-relay direct' 2>&1 | tail -3"))

print("\n=== D. 放行后 ufw 状态 ===")
print(run("ufw status | grep -E '18765|Status' "))

print("\n=== E. 服务器自连公网 18765 ===")
print(run("timeout 4 bash -c 'echo > /dev/tcp/139.199.69.88/18765' && echo 'SELF-PUBLIC OK' || echo 'SELF-PUBLIC FAIL'"))

cli.close()

# F. 从本机（外网）真实探测
print("\n=== F. 本机公网探测 18765（TCP 握手）===")
for attempt in range(3):
    try:
        s = socket.create_connection(("139.199.69.88", 18765), timeout=5)
        s.close()
        print(f"  第{attempt+1}次: OPEN  <-- 放行成功")
        break
    except Exception as ex:
        print(f"  第{attempt+1}次: {type(ex).__name__}")
        time.sleep(1.5)

print("DONE")
