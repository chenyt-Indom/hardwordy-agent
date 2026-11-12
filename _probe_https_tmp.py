# 临时探测脚本：HTTPS 各路径响应（用完即删）
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

PATHS = ["/health", "/hive", "/hive/health", "/", "/api/health"]

for p in PATHS:
    url = f"https://{HOST}{p}"
    print(f"\n=== GET {url} ===")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hive-probe/1.0"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            body = r.read(1200).decode("utf-8", "replace")
            print(f"  status={r.status}  server={r.headers.get('Server')}")
            print("  body:", body[:600])
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(600).decode("utf-8", "replace")
        except Exception:
            pass
        print(f"  HTTPError status={e.code}  server={e.headers.get('Server') if e.headers else '?'}")
        print("  body:", body[:500])
    except Exception as e:
        print(f"  FAILED {type(e).__name__}: {str(e)[:200]}")

# 顺便看看证书是谁签的
print("\n=== TLS certificate info ===")
try:
    import socket
    s = socket.create_connection((HOST, 443), timeout=8)
    ss = ctx.wrap_socket(s, server_hostname=HOST)
    cert = ss.getpeercert()
    print("  subject:", cert.get("subject"))
    print("  issuer:", cert.get("issuer"))
    print("  notAfter:", cert.get("notAfter"))
    ss.close()
except Exception as e:
    print("  cert read failed:", type(e).__name__, e)
