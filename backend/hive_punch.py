# -*- coding: utf-8 -*-
"""NAT 穿透（UDP 打洞）+ 可靠 UDP 传输 —— 群聊跨网一体化的底层。

为什么需要它
------------
群聊原来是纯 TCP 直连：把「公网 IP:端口」给对方，对方直接连。但家用宽带
几乎都在 NAT 后面，外部根本连不进来，于是跨网必然超时。之前只能建议用户
装 Tailscale —— 那是把「跨网」这件事外包给第三方软件，项目本身并没有一体化。

这个模块把跨网能力做进项目里：
  1. 用公共 STUN 问出「本机对外是什么 IP:端口」；
  2. 双方各自向对方的这个地址发包，让两边的 NAT 都记下「我发过给对方」，
     之后对方的包就能进来（这就是打洞）；
  3. 打通后在 UDP 上跑一个轻量可靠层，对外表现成一个「像 socket 的对象」。

**不经过任何第三方服务器传数据**：STUN 只回答「我对外是什么地址」这一个问题，
聊天内容全程 AES-GCM 加密走双方自己的 UDP 通道。

设计要点
--------
* `ReliableUDPSock` 只提供 `sendall` / `recv` / `settimeout` / `close` /
  `shutdown` 五个方法 —— 刚好是 hive_mesh 用到的全部接口，所以
  `_serve` / `_read_loop` / `_send_frame` / `_read_frame` 一行都不用改。
* 可靠层：每片带序号，接收方回 ACK，超时重传；乱序缓存后按序交付；
  滑动窗口做流控。UDP 本身不保证到达和顺序，这一层补上。
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ------------------------------------------------------------------ 常量

PUNCH_MAGIC = b"HIVEPCH1"        # 打洞握手包前缀
STUN_MAGIC = b"\x21\x12\xa4\x42"  # STUN magic cookie

PKT_DATA = 1
PKT_ACK = 2
PKT_PING = 3
PKT_BYE = 4

HDR = struct.Struct(">BI")        # (type, seq)
FRAG = 1000                       # 单片负载上限（UDP 安全 MTU）
WINDOW = 256                      # 滑动窗口：最多 256 片未确认
RTO = 0.35                        # 重传超时（秒）
MAX_TRIES = 12                    # 单片最多重传次数

# 公共 STUN 服务器。只用来问「我对外是什么地址」，不传任何聊天数据。
# 顺序按国内可达性排（实测 stun.l.google.com 和 bilibili 的都能通）。
STUN_SERVERS: List[Tuple[str, int]] = [
    ("stun.chat.bilibili.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.miwifi.com", 3478),
]


# ------------------------------------------------------------------ STUN

def parse_stun(data: bytes) -> Optional[Tuple[str, int]]:
    """从 STUN Binding Response 里取 (公网IP, 公网端口)。"""
    if len(data) < 20 or data[4:8] != STUN_MAGIC:
        return None
    pos = 20
    total = len(data)
    while pos + 4 <= total:
        atype, alen = struct.unpack(">HH", data[pos:pos + 4])
        val = data[pos + 4:pos + 4 + alen]
        if atype == 0x0020 and alen >= 8:          # XOR-MAPPED-ADDRESS
            xport = struct.unpack(">H", val[2:4])[0] ^ 0x2112
            xip = bytes(b ^ m for b, m in zip(val[4:8], STUN_MAGIC))
            return (socket.inet_ntoa(xip), xport)
        if atype == 0x0001 and alen >= 8:          # MAPPED-ADDRESS（老式）
            port = struct.unpack(">H", val[2:4])[0]
            return (socket.inet_ntoa(val[4:8]), port)
        pos += 4 + alen + ((4 - alen % 4) % 4 if alen % 4 else 0)
    return None


def stun_request(txid: Optional[bytes] = None) -> bytes:
    return struct.pack(">HHI", 0x0001, 0, 0x2112A442) + (txid or os.urandom(12))


# ------------------------------------------------------------------ 可靠 UDP

class ReliableUDPSock:
    """把一个 UDP 打洞通道包装成「像 TCP socket 一样」的可靠字节流。

    只需要 hive_mesh 用到的那几个方法，所以上层逻辑完全不用改。
    """

    def __init__(self, udp: socket.socket, peer: Tuple[str, int],
                 name: str = "") -> None:
        self.udp = udp
        self.peer = (peer[0], int(peer[1]))
        self.name = name
        self.alive = True
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._pend: Dict[int, list] = {}      # seq -> [pkt, ts, tries]
        self._seq = 0
        self._expect = 1
        self._ooo: Dict[int, bytes] = {}      # 乱序缓存
        self._timeout = 60.0
        self._send_lock = threading.Lock()
        self._closed_at = 0.0
        self.stats = {"sent": 0, "recv": 0, "retx": 0, "drop": 0}
        threading.Thread(target=self._retx_loop, daemon=True).start()

    # -------------------------------------------------- 对外：像 socket

    def sendall(self, data: bytes) -> None:
        if not self.alive:
            raise OSError("打洞通道已关闭")
        view = memoryview(data)
        i = 0
        while i < len(view):
            chunk = bytes(view[i:i + FRAG])
            i += FRAG
            # 流控：窗口满了就等 ACK 回来，避免一次性灌爆链路
            with self._cv:
                while len(self._pend) >= WINDOW and self.alive:
                    self._cv.wait(0.5)
            if not self.alive:
                raise OSError("打洞通道已关闭")
            with self._send_lock:
                self._seq += 1
                seq = self._seq
                pkt = HDR.pack(PKT_DATA, seq) + chunk
                self._pend[seq] = [pkt, time.time(), 0]
            try:
                self.udp.sendto(pkt, self.peer)
                self.stats["sent"] += 1
            except Exception:
                pass

    def recv(self, n: int) -> bytes:
        deadline = time.time() + (self._timeout if self._timeout else 60.0)
        with self._cv:
            while len(self._buf) < n:
                if not self.alive and not self._buf:
                    return b""
                left = deadline - time.time()
                if left <= 0:
                    if self._buf:
                        out = bytes(self._buf)
                        self._buf.clear()
                        return out
                    raise socket.timeout("打洞通道读取超时")
                self._cv.wait(min(left, 0.5))
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def settimeout(self, t: Optional[float]) -> None:
        self._timeout = float(t or 0)

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self._closed_at = time.time()
        try:
            self.udp.sendto(HDR.pack(PKT_BYE, 0), self.peer)
        except Exception:
            pass
        with self._cv:
            self._cv.notify_all()

    def shutdown(self, how: Any = None) -> None:
        self.close()

    # -------------------------------------------------- 内部

    def _ack(self, seq: int) -> None:
        try:
            self.udp.sendto(HDR.pack(PKT_ACK, seq), self.peer)
        except Exception:
            pass

    def feed(self, typ: int, seq: int, payload: bytes, addr: Any = None) -> None:
        """由 PunchNode 的接收循环把包喂进来。"""
        if typ == PKT_DATA:
            self._ack(seq)
            with self._cv:
                if seq == self._expect:
                    self._buf += payload
                    self._expect += 1
                    while self._expect in self._ooo:
                        self._buf += self._ooo.pop(self._expect)
                        self._expect += 1
                    self._cv.notify_all()
                elif seq > self._expect:
                    if len(self._ooo) < 8192:
                        self._ooo[seq] = payload
                    else:
                        self.stats["drop"] += 1
                # seq < expect：重复包，忽略
            self.stats["recv"] += 1
        elif typ == PKT_ACK:
            with self._cv:
                self._pend.pop(seq, None)
                self._cv.notify_all()
        elif typ == PKT_PING:
            self._ack(seq)
        elif typ == PKT_BYE:
            self.close()

    def _retx_loop(self) -> None:
        while self.alive:
            time.sleep(0.12)
            now = time.time()
            with self._send_lock:
                items = list(self._pend.items())
            for seq, ent in items:
                if not self.alive:
                    return
                pkt, ts, tries = ent
                if now - ts < RTO:
                    continue
                if tries >= MAX_TRIES:
                    # 对端长时间不确认，认定链路已断
                    self.close()
                    return
                ent[1] = now
                ent[2] = tries + 1
                try:
                    self.udp.sendto(pkt, self.peer)
                    self.stats["retx"] += 1
                except Exception:
                    pass

    def label(self) -> str:
        return self.name or ("%s:%s" % self.peer)


# ------------------------------------------------------------------ 打洞节点

class PunchNode:
    """管理本机的打洞 UDP 端口：查映射地址、主动打洞、被动接受打洞。

    被动收到别人的打洞请求时，会把建立好的 `ReliableUDPSock` 通过
    `on_tunnel` 回调交给上层（hive_mesh 拿它去跑正常的握手流程）。
    """

    def __init__(self) -> None:
        self.udp: Optional[socket.socket] = None
        self.port = 0
        self._stop = False
        self._lock = threading.Lock()
        self.tunnels: Dict[Tuple[str, int], ReliableUDPSock] = {}
        self.on_tunnel: Optional[Callable[[ReliableUDPSock, Tuple[str, int]], None]] = None
        self._mapped: Optional[Dict[str, Any]] = None
        self._stun_lock = threading.Lock()
        self._stun_evt: Optional[threading.Event] = None
        self._stun_res: Optional[Tuple[str, int]] = None
        self._punch_ok: Dict[Tuple[str, int], Tuple[str, int]] = {}
        self._punch_seen: Dict[Tuple[str, int], float] = {}
        self._last_err = ""

    # -------------------------------------------------- 生命周期

    def start(self, port: int = 0) -> Dict[str, Any]:
        if self.udp:
            return {"ok": True, "port": self.port}
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    u.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except Exception:
                    pass
            u.bind(("0.0.0.0", int(port or 0)))
            u.settimeout(0.3)
        except Exception as e:
            self._last_err = str(e)
            return {"ok": False, "error": "打洞端口绑定失败：%s" % e}
        self.udp = u
        self.port = u.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._recv_loop, daemon=True).start()
        return {"ok": True, "port": self.port}

    def stop(self) -> None:
        self._stop = True
        with self._lock:
            for t in list(self.tunnels.values()):
                t.close()
            self.tunnels.clear()
        if self.udp:
            try:
                self.udp.close()
            except Exception:
                pass
            self.udp = None

    # -------------------------------------------------- 映射地址

    def mapped(self, timeout: float = 2.0, force: bool = False) -> Optional[Dict[str, Any]]:
        """查本机的公网映射地址（带 5 分钟缓存）。

        必须用**打洞用的那个 socket** 去查 —— 换一个 socket 查出来的是另一个
        映射端口，拿它去打洞是打不通的。
        """
        if not self.udp:
            return None
        if not force and self._mapped and (time.time() - self._mapped["ts"] < 300):
            return self._mapped
        got = self._stun_lookup(timeout)
        if got:
            self._mapped = {"ip": got[0], "port": got[1], "ts": time.time()}
            return self._mapped
        return None

    def _stun_lookup(self, timeout: float = 2.0) -> Optional[Tuple[str, int]]:
        with self._stun_lock:
            for host, port in STUN_SERVERS:
                evt = threading.Event()
                self._stun_evt = evt
                self._stun_res = None
                try:
                    self.udp.sendto(stun_request(), (host, port))  # type: ignore
                except Exception:
                    continue
                if evt.wait(timeout):
                    if self._stun_res:
                        return self._stun_res
            return None

    # -------------------------------------------------- 打洞

    def connect(self, host: str, port: int, timeout: float = 10.0) -> Dict[str, Any]:
        """主动向 host:port 打洞。成功返回 {"ok": True, "sock": ReliableUDPSock}。"""
        if not self.udp:
            return {"ok": False, "error": "打洞通道没启动"}
        peer = (str(host), int(port))
        if peer[0] in ("127.0.0.1", "localhost") and self.port == peer[1]:
            return {"ok": False, "error": "不能连自己"}
        with self._lock:
            t = self.tunnels.get(peer)
            if t and t.alive:
                return {"ok": True, "sock": t, "reused": True}
        token = os.urandom(4)
        hello = PUNCH_MAGIC + b"Q" + token
        deadline = time.time() + max(3.0, float(timeout))
        tries = 0
        while time.time() < deadline and not self._stop:
            try:
                self.udp.sendto(hello, peer)
                tries += 1
            except Exception as e:
                self._last_err = str(e)
            with self._lock:
                if peer in self._punch_ok:
                    self._punch_ok.pop(peer, None)
                    break
            time.sleep(0.12)
        with self._lock:
            t = self.tunnels.get(peer)
            if t and t.alive:
                return {"ok": True, "sock": t, "tries": tries}
        return {
            "ok": False,
            "error": ("打洞超时：%s:%s 在 %.0f 秒内没有回应。"
                      "如果对方也是家用宽带，请让对方也把你的地址填进去、两边同时点连接。"
                      % (peer[0], peer[1], timeout)),
            "tries": tries,
        }

    def tunnel_for(self, addr: Tuple[str, int]) -> Optional[ReliableUDPSock]:
        with self._lock:
            return self.tunnels.get((addr[0], int(addr[1])))

    # -------------------------------------------------- 接收循环

    def _recv_loop(self) -> None:
        while not self._stop and self.udp:
            try:
                data, addr = self.udp.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                continue
            key = (addr[0], int(addr[1]))
            # 1) STUN 响应
            if len(data) >= 20 and data[4:8] == STUN_MAGIC:
                evt = self._stun_evt
                if evt and not evt.is_set():
                    res = parse_stun(data)
                    if res:
                        self._stun_res = res
                        evt.set()
                continue
            # 2) 打洞握手
            if data.startswith(PUNCH_MAGIC):
                self._on_punch(data, key)
                continue
            # 3) 可靠层数据
            if len(data) < HDR.size:
                continue
            typ, seq = HDR.unpack(data[:HDR.size])
            with self._lock:
                t = self.tunnels.get(key)
            if t and t.alive:
                t.feed(typ, seq, data[HDR.size:], key)
            else:
                # 没有对应通道：回一个 BYE，让对方早点放弃
                try:
                    self.udp.sendto(HDR.pack(PKT_BYE, 0), key)
                except Exception:
                    pass

    def _on_punch(self, data: bytes, key: Tuple[str, int]) -> None:
        kind = data[len(PUNCH_MAGIC):len(PUNCH_MAGIC) + 1]
        if kind == b"Q":
            # 对方在找我：回一个响应，并把通道建起来
            try:
                self.udp.sendto(PUNCH_MAGIC + b"R" + data[len(PUNCH_MAGIC) + 1:], key)  # type: ignore
            except Exception:
                pass
            with self._lock:
                self._punch_seen[key] = time.time()
                t = self.tunnels.get(key)
                fresh = not (t and t.alive)
                if fresh:
                    t = ReliableUDPSock(self.udp, key, name="%s:%s" % key)  # type: ignore
                    self.tunnels[key] = t
            if fresh and self.on_tunnel:
                try:
                    self.on_tunnel(t, key)
                except Exception:
                    pass
        elif kind == b"R":
            with self._lock:
                self._punch_ok[key] = key
                t = self.tunnels.get(key)
                if not (t and t.alive):
                    t = ReliableUDPSock(self.udp, key, name="%s:%s" % key)  # type: ignore
                    self.tunnels[key] = t

    # -------------------------------------------------- 状态

    def status(self) -> Dict[str, Any]:
        with self._lock:
            n = len([t for t in self.tunnels.values() if t.alive])
        m = self._mapped or {}
        return {
            "ok": bool(self.udp),
            "port": self.port,
            "mapped_ip": m.get("ip", ""),
            "mapped_port": m.get("port", 0),
            "mapped_addr": ("%s:%s" % (m.get("ip"), m.get("port"))) if m else "",
            "tunnels": n,
            "error": self._last_err,
        }
