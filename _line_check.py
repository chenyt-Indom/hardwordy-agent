# -*- coding: utf-8 -*-
"""验证 4 条 nginx 线路 + 清理临时文件"""
import sys, io, socket, base64, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

H = "139.199.69.88"

def ws_handshake(host, port, path, tls=False, timeout=8):
    """真实 WebSocket 握手，返回 (ok, 耗时ms, 说明)"""
    import time, ssl
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = s.recv(1024).decode('utf-8', 'replace')
        s.close()
        ms = (time.time() - t0) * 1000
        if "101" in resp.split("\r\n")[0]:
            return True, ms, "101 Switching Protocols"
        return False, ms, resp.split("\r\n")[0][:60]
    except Exception as ex:
        return False, (time.time()-t0)*1000, type(ex).__name__

lines = [
    ("ws://139.199.69.88/hive-relay",        H, 80,   "/hive-relay",   False),
    ("wss://139.199.69.88:443/hive-relay",   H, 443,  "/hive-relay",   True),
    ("wss://139.199.69.88:443/hive",         H, 443,  "/hive",         True),
    ("ws://139.199.69.88:8080/hive-relay",   H, 8080, "/hive-relay",   False),
    ("tcp://139.199.69.88:18765 (裸)",       H, 18765, None,           False),
]

print("=== 全部线路真实握手 ===")
ok_cnt = 0
for name, host, port, path, tls in lines:
    if path is None:
        try:
            s = socket.create_connection((host, port), timeout=6); s.close()
            print(f"  OK    {name}")
            ok_cnt += 1
        except Exception as ex:
            print(f"  FAIL  {name}  ({type(ex).__name__})")
        continue
    ok, ms, note = ws_handshake(host, port, path, tls)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {ms:.1f}ms  {note}")
    if ok: ok_cnt += 1

print(f"\n可用: {ok_cnt}/{len(lines)}")
