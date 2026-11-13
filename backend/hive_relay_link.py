"""Hive 中继客户端 —— 把公网中继当成「一个虚拟 peer」挂进 mesh。

为什么这么做
------------
`hive_mesh` 里所有「发消息」的路径最后都落到 `_fanout()`：遍历 `peers_of(gid)`，
对每个 peer 调 `send(typ, obj)`。所以只要造一个**长得像 Peer** 的对象塞进
`node.peers`，并且让它的 `send()` 走中继 socket 而不是直连 socket，
那么——

    · 文字消息 / @hd 流式进度 / 附件分片 / 成员表 / 群删除
    · 落盘、mid 去重、T_SYNC 补发、成员合并

**全部原样复用，一行都不用改**。收到的帧也直接喂回 `node._dispatch()`，
和从真实 peer 收到时走的是同一条处理路径。

加密不变
--------
`RelayPeer` 仍然用 `H.group_key(gid)` 加密每一帧，中继转发的是密文。
中继既看不到内容，也无法伪造 —— 它没有群密钥。

重连
----
断线后按 1/2/4/8/15/30 秒退避重连。每次重连都带上本地记的 `last_seq`，
服务器会把缺的帧补回来，所以断线期间的消息不会丢。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional

import backend.hive as H
import backend.hive_mesh as MESH
from backend import ws_min

PROTO = 1
HDR = struct.Struct(">IB")
K_CTRL = 0
K_FRAME = 1
K_SEQ = 2
MAX_PKT = 9 * 1024 * 1024


# ---------------------------------------------------------------- 底层

def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_pkt(sock: socket.socket):
    hdr = _recv_exact(sock, HDR.size)
    if not hdr:
        return None
    ln, kind = HDR.unpack(hdr)
    if ln <= 0 or ln > MAX_PKT:
        return None
    body = _recv_exact(sock, ln)
    if body is None:
        return None
    return kind, body


def _send_pkt(sock: socket.socket, kind: int, payload: bytes) -> bool:
    try:
        sock.sendall(HDR.pack(len(payload), kind) + payload)
        return True
    except Exception:
        return False


def _frame_bytes(typ: int, rk: bytes, key: bytes, obj: Any) -> Optional[bytes]:
    """按 mesh 的帧格式打包并加密（和 Peer.send 里做的完全一样）。"""
    if MESH.AESGCM is None or not key:
        return None
    nonce = os.urandom(12)
    pt = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        ct = MESH.AESGCM(key).encrypt(nonce, pt, None)
    except Exception:
        return None
    body = nonce + ct
    if len(body) > MESH.MAX_FRAME:
        return None
    return bytes([typ]) + rk + struct.pack(">I", len(body)) + body


def _split_frame(frame: bytes):
    """把 mesh 帧拆成 (typ, rk, body)。格式不对返回 None。"""
    if len(frame) < 9:
        return None
    typ = frame[0]
    rk = frame[1:5]
    ln = struct.unpack(">I", frame[5:9])[0]
    if ln <= 0 or 9 + ln > len(frame):
        return None
    return typ, rk, frame[9:9 + ln]


def parse_url(url: str) -> Dict[str, Any]:
    """把用户填的地址解析成 host/port/scheme。

    支持：
      hive4://1.2.3.4:18765      裸 TCP（默认端口 8765，最快）
      tcp://1.2.3.4:18765        同上
      1.2.3.4:18765              同上
      ws://1.2.3.4/hive-relay    WebSocket（默认端口 80，走 nginx 反代）
      wss://example.com/hive     WebSocket over TLS（默认端口 443）

    ⚠️ 早期版本把 ws:// 当普通 TCP（只是剥掉前缀），因为当时中继只监听裸端口。
    现在真的做 WebSocket 握手了 —— 要挂到 nginx 后面绕开安全组，这是唯一的路。
    """
    u = (url or "").strip()
    if not u:
        return {"ok": False, "error": "地址是空的"}
    scheme = "hive4"
    for p in ("hive4://", "hive3://", "hive://", "ws://", "wss://", "tcp://"):
        if u.lower().startswith(p):
            scheme = p[:-3].lower()
            u = u[len(p):]
            break
    path = ""
    if "/" in u:
        u, _, tail = u.partition("/")
        path = "/" + tail
    if scheme == "ws":
        port = 80
    elif scheme == "wss":
        port = 443
    else:
        port = 8765
    host = u
    if ":" in u:
        h, _, ps = u.rpartition(":")
        host = h
        try:
            port = int(ps)
        except Exception:
            return {"ok": False, "error": "端口不是数字"}
    if not host:
        return {"ok": False, "error": "没有主机名"}
    if not (0 < port < 65536):
        return {"ok": False, "error": "端口超出范围"}
    return {"ok": True, "host": host, "port": port, "scheme": scheme,
            "ws": scheme in ("ws", "wss"), "tls": scheme == "wss",
            "path": path or "/hive-relay"}


def probe(url: str, token: str = "", timeout: float = 4.0) -> Dict[str, Any]:
    """连一下中继，取它的健康信息。用于「填地址前先测通不通」。"""
    p = parse_url(url)
    if not p.get("ok"):
        return p
    t0 = time.time()
    try:
        s = socket.create_connection((p["host"], p["port"]), timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "host": p["host"], "port": p["port"]}
    try:
        s.settimeout(timeout)
        got = _recv_pkt(s)
        if not got:
            return {"ok": False, "error": "连上了但没收到握手包"}
        kind, body = got
        nonce = ""
        try:
            c = json.loads(body.decode("utf-8"))
            nonce = str(c.get("nonce") or "")
        except Exception:
            pass
        _send_pkt(s, K_CTRL, json.dumps({"op": "health"}).encode("utf-8"))
        got = _recv_pkt(s)
        info: Dict[str, Any] = {}
        if got:
            try:
                info = json.loads(got[1].decode("utf-8"))
            except Exception:
                info = {}
        return {"ok": True, "ms": round((time.time() - t0) * 1000, 1),
                "host": p["host"], "port": p["port"],
                "auth_required": bool(nonce), "health": info}
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 虚拟 peer

class RelayPeer(MESH.Peer):
    """长得像 Peer，但 send() 走中继。

    `_fanout()` 会对它调 `send(typ, obj)` —— 于是发消息、发流、发附件、
    发成员表全部自动经过中继，不需要在 mesh 里加任何分支。
    """

    def __init__(self, link: "RelayLink", gid: str, key: bytes):
        super().__init__(link.sock or socket.socket(), (link.host, link.port), gid, out=True)
        self.link = link
        self.key = key
        self.uid = ""                 # 中继不是人，不参与成员表
        self.name = "中继"
        self.dev = "relay"
        self.ver = MESH.PROTO_VER
        self.alive = True
        self.synced = True

    def send(self, typ: int, obj: Any) -> bool:
        return self.link.send(typ, obj)

    def label(self) -> str:
        return "relay"

    def close(self) -> None:
        self.alive = False


# ---------------------------------------------------------------- 链路

class RelayLink:
    """一条到中继的长连接 + 自动重连 + seq 水位记录。"""

    def __init__(self, state_path: str = ""):
        self.url = ""
        self.host = ""
        self.port = 0
        self.ws = False                 # 走 WebSocket（挂 nginx 反代时）
        self.tls = False                # wss://
        self.ws_path = "/hive-relay"
        self.gid = ""
        self.token = ""
        self.sock: Optional[socket.socket] = None
        self.peer: Optional[RelayPeer] = None
        self.state = "offline"          # offline / connecting / online / error
        self.error = ""
        self.last_seq = 0
        self.server_seq = 0
        self.peers = 0
        self.rtt_ms = 0.0
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0
        self.connected_at = 0.0
        self.reconnects = 0
        self._stop = False
        self._send_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state_path = state_path
        self._seqs: Dict[str, int] = {}
        self._load_state()

    # -------------------------------------------------- 水位持久化

    def _load_state(self) -> None:
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._seqs = {str(k): int(v) for k, v in (d.get("seq") or {}).items()}
        except Exception:
            self._seqs = {}

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seq": self._seqs}, f)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def _note_seq(self, seq: int) -> None:
        if seq <= self.last_seq:
            return
        self.last_seq = seq
        if self.gid:
            self._seqs[self.gid] = seq
            if seq % 20 == 0:
                self._save_state()

    # -------------------------------------------------- 生命周期

    def start(self, url: str, gid: str, token: str = "") -> Dict[str, Any]:
        p = parse_url(url)
        if not p.get("ok"):
            self.state = "error"
            self.error = p.get("error", "地址不对")
            return self.status()
        g = H.load_group(gid)
        if not g:
            self.state = "error"
            self.error = "群不存在"
            return self.status()
        if not H.group_key(gid):
            self.state = "error"
            self.error = "这个群没有密钥"
            return self.status()

        self.stop()
        self.url = url.strip()
        self.host = p["host"]
        self.port = p["port"]
        self.gid = gid
        self.token = token or ""
        self.last_seq = int(self._seqs.get(gid) or 0)
        self.error = ""
        self._stop = False
        self.state = "connecting"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # 给第一次连接一点时间，让调用方拿到真实状态
        for _ in range(40):
            if self.state in ("online", "error"):
                break
            time.sleep(0.05)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop = True
        self._teardown()
        self.state = "offline"
        return self.status()

    def _teardown(self) -> None:
        peer = self.peer
        self.peer = None
        if peer is not None:
            peer.alive = False
            node = MESH.node()
            with MESH._LOCK:
                try:
                    node.peers.remove(peer)
                except ValueError:
                    pass
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    # -------------------------------------------------- 连接循环

    def _loop(self) -> None:
        backoff = [1, 2, 4, 8, 15, 30]
        i = 0
        while not self._stop:
            try:
                ok = self._connect_once()
            except Exception as e:
                self.error = "%s: %s" % (type(e).__name__, e)
                ok = False
            if ok:
                i = 0
                self._read_loop()
                self._teardown()
                if self._stop:
                    break
                self.state = "connecting"
                self.error = "连接断开，正在重连"
            else:
                self._teardown()
                if self._stop:
                    break
                self.state = "connecting" if self.state != "error" else "error"
            delay = backoff[min(i, len(backoff) - 1)]
            i += 1
            for _ in range(int(delay * 10)):
                if self._stop:
                    break
                time.sleep(0.1)

    def _connect_once(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=8.0)
        except Exception as e:
            self.state = "error"
            self.error = "连不上 %s:%s（%s）" % (self.host, self.port,
                                              type(e).__name__)
            return False
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        # 握手：先收 challenge，再回 HMAC
        got = _recv_pkt(sock)
        if not got:
            self.error = "中继没有响应握手"
            sock.close()
            return False
        try:
            ch = json.loads(got[1].decode("utf-8"))
        except Exception:
            self.error = "握手包解析失败"
            sock.close()
            return False
        nonce = str(ch.get("nonce") or "")
        mac = hmac.new(self.token.encode("utf-8"),
                       (nonce + "|" + self.gid).encode("utf-8"),
                       hashlib.sha256).hexdigest()
        t0 = time.time()
        hello = {"op": "hello", "gid": self.gid, "mac": mac,
                 "since": int(self.last_seq), "ver": PROTO}
        if not _send_pkt(sock, K_CTRL, json.dumps(hello).encode("utf-8")):
            self.error = "握手发送失败"
            sock.close()
            return False
        got = _recv_pkt(sock)
        if not got:
            self.error = "中继没回 ready"
            sock.close()
            return False
        try:
            r = json.loads(got[1].decode("utf-8"))
        except Exception:
            self.error = "ready 解析失败"
            sock.close()
            return False
        if r.get("op") == "err":
            self.state = "error"
            self.error = str(r.get("msg") or "中继拒绝了连接")
            sock.close()
            return False
        if r.get("op") != "ready":
            self.error = "中继回了意外的包"
            sock.close()
            return False

        self.rtt_ms = round((time.time() - t0) * 1000, 1)
        self.server_seq = int(r.get("seq") or 0)
        self.peers = int(r.get("peers") or 0)
        self.sock = sock

        # 把中继挂成一个 peer —— 从此 mesh 的所有发送都会经过它
        node = MESH.node()
        if not node.started:
            node.start(0, [self.gid])
        node.add_group(self.gid)
        key = H.group_key(self.gid) or b""
        peer = RelayPeer(self, self.gid, key)
        self.peer = peer
        with MESH._LOCK:
            node.peers = [p for p in node.peers if not isinstance(p, RelayPeer)]
            node.peers.append(peer)

        self.state = "online"
        self.error = ""
        self.connected_at = time.time()
        self.reconnects += 1 if self.reconnects else 0
        return True

    def _read_loop(self) -> None:
        sock = self.sock
        if sock is None:
            return
        node = MESH.node()
        key = H.group_key(self.gid) or b""
        while not self._stop and sock is not None:
            got = _recv_pkt(sock)
            if not got:
                break
            kind, payload = got
            if kind == K_CTRL:
                try:
                    c = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if c.get("op") == "pong":
                    self.rtt_ms = round((time.time() - float(c.get("t") or 0)) * 1000, 1)
                continue
            if kind != K_SEQ:
                continue
            if len(payload) < 8:
                continue
            seq = struct.unpack(">Q", payload[:8])[0]
            frame = payload[8:]
            self._note_seq(seq)
            self.frames_in += 1
            sp = _split_frame(frame)
            if not sp:
                continue
            typ, rk, body = sp
            # 路由键必须对得上本群，否则是串群，直接丢
            if rk != MESH._rk(self.gid):
                continue
            obj = MESH._open_frame(body, key)
            if not isinstance(obj, dict):
                continue
            peer = self.peer
            if peer is None:
                break
            try:
                node._dispatch(peer, typ, obj)
            except Exception:
                pass
        if not self._stop:
            self.state = "connecting"
            self.error = "连接断开，正在重连"

    # -------------------------------------------------- 发送

    def send(self, typ: int, obj: Any) -> bool:
        sock = self.sock
        if sock is None or self.state != "online":
            return False
        key = H.group_key(self.gid) or b""
        frame = _frame_bytes(typ, MESH._rk(self.gid), key, obj)
        if not frame:
            return False
        with self._send_lock:
            if not _send_pkt(sock, K_FRAME, frame):
                self.dropped += 1
                return False
        self.frames_out += 1
        return True

    def ping(self) -> bool:
        sock = self.sock
        if sock is None:
            return False
        with self._send_lock:
            return _send_pkt(sock, K_CTRL,
                             json.dumps({"op": "ping", "t": time.time()}).encode("utf-8"))

    # -------------------------------------------------- 状态

    def status(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "gid": self.gid,
            "state": self.state,
            "online": self.state == "online",
            "error": self.error,
            "rtt_ms": self.rtt_ms,
            "last_seq": self.last_seq,
            "server_seq": self.server_seq,
            "peers": self.peers,
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "dropped": self.dropped,
            "uptime": round(time.time() - self.connected_at, 1) if self.connected_at else 0,
            "has_token": bool(self.token),
        }


_LINK: Optional[RelayLink] = None
_LOCK = threading.Lock()


def link(state_path: str = "") -> RelayLink:
    global _LINK
    with _LOCK:
        if _LINK is None:
            _LINK = RelayLink(state_path)
        elif state_path and not _LINK._state_path:
            _LINK._state_path = state_path
            _LINK._load_state()
        return _LINK
