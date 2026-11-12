# 临时安全检查：服务器上是否暴露了敏感接口（用完即删）
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

CHECKS = [
    "/api/selfdev/tree",
    "/api/selfdev/read?rel=backend/main.py",
    "/api/config",
    "/api/sessions",
    "/api/kb",
    "/api/memory",
    "/api/tts/voices",
    "/api/files?path=.",
]

for p in CHECKS:
    url = f"https://{HOST}{p}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sec-check/1.0"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            body = r.read(300).decode("utf-8", "replace")
            print(f"  [OPEN ] {p:44s} {r.status}  {body[:100]}")
    except urllib.error.HTTPError as e:
        print(f"  [{e.code}   ] {p:44s}")
    except Exception as e:
        print(f"  [FAIL ] {p:44s} {type(e).__name__}")
