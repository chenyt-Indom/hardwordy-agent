# -*- coding: utf-8 -*-
"""清理：删除临时 ufw 规则、停掉监听进程、删除上传的测试文件。"""
import paramiko
HOST, USER, KEY = "139.199.69.88", "root", r"C:\Users\19853\.ssh\id_rsa"
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, 22, USER, key_filename=KEY, timeout=10)
def run(c, t=20):
    si, so, se = cli.exec_command(c, timeout=t)
    return so.read().decode("utf-8", "replace").strip()

print(run("pkill -f _punch_listen.py; echo killed"))
print(run("ufw delete allow 34789/udp 2>&1 | tail -2"))
print(run("rm -f /opt/hive/_punch_listen.py /opt/hive/hive_punch.py /tmp/punch.log; echo cleaned"))
print("--- verify ufw ---")
print(run("ufw status | grep 34789 || echo 'rule removed'"))
print("--- verify process ---")
print(run("ps aux | grep _punch_listen | grep -v grep || echo 'no process'"))
print("--- relay service still ok? ---")
print(run("systemctl is-active hive-relay; ss -ltnp | grep 18765 || echo 'no-18765'"))
cli.close()
