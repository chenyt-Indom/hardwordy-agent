# -*- coding: utf-8 -*-
"""临时验证脚本：reload nginx 并验证中继两条通道 + 现有站点没坏。

用完就删（不留在项目里）。
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import sys
import warnings

warnings.filterwarnings("ignore")

HOST = "139.199.69.88"
KEY = os.path.expanduser("~/.ssh/id_rsa")


def ssh():
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, 22, username="root", key_filename=KEY,
                timeout=15, allow_agent=False, look_for_keys=False)
    return cli


def sh(cli, cmd, t=40):
    i, o, e = cli.exec_command(cmd, timeout=t)
    return o.read().decode("utf-8", "replace").strip()


def ws_probe(host, port, path, tls=False, timeout=8):
    """做一次真实 WebSocket 握手，返回状态行。"""
    s = socket.create_connection((host, port), timeout=timeout)
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(s, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key))
    s.sendall(req.encode())
    s.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        c = s.recv(4096)
        if not c:
            break
        data += c
    s.close()
    return data.split(b"\r\n")[0].decode(errors="replace")


def main():
    cli = ssh()
    print("=== reload nginx ===")
    print(sh(cli, "nginx -t 2>&1 | tail -1 && systemctl reload nginx && sleep 1 && systemctl is-active nginx"))

    print("\n=== 服务器本机测 /hive-relay（走 nginx）===")
    print(sh(cli, "python3 /tmp/ws_probe.py"))

    print("\n=== 现有站点是否仍正常 ===")
    print(sh(cli, "curl -s -o /dev/null -w 'https_root=%{http_code}  ' -k https://127.0.0.1/ ; "
                  "curl -s -o /dev/null -w 'http_301=%{http_code}' -H 'Host: firefightai.top' http://127.0.0.1/"))

    print("\n=== 中继服务直连（服务器本机 18765）===")
    print(sh(cli, "ss -lnt | grep 18765 || echo '未监听'"))
    cli.close()

    print("\n=== 从本机（公网）测两条通道 ===")
    for label, fn in [
        ("ws://139.199.69.88/hive-relay", lambda: ws_probe(HOST, 80, "/hive-relay")),
        ("wss://firefightai.top/hive", lambda: ws_probe("firefightai.top", 443, "/hive", tls=True)),
        ("直连 139.199.69.88:18765", lambda: ws_probe(HOST, 18765, "/hive-relay")),
    ]:
        try:
            print("  %-32s -> %s" % (label, fn()))
        except Exception as e:
            print("  %-32s -> %s: %s" % (label, type(e).__name__, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
