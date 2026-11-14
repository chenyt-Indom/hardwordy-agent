# -*- coding: utf-8 -*-
"""实测各条候选通路到底通不通 —— 决定备用线路怎么排。"""
import socket
import ssl
import time
import sys

HOST = "139.199.69.88"


def tcp(host, port, timeout=4.0):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, round((time.time() - t0) * 1000, 1), ""
    except Exception as e:
        return False, round((time.time() - t0) * 1000, 1), "%s: %s" % (type(e).__name__, e)


def ws_handshake(host, port, path, tls=False, timeout=5.0):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                break
            buf += c
        s.close()
        line = buf.split(b"\r\n")[0].decode("latin1", "replace")
        ok = "101" in line
        return ok, round((time.time() - t0) * 1000, 1), line[:60]
    except Exception as e:
        return False, round((time.time() - t0) * 1000, 1), "%s: %s" % (type(e).__name__, e)


def dns(name):
    try:
        t0 = time.time()
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(name, None)})
        return ips, round((time.time() - t0) * 1000, 1)
    except Exception as e:
        return [], str(e)


print("=" * 62)
print("DNS 解析")
print("=" * 62)
for h in (HOST, "example.com"):
    ips, ms = dns(h)
    print("  %-18s -> %s  (%s ms)" % (h, ips or "失败", ms))

print()
print("=" * 62)
print("裸 TCP 端口")
print("=" * 62)
for port in (22, 80, 443, 18765, 8765, 8080):
    ok, ms, err = tcp(HOST, port)
    print("  :%-6s %-4s %7s ms  %s" % (port, "通" if ok else "断", ms, err))

print()
print("=" * 62)
print("WebSocket 路径")
print("=" * 62)
for path in ("/hive-relay", "/hive", "/"):
    ok, ms, info = ws_handshake(HOST, 80, path)
    print("  ws  :80%-14s %-4s %7s ms  %s" % (path, "通" if ok else "断", ms, info))
for path in ("/hive-relay", "/hive"):
    ok, ms, info = ws_handshake(HOST, 443, path, tls=True)
    print("  wss :443%-13s %-4s %7s ms  %s" % (path, "通" if ok else "断", ms, info))

print()
print("=" * 62)
print("稳定性：主线路连测 5 次")
print("=" * 62)
for i in range(5):
    ok, ms, info = ws_handshake(HOST, 80, "/hive-relay")
    print("  第%d次 %-4s %7s ms  %s" % (i + 1, "通" if ok else "断", ms, info))
    time.sleep(0.3)
