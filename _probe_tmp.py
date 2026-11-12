# 临时探测脚本：检查中继服务器可达性（用完即删）
import socket
import time
import json

HOST = "139.199.69.88"
PORTS = [80, 8765, 443, 8080]

def tcp_probe(host, port, timeout=4.0):
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        ms = int((time.time() - t0) * 1000)
        return True, ms, ""
    except socket.timeout:
        return False, int((time.time() - t0) * 1000), "超时（被防火墙丢弃或不可达）"
    except ConnectionRefusedError:
        return False, int((time.time() - t0) * 1000), "端口关闭/无服务（refused）"
    except Exception as e:
        return False, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass

print(f"目标: {HOST}")
print("=" * 60)
for p in PORTS:
    ok, ms, err = tcp_probe(HOST, p)
    flag = "✅ 通" if ok else "❌ 不通"
    print(f"  {p:>5} : {flag}  {ms}ms  {err}")

print("\n--- HTTP /health 探测 (port 80) ---")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((HOST, 80))
    req = f"GET /health HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode())
    buf = b""
    while len(buf) < 4096:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    text = buf.decode("utf-8", "replace")
    print(text[:800])
except Exception as e:
    print(f"  HTTP 失败: {type(e).__name__}: {e}")

print("\n--- HTTP /health 探测 (port 8765) ---")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((HOST, 8765))
    req = f"GET /health HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode())
    buf = b""
    while len(buf) < 4096:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    print(buf.decode("utf-8", "replace")[:800])
except Exception as e:
    print(f"  HTTP 失败: {type(e).__name__}: {e}")
