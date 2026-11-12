# 临时测试：真实服务器 probe_auto（用完即删）
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend import hive_deploy

r = hive_deploy.probe_auto("139.199.69.88", timeout=4.0)
print("ok:", r.get("ok"))
print("url:", r.get("url"))
print("tried:", r.get("tried"))
err = r.get("error") or {}
print("error:", err.get("code"), "/", err.get("title"))
print("reason:", str(err.get("reason"))[:200])
print("\n--- steps ---")
for s in r.get("steps") or []:
    print(f"  [{s.get('level'):4s}] {s.get('text')}")
