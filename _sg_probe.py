# -*- coding: utf-8 -*-
"""探测服务器上有没有可自动修改安全组的凭据"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import paramiko

HOST = "139.199.69.88"
USER = "root"
KEY = r"C:\Users\19853\.ssh\id_rsa"

def run(cli, cmd, timeout=25):
    try:
        i, o, e = cli.exec_command(cmd, timeout=timeout)
        out = o.read().decode('utf-8', 'replace')
        err = e.read().decode('utf-8', 'replace')
        return (out + err).strip()
    except Exception as ex:
        return f"<ERR {ex}>"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, 22, USER, key_filename=KEY, timeout=15)

probes = [
    ("instance-id", "curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/instance-id"),
    ("region",      "curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/placement/region"),
    ("zone",        "curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/placement/zone"),
    ("cam-role",    "curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/"),
    ("tccli",       "which tccli 2>/dev/null || echo NONE"),
    ("py-tencent",  "python3 -c \"import tencentcloud;print(tencentcloud.__file__)\" 2>&1 | head -2"),
    ("env-keys",    "env | grep -i -E 'TENCENT|QCLOUD|SECRET' | sed 's/=.*/=<hidden>/' || echo NONE"),
    ("home-files",  "ls -a ~ 2>/dev/null | grep -i -E 'tccli|tencent' || echo NONE"),
    ("os",          "cat /etc/os-release | grep PRETTY | head -1"),
]
for name, cmd in probes:
    print(f"[{name}] {run(cli, cmd)}")

# 如果有角色，取临时密钥（只看有没有，不打印明文）
role = run(cli, "curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/")
if role and role not in ("NONE", "") and "<ERR" not in role and "404" not in role:
    cred = run(cli, f"curl -s -m 5 http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/{role.strip()}")
    print(f"[cred-len] {len(cred)} chars")
    print(f"[cred-keys] {[k for k in ['TmpSecretId','TmpSecretKey','Token','Expiration'] if k in cred]}")

cli.close()
print("DONE")
