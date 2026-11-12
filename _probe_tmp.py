# 临时探测脚本：检查中继服务器可达性（用完即删）
import socket
import time
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = "139.199.69.88"
PORTS = [80, 8765, 443, 8080]


def tcp_probe(host, port, timeout=4.0):
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        ms = int((time.time() - t0) * 1000)
        return True, ms, "connected"
    except socket.timeout:
        return False, int((time.time() - t0) * 1000), "timeout (firewall drop / unreachable)"
    except ConnectionRefusedError:
        return False, int((time.time() - t0) * 1000), "refused (port closed, no service)"
    except Exception as e:
        return False, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_health(host, port, timeout=5.0):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        req = f"GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode())
        buf = b""
        while len(buf) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return buf.decode("utf-8", "replace")
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {e}"


print(f"TARGET: {HOST}")
print("=" * 60)
for p in PORTS:
    ok, ms, err = tcp_probe(HOST, p)
    flag = "OK  " if ok else "FAIL"
    print(f"  port {p:>5} : {flag}  {ms}ms  {err}")

for port in (80, 8765):
    print(f"\n--- HTTP GET /health on port {port} ---")
    print(http_health(HOST, port)[:700])
