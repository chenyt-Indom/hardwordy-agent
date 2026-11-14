# -*- coding: utf-8 -*-
"""补测 8080 及其它可能的中转路径。"""
import socket
import ssl
import time

HOST = "139.199.69.88"


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
        return ("101" in line), round((time.time() - t0) * 1000, 1), line[:60]
    except Exception as e:
        return False, round((time.time() - t0) * 1000, 1), "%s: %s" % (type(e).__name__, e)


for port, path in ((8080, "/hive-relay"), (8080, "/hive"),
                   (443, "/hive-relay"), (80, "/hive-relay")):
    ok, ms, info = ws_handshake(HOST, port, path, tls=(port == 443))
    print("port=%-5s path=%-14s %-3s %7s ms  %s"
          % (port, path, "OK" if ok else "NO", ms, info))
