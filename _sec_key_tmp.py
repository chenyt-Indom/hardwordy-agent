# 临时安全检查：配置里有没有暴露密钥（用完即删）
import json
import ssl
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = "139.199.69.88"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = f"https://{HOST}/api/config"
req = urllib.request.Request(url, headers={"User-Agent": "sec-check/1.0"})
with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
    cfg = json.loads(r.read().decode("utf-8", "replace"))

c = cfg.get("config") or {}
print("配置字段清单：")
for k, v in c.items():
    s = str(v)
    # 只显示前几个字符，判断是不是真密钥
    if any(t in k.lower() for t in ("key", "token", "secret", "password")):
        shown = (s[:6] + "..." + s[-4:]) if len(s) > 12 else ("(空)" if not s else "***")
        print(f"  ⚠️ {k} = {shown}  (长度 {len(s)})")
    else:
        print(f"     {k} = {s[:70]}")
