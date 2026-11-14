# -*- coding: utf-8 -*-
"""在 YJ-FIREWALL-INPUT 之前插入 ACCEPT，绕过安全组同步链"""
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

print("=== A. YJ 链里针对 18765 的规则 ===")
print(run("iptables -L YJ-FIREWALL-INPUT -n --line-numbers 2>&1 | grep -E '18765|Chain|DROP|REJECT' | head -12"))

print("\n=== B. 备份当前 iptables ===")
print(run("mkdir -p /root/fw_bak && iptables-save > /root/fw_bak/iptables_$(date +%Y%m%d_%H%M%S).bak && ls -la /root/fw_bak/ | tail -3"))

print("\n=== C. 插入 ACCEPT 18765 到 INPUT 最前（优先于 YJ 链）===")
print(run("iptables -I INPUT 1 -p tcp --dport 18765 -j ACCEPT && echo INSERTED"))
print(run("iptables -L INPUT -n --line-numbers | head -6"))

print("\n=== D. 服务器自连公网 18765 ===")
r = run("timeout 4 bash -c 'echo > /dev/tcp/139.199.69.88/18765' && echo 'SELF-PUBLIC OK' || echo 'SELF-PUBLIC FAIL'")
print(r)

cli.close()

print("\n=== E. 本机外网探测 18765 ===")
ok = False
for attempt in range(3):
    try:
        s = socket.create_connection(("139.199.69.88", 18765), timeout=6)
        s.close()
        print(f"  第{attempt+1}次: OPEN  <-- 放行成功")
        ok = True
        break
    except Exception as ex:
        print(f"  第{attempt+1}次: {type(ex).__name__}")
        time.sleep(1.5)

print("\nRESULT:", "SUCCESS" if ok else "STILL_BLOCKED")
