# -*- coding: utf-8 -*-
"""诊断 18765 到底被什么拦住"""
import sys, io
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

print("=== 1. 18765 监听在哪 ===")
print(run("ss -lntp | grep -E '18765|8000|8080|:80 |:443 ' || echo NONE"))

print("\n=== 2. ufw 状态 ===")
print(run("ufw status verbose 2>&1 | head -20"))

print("\n=== 3. iptables INPUT 链 ===")
print(run("iptables -L INPUT -n --line-numbers 2>&1 | head -15"))

print("\n=== 4. 本机自连 18765（排除安全组因素）===")
print(run("timeout 3 bash -c 'echo > /dev/tcp/127.0.0.1/18765' && echo 'LOCAL OK' || echo 'LOCAL FAIL'"))
print(run("timeout 3 bash -c 'echo > /dev/tcp/139.199.69.88/18765' && echo 'SELF-PUBLIC OK' || echo 'SELF-PUBLIC FAIL'"))

print("\n=== 5. 80/8080 入口反代是否指向 18765 ===")
print(run("grep -rn 'hive-relay' /etc/nginx/ 2>/dev/null | head -10"))

print("\n=== 6. 中继服务状态 ===")
print(run("systemctl is-active hive-relay; systemctl is-enabled hive-relay"))

cli.close()
print("DONE")
