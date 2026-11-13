"""极简 WebSocket（RFC 6455）—— 只做二进制帧，够把中继挂到 nginx 后面。

为什么需要它
------------
中继原生是裸 TCP 协议（hive4://），而 nginx 的 http 反代**只能转发
HTTP / WebSocket，转发不了裸 TCP**。要把中继挂到已放行的 80 端口上
（从而绕开「腾讯云安全组只放行固定端口」这个限制），两端就都得会说
WebSocket。服务器上不装任何第三方库，所以这里手写一份最小实现。

设计取舍
--------
· 只支持二进制帧（0x2）—— 中继传的是二进制协议包，不需要文本帧
· 支持 ping/pong/close 控制帧（nginx 默认 60 秒无数据会断，必须能应答）
· 不支持分片（fragmentation）—— 我们发出的包都小于 64KB，单帧足够；
  收到的分片帧按原样拼接（兼容性兜底）
· 不做扩展协商（permessage-deflate 之类）

用法
----
服务端：
    rest = ws_min.handshake_server(sock)      # 返回握手后残留的数据
    if rest is None: 说明不是 WS 请求，关掉
    s = ws_min.WSSock(sock, rest=rest, mask_out=False)

客户端：
    rest = ws_min.handshake_client(sock, "1.2.3.4:80", "/hive-relay")
    if rest is None: 握手失败
    s = ws_min.WSSock(sock, rest=rest, mask_out=True)

`WSSock` 只暴露 `sendall` / `recv` / `settimeout` / `shutdown` / `close`，
和普通 socket 的行为一致 —— 上层协议代码一行都不用改。
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Optional

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 帧操作码
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _read_http_head(sock: socket.socket, limit: int = 16384):
    """读到 \\r\\n\\r\\n 为止。返回 (头部文本, 剩余字节) 或 None。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > limit:
            return None
        try:
            chunk = sock.recv(2048)
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def _accept_key(key: str) -> str:
    raw = hashlib.sha1((key + GUID).encode("latin-1")).digest()
    return base64.b64encode(raw).decode("ascii")


def handshake_server(sock: socket.socket) -> Optional[bytes]:
    """服务端握手。成功返回残留数据（可能是 b""），失败返回 None。"""
    got = _read_http_head(sock)
    if not got:
        return None
    head, rest = got
    lines = head.split("\r\n")
    if not lines or not lines[0].upper().startswith("GET "):
        return None
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    key = hdrs.get("sec-websocket-key")
    if not key:
        return None
    # 只接受 v13（所有现代客户端都用这个）
    if hdrs.get("sec-websocket-version", "13") != "13":
        return None
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n\r\n" % _accept_key(key)
    )
    try:
        sock.sendall(resp.encode("latin-1"))
    except Exception:
        return None
    return rest


def handshake_client(sock: socket.socket, host: str,
                     path: str = "/hive-relay") -> Optional[bytes]:
    """客户端握手。成功返回残留数据，失败返回 None。"""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n" % (path or "/", host, key)
    )
    try:
        sock.sendall(req.encode("latin-1"))
    except Exception:
        return None
    got = _read_http_head(sock)
    if not got:
        return None
    head, rest = got
    lines = head.split("\r\n")
    if not lines or "101" not in lines[0]:
        return None
    if _accept_key(key) not in head:
        return None
    return rest


def _encode(data: bytes, mask: bool, opcode: int = OP_BIN) -> bytes:
    """组一个完整的（不分片）帧。"""
    n = len(data)
    h = bytearray([0x80 | opcode])          # FIN=1
    mbit = 0x80 if mask else 0
    if n < 126:
        h.append(mbit | n)
    elif n < 65536:
        h.append(mbit | 126)
        h += struct.pack("!H", n)
    else:
        h.append(mbit | 127)
        h += struct.pack("!Q", n)
    if mask:
        mk = os.urandom(4)
        h += mk
        data = bytes(d ^ mk[i & 3] for i, d in enumerate(data))
    return bytes(h) + data


class WSSock:
    """把 WebSocket 帧包装成 socket 语义。

    mask_out=True 用于客户端（RFC 6455 要求客户端发出的帧必须掩码）。
    """

    def __init__(self, sock: socket.socket, rest: bytes = b"",
                 mask_out: bool = False):
        self._s = sock
        self._buf = bytearray(rest or b"")
        self._mask_out = bool(mask_out)
        self._wlock = threading.Lock()
        self._closed = False

    # ---------------------------------------------------------- socket 语义
    def settimeout(self, t) -> None:
        try:
            self._s.settimeout(t)
        except Exception:
            pass

    def setsockopt(self, *a) -> None:
        try:
            self._s.setsockopt(*a)
        except Exception:
            pass

    def sendall(self, data: bytes) -> None:
        with self._wlock:
            self._s.sendall(_encode(data, self._mask_out))

    def recv(self, n: int) -> bytes:
        if n <= 0:
            return b""
        while len(self._buf) < n:
            if not self._fill():
                return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def shutdown(self, how=socket.SHUT_RDWR) -> None:
        try:
            self._s.shutdown(how)
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._s.close()
        except Exception:
            pass

    # ---------------------------------------------------------- 内部
    def _recvn(self, n: int):
        buf = b""
        while len(buf) < n:
            try:
                c = self._s.recv(n - len(buf))
            except Exception:
                return None
            if not c:
                return None
            buf += c
        return buf

    def _fill(self) -> bool:
        """读一个帧，把载荷塞进 _buf。控制帧就地处理。"""
        while True:
            hdr = self._recvn(2)
            if not hdr:
                return False
            b1, b2 = hdr[0], hdr[1]
            op = b1 & 0x0F
            masked = bool(b2 & 0x80)
            ln = b2 & 0x7F
            if ln == 126:
                ext = self._recvn(2)
                if not ext:
                    return False
                ln = struct.unpack("!H", ext)[0]
            elif ln == 127:
                ext = self._recvn(8)
                if not ext:
                    return False
                ln = struct.unpack("!Q", ext)[0]
            if ln > 64 * 1024 * 1024:       # 防炸内存
                return False
            mask = b""
            if masked:
                mask = self._recvn(4)
                if not mask:
                    return False
            data = self._recvn(ln) if ln else b""
            if data is None:
                return False
            if masked and data:
                data = bytes(d ^ mask[i & 3] for i, d in enumerate(data))

            if op == OP_CLOSE:
                return False
            if op == OP_PING:
                try:
                    with self._wlock:
                        self._s.sendall(_encode(data, self._mask_out, OP_PONG))
                except Exception:
                    return False
                continue
            if op == OP_PONG:
                continue
            # 数据帧（含续帧）：原样拼接
            self._buf += data
            return True
