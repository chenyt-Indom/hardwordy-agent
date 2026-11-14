# -*- coding: utf-8 -*-
"""查本机腾讯云凭据 + 服务器端口可达性"""
import sys, io, os, socket, json, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

print("=== 本机腾讯云凭据 ===")
hits = []
for k in list(os.environ.keys()):
    if any(t in k.upper() for t in ("TENCENT", "QCLOUD", "TC3", "SECRETID", "SECRETKEY")):
        hits.append(k)
print("环境变量:", hits or "无")
for p in [os.path.expanduser("~/.tccli"), os.path.expanduser("~/.config/tencentcloud")]:
    print(f"{p}: {'存在' if os.path.exists(p) else '无'}")
# 项目内配置
proj = os.path.dirname(os.path.abspath(__file__))
found = []
for pat in ["data/**/*.json", "*.json", "deploy/*.json"]:
    for f in glob.glob(os.path.join(proj, pat), recursive=True):
        try:
            if os.path.getsize(f) > 200000: continue
            txt = open(f, 'r', encoding='utf-8', errors='ignore').read().lower()
            if "secretid" in txt or "secretkey" in txt:
                found.append(os.path.relpath(f, proj))
        except Exception:
            pass
print("项目内含密钥字段的配置:", found or "无")

print("\n=== 服务器端口公网可达性（TCP）===")
def probe(host, port, timeout=4):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return "OPEN"
    except Exception as e:
        return f"BLOCKED({type(e).__name__})"

H = "139.199.69.88"
for port in [22, 80, 443, 5000, 8000, 8080, 18765, 9000]:
    print(f"  TCP {port:>5}: {probe(H, port)}")
