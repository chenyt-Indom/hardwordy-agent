"""Hive Mesh —— 无服务器的群聊网络（纯 P2P，替代中继）。

为什么要有这个文件
------------------
原来群聊靠一台中继服务器转发。但需求是「不要服务器」，而两台机器在公网上
互相找不到对方，所以必须自己解决「找到对方」和「消息怎么传」。

设计（每条都是为了绕开一个具体的坑）
------------------------------------
1. **一个端口，所有群共用**：进程只监听一个 TCP 端口，帧头 4 字节路由键
   （sha256(gid) 前 4 字节）区分是哪个群。否则每加一个群就要占一个端口。
2. **加密即认证**：消息用群密钥 AES-256-GCM 加密，能解开就说明对方有群密钥
   = 是群成员。不需要额外的登录/令牌机制，也杜绝了「知道端口就能连」。
3. **全连接 mesh + 泛洪**：每个人连上所有能连的人，消息发给所有人，收到后
   继续转发（TTL 限制 + mid 去重）。这样**任意一个人掉线都不影响其他人** ——
   不会出现「群主关机群就停」的单点。
4. **两种牵线方式**：局域网靠 UDP 广播自动发现（零配置）；跨网络靠连接码
   人工传一次（数据一字节都不经过任何服务器）。
5. **peer exchange**：连上一个人后互报自己认识谁，自动尝试直连更多成员。
   跨网络时只要连上一个人，剩下的会自动铺开。
6. **断线不丢**：连上后互报本地最大时间戳，把对方缺的补发过去。

线程模型
--------
· 一个 accept 线程
· 每个连接一个读线程
· 一个 UDP 广播线程
· 发送时用 per-peer 锁，避免半包交错
所有跨线程共享的状态用 _LOCK 保护。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import backend.hive as H
from backend import hive_punch as HP

logger = logging.getLogger("hive_mesh")

# 已经告警过的「不认识的帧类型」—— 同类只记一次，避免每帧刷屏。
# ★ 2026-11-13：两台机器的代码可能不一样（用户改完自开发、只在一台重启过），
#   新版会发出旧版不认识的帧。那种帧必须**安全忽略**，不能断连 ——
#   否则现象就是「代码一不同步，连上就掉」，看起来像联机失败。
_UNKNOWN_TYP: Set[int] = set()

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore

PROTO_VER = 3
MESH_PORT = 8899
MAX_FRAME = 8 * 1024 * 1024      # 单帧上限 8MB（消息/进度帧够用；大文件走 P2P 传输）
TTL_DEFAULT = 3                   # 泛洪最大跳数
BEACON_PORT = 45231               # UDP 发现端口（所有机器用同一个才能互相听到）
BEACON_EVERY = 3.0                # UDP 广播间隔
PEER_TIMEOUT = 45.0               # 多久没收到心跳算掉线

# 帧类型
T_HELLO = 1     # 握手：我是谁、我在哪些群、我认识谁
T_MSG = 2       # 一条聊天消息（要落盘）
T_STREAM = 3    # @hd 实时进度（不落盘）
T_PING = 4
T_PONG = 5
T_PEERS = 6     # peer exchange
T_SYNC = 7      # 断线补发请求/应答
T_CANCEL = 8    # @hd 取消广播
T_BYE = 9       # 主动断开
T_FILE = 10     # 附件分片（512KB 一片，走泛洪）
T_MEMBERS = 11  # 成员表同步（谁在群里、什么角色）
T_PROBE = 12    # 入群探测：明文问「你有哪些群」，回 gid + 验证标签（不含密钥）
T_GONE = 13     # 群被群主删除：收到的一方把本地这个群也下线（移到 _deleted）

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 编解码

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


def _rk(gid: str) -> bytes:
    """路由键：gid 的 sha256 前 4 字节。用于在共用的端口上区分群。"""
    return hashlib.sha256(gid.encode("utf-8")).digest()[:4]


def _prefer_out(my_uid: str, peer_uid: str) -> bool:
    """一对 uid 之间，本机是否应该保留「自己主动连出去」的那条连接。

    规则：uid 字典序较小的一方保留 out 连接（也就是由它发起）。

    ⚠️ 2026-11-13 修：这条规则必须**双方算出来是同一条 TCP**，否则会出事。
    原来只是「同 uid 已有连接就关掉新来的」，结果两边各自关掉了不同方向的
    那一条：A 留下 A→B，B 留下 B→A，两条连接的「另一半」都被关掉了。
    于是双方都往对方已经关闭的 socket 里写 —— sendall 成功、对端永远收不到，
    消息、AI 回答、附件全部静默丢失。实测：peer 显示 alive，broadcast 返回 1，
    但对端 seen 为空、一条都没落盘。
    """
    return str(my_uid or "") < str(peer_uid or "")


def _send_frame(sock: socket.socket, typ: int, rk: bytes, key: bytes, obj: Any) -> bool:
    if AESGCM is None:
        return False
    nonce = os.urandom(12)
    pt = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        ct = AESGCM(key).encrypt(nonce, pt, None)
    except Exception:
        return False
    body = nonce + ct
    if len(body) > MAX_FRAME:
        return False
    pkt = bytes([typ]) + rk + struct.pack(">I", len(body)) + body
    try:
        sock.sendall(pkt)
        return True
    except Exception:
        return False


def _read_frame(sock: socket.socket) -> Optional[Tuple[int, bytes, bytes]]:
    """读一帧，返回 (类型, 路由键, 密文体)。不解密 —— 解密要由调用方按路由键找密钥。"""
    hdr = _recv_exact(sock, 9)
    if not hdr:
        return None
    typ = hdr[0]
    rk = hdr[1:5]
    ln = struct.unpack(">I", hdr[5:9])[0]
    if ln <= 0 or ln > MAX_FRAME:
        return None
    body = _recv_exact(sock, ln)
    if body is None:
        return None
    return typ, rk, body


def _send_plain(sock: socket.socket, typ: int, obj: Any) -> bool:
    """发一帧**不加密**的内容。只用于入群探测 —— 里面不含密钥、不含消息。"""
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        pkt = bytes([typ]) + b"\x00\x00\x00\x00" + struct.pack(">I", len(body)) + body
        sock.sendall(pkt)
        return True
    except Exception:
        return False


def _open_frame(body: bytes, key: bytes) -> Optional[Any]:
    if AESGCM is None or len(body) < 13:
        return None
    nonce, ct = body[:12], body[12:]
    try:
        pt = AESGCM(key).decrypt(nonce, ct, None)
        return json.loads(pt.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- 本机地址

def lan_ip() -> str:
    """拿本机在局域网里的地址。用 UDP connect 一个外部地址来探，不会真发包。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def tailscale_ip() -> str:
    """找 Tailscale 给本机分配的地址（100.64.0.0/10）。没装就返回空串。

    装了 Tailscale 之后，两台异地机器等于处在同一个局域网里，
    直接连这个 100.x 地址就能通，不需要公网 IP、不需要端口映射、不需要中继。
    """
    try:
        import psutil
        for _name, lst in psutil.net_if_addrs().items():
            for a in lst:
                if getattr(a.family, "name", "") != "AF_INET":
                    continue
                ip = a.address or ""
                parts = ip.split(".")
                if len(parts) != 4:
                    continue
                try:
                    second = int(parts[1])
                except Exception:  # noqa: BLE001
                    continue
                # 100.64.0.0/10 —— 运营商级 NAT 段，Tailscale 就用它
                if parts[0] == "100" and 64 <= second <= 127:
                    return ip
    except Exception:  # noqa: BLE001
        pass
    return ""


_PUB_CACHE = {"ip": "", "ts": 0.0}


def _public_ip() -> str:
    """尽力探公网 IP（失败就返回空，不阻塞主流程）。

    结果带缓存（2026-11-13）：「📡 我的地址」弹窗每次打开都会走到这里，
    不缓存的话每点一次都要干等最多 2.5 秒。成功缓存 10 分钟，
    失败只缓存 60 秒 —— 万一只是当时断了一下网，不至于十分钟都显示「没探到」。
    """
    now = time.time()
    ttl = 600 if _PUB_CACHE["ip"] else 60
    if _PUB_CACHE["ts"] and (now - _PUB_CACHE["ts"]) < ttl:
        return _PUB_CACHE["ip"]
    ip = ""
    # 多源依次试（2026-11-13 实测）：api.ipify.org 在国内会被「远程主机强迫关闭连接」，
    # 只写它一个的话，国内用户永远看不到「跨网络」地址。
    for url in ("https://ipv4.icanhazip.com",
                "http://ip.3322.net",
                "https://api.ipify.org"):
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=2.5) as r:
                s = r.read(64).decode("utf-8", "ignore").strip()
            # 有些源会带换行或多余文字，只认「纯 IPv4」那种形态
            s = s.split()[0] if s else ""
            parts = s.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255
                                       for p in parts):
                ip = s
                break
        except Exception:
            continue
    _PUB_CACHE["ip"] = ip
    _PUB_CACHE["ts"] = now
    return ip


# ---------------------------------------------------------------- Peer

class Peer:
    """一条到别人的 TCP 连接。可能是我们连出去的，也可能是别人连进来的。"""

    def __init__(self, sock: socket.socket, addr: Tuple[str, int], gid: str,
                 out: bool = False) -> None:
        self.sock = sock
        self.addr = addr
        self.gid = gid
        self.out = out
        self.uid = ""
        self.name = ""
        self.dev = ""
        self.ver = 0
        self.port = 0
        self.alive = True
        self.last_seen = time.time()
        self.send_lock = threading.Lock()
        self.key: bytes = b""
        self.synced = False

    def send(self, typ: int, obj: Any) -> bool:
        if not self.alive or not self.key:
            return False
        with self.send_lock:
            return _send_frame(self.sock, typ, _rk(self.gid), self.key, obj)

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def label(self) -> str:
        return self.uid or ("%s:%s" % self.addr)


# ---------------------------------------------------------------- MeshNode

class MeshNode:
    def __init__(self) -> None:
        self.port = 0
        self.started = False
        self.uid = ""
        self.uname = ""
        self.dev = ""            # 本机设备标识（见 H.dev_id）
        self.uid_conflict = None  # 发现「同 user_id 的另一台机器」时记在这里
        self._beacon_port = BEACON_PORT
        self.groups: Set[str] = set()
        self.peers: List[Peer] = []
        self.seen: Dict[str, Set[str]] = {}       # gid -> 见过的 mid
        self.vts: Dict[str, str] = {}             # gid -> 验证标签（随广播带出去）
        self.gnames: Dict[str, str] = {}          # gid -> 群名（随广播带出去，方便认群）
        self.beacons: Dict[str, Dict[str, Any]] = {}  # gid -> 最近一次收到的广播
        self.known: Dict[str, Dict[str, Any]] = {}  # uid -> {host, port, name, gid}
        self.outbox: List[Dict[str, Any]] = []    # 断线时排队的消息
        self._retry: Dict[str, float] = {}        # uid -> 上次尝试重连的时间
        self._full_req: Set[str] = set()          # 已向别人请求过「全量历史」的 gid
        self._last_sync: Dict[str, Any] = {}      # 最近一次历史补发的记录（排查用）
        self._srv: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        # UDP 打洞通道：跨网络时靠它把 TCP 直连不到的双方连起来。
        # 独立于上面的 _udp（那个只做局域网广播，绑在固定端口上）。
        self.punch: Optional[HP.PunchNode] = None
        self._stop = False
        self._last_err = ""
        self._started_at = 0.0
        self.on_msg = None      # Callable[[str, dict], None]
        self.on_stream = None   # Callable[[str, dict], None]
        self.on_cancel = None   # Callable[[str, dict], None]
        self.on_file = None     # Callable[[str, dict], None]  附件分片
        self.on_peer = None     # Callable[[], None]
        self.on_members = None  # Callable[[str], None]  成员表有变化（收到别人的）
        self.on_gone = None     # Callable[[str, str], None]  某个群被群主删了（gid, 群名）

    # ------------------------------------------------------------ 生命周期

    def start(self, port: int = 0, groups: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.started:
            if groups:
                for g in groups:
                    self.add_group(g)
            return self.status()
        self._stop = False
        want = port or MESH_PORT
        srv = None
        last = ""
        for p in range(want, want + 12):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # ⚠️ Windows 上 SO_REUSEADDR 的语义和 Linux 不同：它允许**两个进程**
                # 绑同一个端口，后绑的那个会抢走流量。实测两个节点同时绑 8899 都成功，
                # 于是 A 连过去连到了自己 —— 消息发不出去，而且完全不报错。
                # 用 SO_EXCLUSIVEADDRUSE 才是「端口真的被占了就失败」，递增重试才有意义。
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", p))
                s.listen(32)
                s.settimeout(1.0)
                srv = s
                self.port = p
                break
            except Exception as e:
                last = str(e)
                try:
                    s.close()
                except Exception:
                    pass
        if not srv:
            self._last_err = "端口被占用：%s" % last
            return self.status()
        self._srv = srv
        self.started = True
        self._started_at = time.time()
        _me = H.profile()
        self.uid = str(_me.get("user_id") or "")
        self.uname = str(_me.get("name") or "")
        self.dev = H.dev_id()
        for g in (groups or []):
            self.groups.add(g)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._beacon_loop, daemon=True).start()
        threading.Thread(target=self._housekeep_loop, daemon=True).start()
        self._start_punch()
        return self.status()

    def _start_punch(self) -> None:
        """启动 UDP 打洞通道（跨网络用）。失败不影响局域网/公网直连。"""
        if self.punch:
            return
        try:
            node = HP.PunchNode()
            node.on_tunnel = self._on_punch_tunnel
            r = node.start(0)
            if not r.get("ok"):
                self._last_err = "打洞通道启动失败：%s" % r.get("error")
                return
            self.punch = node
            # 后台查一次公网映射地址，让「我的地址」立刻有东西可显示。
            # 不阻塞 start()：STUN 偶尔要等 1~2 秒，卡在这里会让进群变慢。
            threading.Thread(target=node.mapped, kwargs={"timeout": 2.5},
                             daemon=True).start()
        except Exception as e:  # noqa: BLE001
            self._last_err = "打洞通道启动异常：%s" % e

    def _on_punch_tunnel(self, sock, addr) -> None:
        """别人打洞打进来了：当成一条入站连接交给正常的握手流程。

        ReliableUDPSock 提供了 sendall/recv/settimeout/close/shutdown，
        和 socket 的接口一致，所以 _serve 一行都不用改。
        """
        try:
            sock.settimeout(60.0)
        except Exception:
            pass
        threading.Thread(target=self._serve, args=(sock, addr, True),
                         daemon=True).start()

    def stop(self) -> Dict[str, Any]:
        self._stop = True
        with _LOCK:
            for p in list(self.peers):
                try:
                    p.send(T_BYE, {"uid": self.uid})
                except Exception:
                    pass
                p.close()
            self.peers.clear()
        for s in (self._srv, self._udp):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        self._srv = None
        self._udp = None
        if self.punch:
            try:
                self.punch.stop()
            except Exception:
                pass
            self.punch = None
        self.started = False
        self.groups.clear()
        return self.status()

    def add_group(self, gid: str, vt: str = "", gname: str = "") -> None:
        with _LOCK:
            self.groups.add(gid)
            if vt:
                self.vts[gid] = vt
            if gname:
                self.gnames[gid] = gname

    def remove_group(self, gid: str) -> None:
        with _LOCK:
            self.groups.discard(gid)
            for p in list(self.peers):
                if p.gid == gid:
                    p.close()
                    try:
                        self.peers.remove(p)
                    except ValueError:
                        pass

    # ------------------------------------------------------------ 状态

    def status(self) -> Dict[str, Any]:
        with _LOCK:
            ps = [p for p in self.peers if p.alive]
            members: Dict[str, Dict[str, Any]] = {}
            for p in ps:
                if p.uid:
                    members[p.uid] = {
                        "user_id": p.uid,
                        "name": p.name or p.uid,
                        "ver": p.ver,
                        "addr": "%s:%s" % p.addr,
                        "online": True,
                        "last_seen": p.last_seen,
                    }
            return {
                "ok": True,
                "started": self.started,
                "port": self.port,
                "lan_ip": lan_ip(),
                "groups": sorted(self.groups),
                "peers": len(ps),
                "members": list(members.values()),
                "queued": len(self.outbox),
                "proto": PROTO_VER,
                "error": self._last_err,
                "uptime": int(time.time() - self._started_at) if self._started_at else 0,
                "dev": self.dev,
                "uid_conflict": self.uid_conflict,
            }

    def refresh_identity(self) -> Dict[str, Any]:
        """换身份后重新读一次（user_id 变了要立刻生效，否则还按老身份发消息）。"""
        _me = H.profile()
        self.uid = str(_me.get("user_id") or "")
        self.uname = str(_me.get("name") or "")
        self.uid_conflict = None
        for gid in list(self.groups):
            try:
                self.broadcast_members(gid)
            except Exception:
                pass
        return self.status()

    def peers_of(self, gid: str) -> List[Peer]:
        with _LOCK:
            return [p for p in self.peers if p.alive and p.gid == gid]

    # ------------------------------------------------------------ 监听

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                c, addr = self._srv.accept()  # type: ignore
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                c.settimeout(60.0)
                threading.Thread(target=self._serve, args=(c, addr, True),
                                 daemon=True).start()
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass

    def _serve(self, sock: socket.socket, addr: Tuple[str, int], inbound: bool) -> None:
        """处理一条连接：先收 HELLO 确定是哪个群，再进入读循环。"""
        peer: Optional[Peer] = None
        try:
            fr = _read_frame(sock)
            if not fr:
                sock.close()
                return
            typ, rk, body = fr
            if typ == T_PROBE:
                # 明文探测：只回「我有哪些群 + 验证标签 + 群名」。
                # 用途：对方只知道地址和密码时，能确认「密码对不对、是哪个群」。
                # 这里**不回密钥、不回成员、不回任何消息**。
                with _LOCK:
                    info = [{"gid": g, "vt": self.vts.get(g, ""),
                             "gn": self.gnames.get(g, "")}
                            for g in list(self.groups)]
                _send_plain(sock, T_PROBE, {"svc": "hive-mesh",
                                            "ver": PROTO_VER, "groups": info})
                sock.close()
                return
            if typ != T_HELLO:
                sock.close()
                return
            # 用路由键找出是哪个群 —— 不知道这个群就无从解密
            gid = ""
            key = b""
            with _LOCK:
                for g in list(self.groups):
                    if _rk(g) == rk:
                        gid = g
                        break
            if not gid:
                sock.close()
                return
            key = H.group_key(gid) or b""
            if not key:
                sock.close()
                return
            info = _open_frame(body, key)
            if not isinstance(info, dict):
                # 解不开 = 没有群密钥 = 不是成员，直接断
                sock.close()
                return
            peer = Peer(sock, addr, gid, out=not inbound)
            peer.key = key
            peer.uid = str(info.get("uid") or "")
            peer.name = str(info.get("name") or "")
            peer.dev = str(info.get("dev") or "")
            peer.ver = int(info.get("ver") or 0)
            peer.port = int(info.get("port") or 0)
            peer.last_seen = time.time()

            # 同一个 user_id、但不是这台机器 —— 说明对方拿的是拷贝过去的目录。
            # 这时候两台机器在群里没法区分，必须让用户知道（否则消息会显示成自己发的）。
            if peer.uid and peer.uid == self.uid and peer.dev and peer.dev != self.dev:
                self.uid_conflict = {
                    "uid": peer.uid, "dev": peer.dev, "my_dev": self.dev,
                    "addr": "%s:%s" % addr, "name": peer.name,
                    "ts": time.time(),
                }

            peer.send(T_HELLO, {
                "uid": self.uid,
                "name": self.uname,
                "dev": self.dev,
                "ver": PROTO_VER,
                "port": self.port,
                "gid": gid,
                "known": self._known_list(),
                "members": self._members_of(gid),
                "kicked": self._kicked_of(gid),
            })
            with _LOCK:
                # 同一 uid 已经有连接了：只保留「uid 较小方发起」的那一条。
                # 双方用同一个规则算，落到的必然是**同一条 TCP 连接** ——
                # 否则会出现「各自留下不同方向」的两条死连接，消息全丢。
                for p in self.peers:
                    if p.alive and p.gid == gid and p.uid and p.uid == peer.uid:
                        keep_new = _prefer_out(self.uid, peer.uid) == (not inbound)
                        if not keep_new:
                            peer.close()
                            return
                        # 新连接才是该留的那条：把旧的关掉
                        p.alive = False
                        try:
                            p.sock.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        try:
                            p.sock.close()
                        except Exception:
                            pass
                        try:
                            self.peers.remove(p)
                        except ValueError:
                            pass
                        break
                self.peers.append(peer)
                if peer.uid:
                    self.known[peer.uid] = {
                        "host": addr[0], "port": peer.port or 0,
                        "name": peer.name, "gid": gid,
                    }
            if self.on_peer:
                try:
                    self.on_peer()
                except Exception:
                    pass
            # 有新成员进来就告诉大家，让其他人也能直连他 ——
            # 否则后加入的人只能靠中间人转发，中间人一掉线就断。
            threading.Thread(target=self._announce_peers, args=(gid,),
                             daemon=True).start()
            self._read_loop(peer)
        except Exception as e:
            self._last_err = str(e)
        finally:
            if peer is not None:
                peer.close()
                with _LOCK:
                    try:
                        self.peers.remove(peer)
                    except ValueError:
                        pass
                if self.on_peer:
                    try:
                        self.on_peer()
                    except Exception:
                        pass

    def _read_loop(self, peer: Peer) -> None:
        while not self._stop and peer.alive:
            fr = _read_frame(peer.sock)
            if not fr:
                break
            typ, _rk_, body = fr
            obj = _open_frame(body, peer.key)
            if obj is None:
                continue
            peer.last_seen = time.time()
            try:
                self._dispatch(peer, typ, obj)
            except Exception as e:
                self._last_err = str(e)
        peer.alive = False

    # ------------------------------------------------------------ 消息处理

    def ingest(self, gid: str, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把收到的消息落盘。抽成方法而不是直接调 H，是为了让每个节点能有
        自己的落盘目标（同一进程跑多个账号时必需）。"""
        return H.ingest_message(gid, msg)

    def _dispatch(self, peer: Peer, typ: int, obj: Dict[str, Any]) -> None:
        gid = peer.gid
        if typ == T_PING:
            peer.send(T_PONG, {"t": time.time()})
            return
        if typ == T_PONG:
            return
        if typ == T_BYE:
            peer.alive = False
            return
        if typ == T_HELLO:
            # 双向握手：对方也在 hello，补齐信息 + peer exchange
            if not peer.uid:
                peer.uid = str(obj.get("uid") or "")
                peer.name = str(obj.get("name") or "")
                peer.dev = str(obj.get("dev") or "")
                peer.ver = int(obj.get("ver") or 0)
                peer.port = int(obj.get("port") or 0)
            if peer.uid and peer.uid == self.uid and peer.dev and peer.dev != self.dev:
                self.uid_conflict = {
                    "uid": peer.uid, "dev": peer.dev, "my_dev": self.dev,
                    "addr": "%s:%s" % peer.addr, "name": peer.name,
                    "ts": time.time(),
                }
            for k in (obj.get("known") or []):
                if isinstance(k, dict) and k.get("uid"):
                    self._learn(k, gid)
            self._merge_members(gid, obj.get("members"), obj.get("kicked"))
            # 握手完成后互报水位：双方各发一次 T_SYNC，谁缺谁补。
            # ⚠️ 2026-11-13 修：原来握手只发 T_HELLO，从不发 T_SYNC ——
            #    于是「历史消息补发」这条路径其实从未生效，只有 outbox 在兜。
            #    结果：走 send_message 落盘的消息（@hd 的回答、系统消息）
            #    在对方当时不在线时，上线后永远补不到。实测确认。
            try:
                peer.send(T_SYNC, {"since": H.max_ts(gid)})
            except Exception:
                pass
            return
        if typ == T_MEMBERS:
            self._merge_members(gid, obj.get("members"), obj.get("kicked"))
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_MEMBERS,
                             {"members": obj.get("members") or [],
                              "kicked": obj.get("kicked") or [], "ttl": ttl},
                             skip=peer)
            return
        if typ == T_PEERS:
            for k in (obj.get("known") or []):
                if isinstance(k, dict) and k.get("uid"):
                    self._learn(k, gid)
            return
        if typ == T_SYNC:
            # 对方报了他本地最大的时间戳，我把比它新的都补发过去。
            # 双方都会发一次，所以两边各自补齐 —— 谁缺谁收，不缺的靠 mid 去重。
            #
            # full=True 表示「我是新来的，本地没有历史」，直接推全量（限 800 条）。
            since = float(obj.get("since") or 0)
            full = bool(obj.get("full"))
            limit = 800 if full else 1000
            sent = 0
            for m in H.read_messages(gid, 0, limit):
                if full or float(m.get("ts") or 0) > since:
                    if peer.send(T_MSG, {"msg": m, "ttl": 0}):
                        sent += 1
            if sent:
                with _LOCK:
                    self._last_sync = {"gid": gid, "sent": sent,
                                       "full": full, "ts": time.time()}
            return
        if typ == T_MSG:
            m = obj.get("msg")
            if not isinstance(m, dict):
                return
            m = H.normalize_msg(m)
            if not m:
                return
            mid = str(m.get("id") or "")
            if mid:
                with _LOCK:
                    s = self.seen.setdefault(gid, set())
                    if mid in s:
                        return          # 泛洪时同一条会从多条路径到达
                    s.add(mid)
            saved = self.ingest(gid, m)
            if saved is None:
                return
            if self.on_msg:
                try:
                    self.on_msg(gid, saved)
                except Exception:
                    pass
            # 继续转发（去掉来源那条，TTL 减一）
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_MSG, {"msg": saved, "ttl": ttl}, skip=peer)
            return
        if typ == T_GONE:
            # 群主删了群：本地这份也下线（移到 _deleted，不是真删）。
            # 转发给其他成员 —— 有人可能没直连群主，靠中间人传过去。
            nm = str(obj.get("name") or "")
            try:
                if H.load_group(gid):
                    H.delete_group(gid, force=True)
            except Exception:
                pass
            with _LOCK:
                self.groups.discard(gid)
                self.vts.pop(gid, None)
                self.gnames.pop(gid, None)
                self.beacons.pop(gid, None)
            if self.on_gone:
                try:
                    self.on_gone(gid, nm)
                except Exception:
                    pass
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_GONE, {"name": nm, "ttl": ttl}, skip=peer)
            return
        if typ == T_STREAM:
            if self.on_stream:
                try:
                    self.on_stream(gid, obj)
                except Exception:
                    pass
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_STREAM, dict(obj, ttl=ttl), skip=peer)
            return
        if typ == T_CANCEL:
            if self.on_cancel:
                try:
                    self.on_cancel(gid, obj)
                except Exception:
                    pass
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_CANCEL, dict(obj, ttl=ttl), skip=peer)
            return
        if typ == T_FILE:
            # 附件分片：先交给上层攒（攒齐才落盘），再继续转发给其他成员。
            # 不去重 —— 每片有自己的 idx，重复到达由上层按 idx 覆盖，天然幂等。
            if self.on_file:
                try:
                    self.on_file(gid, obj)
                except Exception:
                    pass
            ttl = int(obj.get("ttl") or TTL_DEFAULT) - 1
            if ttl > 0:
                self._fanout(gid, T_FILE, dict(obj, ttl=ttl), skip=peer)
            return
        # ★ 2026-11-13：走到这里 = **本机不认识这个帧类型**。
        #   最典型的场景就是「对方代码比本机新」（自开发改过、只有一边重启了）。
        #   必须**安全忽略**：不抛异常、不断连 —— 否则两台机器代码一不同步，
        #   连上就断，用户看到的就是「联机失败」。
        #   记一次日志（同类只记一次），否则排查时完全看不出发生过什么。
        if typ not in _UNKNOWN_TYP:
            _UNKNOWN_TYP.add(int(typ))
            logger.warning("收到不认识的帧类型 %s（多半是对方版本更新），"
                           "已安全忽略；连接保持", typ)

    def _announce_peers(self, gid: str) -> None:
        """把「我认识谁」广播出去。

        延迟 0.5 秒再发：对方刚连上时自己的 peers 列表里可能还没登记完，
        立刻发会漏掉刚刚这条连接的双方。
        """
        time.sleep(0.5)
        if self._stop:
            return
        self.flush()                      # 断线期间攒下的消息，先补发
        self.broadcast_members(gid)       # 成员表也同步一份（新连上的人需要）
        known = self._known_list()
        self._fanout(gid, T_PEERS, {"known": known})
        # 互报水位：把对方没有的历史消息补过去（对方不缺的靠 mid 去重）
        #
        # ⚠️ 2026-11-13 修：只看 max_ts 会漏掉「新入群」这一种。
        #    B 刚入群时本地会写一条「B 加入了群」的系统消息，它的 ts 是**现在**，
        #    比 A 的所有历史消息都新 —— A 收到 since=现在，判断「他没缺什么」，
        #    于是 B 永远看不到入群之前的聊天记录。实测确认。
        #    所以本地消息很少时（新成员），主动发一次 full 请求，让对方推全量。
        local_n = 0
        try:
            local_n = len(H.read_messages(gid, 0, 4))
        except Exception:
            pass
        with _LOCK:
            need_full = local_n <= 3 and gid not in self._full_req
            if need_full:
                self._full_req.add(gid)
        if need_full:
            self._fanout(gid, T_SYNC, {"since": 0.0, "full": True})
        else:
            self._fanout(gid, T_SYNC, {"since": H.max_ts(gid)})

    def _known_list(self) -> List[Dict[str, Any]]:
        out = []
        with _LOCK:
            out.append({"uid": self.uid, "host": lan_ip(), "port": self.port,
                        "name": self.uname, "gid": ""})
            for uid, k in self.known.items():
                if uid == self.uid:
                    continue
                out.append({"uid": uid, "host": k.get("host", ""),
                            "port": k.get("port", 0), "name": k.get("name", ""),
                            "gid": k.get("gid", "")})
        return out

    def _members_of(self, gid: str) -> List[Dict[str, Any]]:
        """本机记的成员表（发给对方做同步）。群不存在就返回空。"""
        try:
            return H.members_snapshot(gid)
        except Exception:
            return []

    def _merge_members(self, gid: str, arr: Any, kicked: Any = None) -> None:
        """收到别人的成员表就合并。变了就通知上层刷新界面。"""
        n = 0
        try:
            if isinstance(arr, list) and arr:
                n = H.merge_members(gid, arr)
            if isinstance(kicked, list) and kicked:
                n += H.merge_kicked(gid, kicked)
        except Exception:
            return
        if n and self.on_members:
            try:
                self.on_members(gid)
            except Exception:
                pass

    def _learn(self, k: Dict[str, Any], gid: str) -> None:
        """记住一个人的地址；如果还不认识，主动连过去（peer exchange 的核心）。"""
        uid = str(k.get("uid") or "")
        host = str(k.get("host") or "")
        port = int(k.get("port") or 0)
        if not uid or uid == self.uid or not host or not port:
            return
        with _LOCK:
            self.known[uid] = {"host": host, "port": port,
                               "name": k.get("name", ""), "gid": gid}
            for p in self.peers:
                if p.alive and p.uid == uid:
                    return
        # 只让 uid 较小的一方发起连接（见 _prefer_out 的说明）：
        # 双方都主动连会形成双向双连接，去重时容易各自关掉不同方向的那条。
        if not _prefer_out(self.uid, uid):
            return
        threading.Thread(target=self.connect, args=(host, port, gid),
                         daemon=True).start()

    # ------------------------------------------------------------ 发送

    def _fanout(self, gid: str, typ: int, obj: Any, skip: Optional[Peer] = None) -> int:
        n = 0
        for p in self.peers_of(gid):
            if skip is not None and p is skip:
                continue
            if p.send(typ, obj):
                n += 1
        return n

    def broadcast(self, gid: str, typ: int, obj: Any) -> int:
        """发消息给所有 peer。一个都没发出去就进队列，等连上再补。"""
        obj = dict(obj)
        obj.setdefault("ttl", TTL_DEFAULT)
        n = self._fanout(gid, typ, obj)
        if n == 0 and typ == T_MSG:
            with _LOCK:
                self.outbox.append({"gid": gid, "obj": obj, "ts": time.time()})
                if len(self.outbox) > 2000:
                    self.outbox = self.outbox[-2000:]
        return n

    def broadcast_members(self, gid: str) -> int:
        """把本地成员表广播出去（加群/踢人/改角色/退群后调用）。

        没有 peer 时不排队 —— 成员表不是消息，对方下次握手会重新交换，
        排一堆旧快照反而会把已经踢掉的人带回来。
        """
        arr = self._members_of(gid)
        if not arr:
            return 0
        return self._fanout(gid, T_MEMBERS,
                            {"members": arr, "kicked": self._kicked_of(gid),
                             "ttl": TTL_DEFAULT})

    def _kicked_of(self, gid: str) -> List[str]:
        """本机记的被踢名单（随成员表一起同步）。"""
        try:
            return list((H.load_group(gid) or {}).get("kicked") or [])
        except Exception:
            return []

    def flush(self) -> int:
        """把断线期间攒下的消息发出去。"""
        n = 0
        with _LOCK:
            todo, self.outbox = self.outbox, []
        for it in todo:
            if self._fanout(it["gid"], T_MSG, it["obj"]) > 0:
                n += 1
            else:
                with _LOCK:
                    self.outbox.append(it)
        return n

    # ------------------------------------------------------------ 主动连接

    def connect(self, host: str, port: int, gid: str) -> Dict[str, Any]:
        if not self.started:
            return {"ok": False, "error": "本机 P2P 网络还没启动"}
        key = H.group_key(gid) or b""
        if not key:
            return {"ok": False, "error": "群不存在或没有密钥"}
        try:
            s = socket.create_connection((host, port), timeout=6.0)
            s.settimeout(60.0)
        except Exception as e:
            return {"ok": False, "error": "连不上 %s:%s —— %s" % (host, port, e)}
        peer = Peer(s, (host, port), gid, out=True)
        peer.key = key
        try:
            if not _send_frame(s, T_HELLO, _rk(gid), key, {
                "uid": self.uid,
                "name": self.uname,
                "dev": self.dev,
                "ver": PROTO_VER,
                "port": self.port,
                "gid": gid,
                "known": self._known_list(),
                "members": self._members_of(gid),
                "kicked": self._kicked_of(gid),
            }):
                s.close()
                return {"ok": False, "error": "握手帧发送失败"}
        except Exception as e:
            try:
                s.close()
            except Exception:
                pass
            return {"ok": False, "error": str(e)}
        threading.Thread(target=self._serve, args=(s, (host, port), False),
                         daemon=True).start()
        return {"ok": True, "host": host, "port": port, "gid": gid}

    def punch_addr(self, refresh: bool = False) -> Dict[str, Any]:
        """本机的「跨网打洞地址」—— 对方拿它就能在 NAT 后面连到你。

        注意这里返回的是 **UDP 端口**，和 TCP 的 self.port 不是同一个；
        地址带 punch:// 前缀，好让对端知道该走打洞而不是 TCP 直连。
        """
        if not self.punch:
            return {"ok": False, "error": "打洞通道没启动"}
        m = self.punch.mapped(timeout=2.5, force=refresh)
        if not m:
            return {"ok": False,
                    "error": "查不到公网映射地址（可能断网，或公共 STUN 都被挡了）"}
        addr = "%s:%s" % (m["ip"], m["port"])
        return {"ok": True, "ip": m["ip"], "port": m["port"],
                "addr": addr, "code": "punch://%s" % addr}

    def connect_punch(self, host: str, port: int, gid: str,
                      timeout: float = 10.0) -> Dict[str, Any]:
        """跨网络连接：TCP 到不了时改用 UDP 打洞。

        打通后这条通道被当成一条普通的出站连接交给 _serve ——
        之后的消息、附件、AI 回答全部复用原有逻辑，不需要任何特殊处理。
        """
        if not self.started:
            return {"ok": False, "error": "本机 P2P 网络还没启动"}
        if not self.punch:
            return {"ok": False, "error": "打洞通道没启动（UDP 端口可能被占用）"}
        key = H.group_key(gid) or b""
        if not key:
            return {"ok": False, "error": "群不存在或没有密钥"}
        r = self.punch.connect(host, port, timeout=timeout)
        if not r.get("ok"):
            return r
        sock = r["sock"]
        peer = Peer(sock, (host, int(port)), gid, out=True)
        peer.key = key
        try:
            ok = _send_frame(sock, T_HELLO, _rk(gid), key, {
                "uid": self.uid,
                "name": self.uname,
                "dev": self.dev,
                "ver": PROTO_VER,
                "port": self.port,
                "gid": gid,
                "known": self._known_list(),
                "members": self._members_of(gid),
                "kicked": self._kicked_of(gid),
            })
        except Exception as e:  # noqa: BLE001
            ok = False
            r = {"ok": False, "error": "打洞后握手失败：%s" % e}
        if not ok:
            try:
                sock.close()
            except Exception:
                pass
            return r if not r.get("ok") else {"ok": False, "error": "打洞后握手帧发送失败"}
        threading.Thread(target=self._serve, args=(sock, (host, int(port)), False),
                         daemon=True).start()
        return {"ok": True, "host": host, "port": int(port), "gid": gid,
                "via": "punch"}

    def connect_code(self, gid: str) -> Dict[str, Any]:
        """生成跨网络用的连接码，并给出可直接念给别人的纯地址。"""
        if not self.started:
            return {"ok": False,
                    "error": "本机 P2P 网络还没启动（先点一下左边的群名进入群聊）"}
        pub = _public_ip()
        lip = lan_ip()
        ts = tailscale_ip()
        # lan_addr / wan_addr 是**不带 hive3:// 前缀**的纯 IP:端口 ——
        # 「密码入群」的地址框要的就是这个格式，直接复制粘贴不会带出协议头。
        # 跨网主路径：本项目自带的 UDP 打洞，不需要装任何外部软件。
        # 查不到（断网/STUN 全被挡）时再退回 Tailscale 提示。
        punch = self.punch_addr() if self.punch else {"ok": False}
        paddr = punch.get("addr", "") if punch.get("ok") else ""
        if paddr:
            note = ("把「跨网地址」（punch:// 开头）发给对方，他粘进连接框就能连。"
                    "数据全程加密，不经过任何服务器。")
        elif ts:
            note = ("打洞地址暂时查不到，但检测到 Tailscale："
                    "把「Tailscale 地址」发给对方也能连。")
        else:
            note = ("打洞地址暂时查不到（可能断网）。同一个 WiFi 下用局域网地址即可。")
        return {
            "ok": True,
            "gid": gid,
            "port": self.port,
            "lan_ip": lip,
            "public_ip": pub,
            "tailscale_ip": ts,
            "lan_addr": "%s:%s" % (lip, self.port),
            "wan_addr": ("%s:%s" % (pub, self.port)) if pub else "",
            "ts_addr": ("%s:%s" % (ts, self.port)) if ts else "",
            "lan_code": "hive3://%s:%s" % (lip, self.port),
            "wan_code": ("hive3://%s:%s" % (pub, self.port)) if pub else "",
            "ts_code": ("hive3://%s:%s" % (ts, self.port)) if ts else "",
            # 跨网打洞（推荐）：UDP 端口，带 punch:// 前缀
            "punch_ok": bool(paddr),
            "punch_ip": punch.get("ip", "") if punch.get("ok") else "",
            "punch_port": punch.get("port", 0) if punch.get("ok") else 0,
            "punch_addr": paddr,
            "punch_code": ("punch://%s" % paddr) if paddr else "",
            "note": note,
        }

    def parse_code(self, code: str) -> Dict[str, Any]:
        c = (code or "").strip()
        # punch:// 表示这是 UDP 打洞地址（端口是 UDP 的，不能拿 TCP 去连）
        mode = "tcp"
        for pre in ("punch://", "udp://"):
            if c.startswith(pre):
                c = c[len(pre):]
                mode = "punch"
                break
        for pre in ("hive3://", "hive2://", "hive://"):
            if c.startswith(pre):
                c = c[len(pre):]
                break
        if "/" in c:
            c = c.split("/")[0]
        if ":" not in c:
            return {"ok": False, "error": "连接码格式不对，应该是 hive3://IP:端口"}
        host, _, port = c.rpartition(":")
        try:
            port_i = int(port)
        except Exception:
            return {"ok": False, "error": "端口不是数字"}
        host = host.strip("[]").strip()
        if not host:
            return {"ok": False, "error": "连接码里没有地址"}
        if not (1 <= port_i <= 65535):
            return {"ok": False,
                    "error": "端口 %d 超出范围（应为 1~65535）" % port_i}
        return {"ok": True, "host": host, "port": port_i, "mode": mode}

    def probe(self, host: str, port: int, timeout: float = 3.0) -> Dict[str, Any]:
        """连到对方端口问一句「你有哪些群」，拿回 [{gid, vt, gn}]。

        只用来给「密码 + 地址」的人确认密码对不对，拿不到任何内容。
        """
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, int(port)))
            body = json.dumps({"svc": "hive-probe", "ver": PROTO_VER}).encode("utf-8")
            s.sendall(bytes([T_PROBE]) + b"\x00\x00\x00\x00"
                      + struct.pack(">I", len(body)) + body)
            fr = _read_frame(s)
            if not fr:
                return {"ok": False, "error": "对方没有回应（可能不是群聊端口）"}
            _typ, _rk, raw = fr
            try:
                o = json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                return {"ok": False, "error": "对方返回的内容看不懂"}
            groups = o.get("groups") if isinstance(o, dict) else None
            return {"ok": True, "host": host, "port": int(port),
                    "groups": groups or []}
        except socket.timeout:
            return {"ok": False, "error": "连接超时：%s:%s 没有响应" % (host, port)}
        except Exception as e:
            return {"ok": False, "error": "连不上 %s:%s（%s）" % (host, port, e)}
        finally:
            try:
                if s:
                    s.close()
            except Exception:
                pass

    def seen_beacons(self, gid: str = "") -> List[Dict[str, Any]]:
        """局域网里听到的群（不限自己所在的群）—— 密码入群靠它搜。"""
        now = time.time()
        with _LOCK:
            items = list(self.beacons.values())
        out = [dict(b) for b in items if (not gid or b.get("gid") == gid)
               and now - float(b.get("ts") or 0) < 120]
        out.sort(key=lambda b: -float(b.get("ts") or 0))
        return out

    # ------------------------------------------------------------ UDP 发现

    def _beacon_loop(self) -> None:
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 必须绑在**广播端口**上：发到 45231 却绑随机端口的话，
            # 别人发的广播永远收不到（这是实测踩到的坑）。
            try:
                u.bind(("", BEACON_PORT))
            except Exception:
                # 端口被占（比如开了两个实例）：降级到随机端口，
                # 只能主动连别人，不能被别人自动发现。
                u.bind(("", 0))
            u.settimeout(0.5)
            self._udp = u
            try:
                self._beacon_port = u.getsockname()[1]
            except Exception:
                self._beacon_port = BEACON_PORT
        except Exception as e:
            self._last_err = "UDP 广播启动失败：%s" % e
            return
        threading.Thread(target=self._udp_recv_loop, daemon=True).start()
        while not self._stop:
            try:
                with _LOCK:
                    groups = list(self.groups)
                for g in groups:
                    pkt = json.dumps({
                        "svc": "hive-mesh", "ver": PROTO_VER,
                        "rk": _rk(g).hex(), "gid": g,
                        "port": self.port, "uid": self.uid,
                        "name": self.uname, "ts": time.time(),
                        # vt = 验证标签，让「只输密码」的人能确认密码对不对
                        "vt": self.vts.get(g, ""),
                        "gn": self.gnames.get(g, ""),
                        "dev": self.dev,
                    }).encode("utf-8")
                    try:
                        self._udp.sendto(pkt, ("255.255.255.255", self._beacon_port))
                        self._udp.sendto(pkt, (lan_ip().rsplit(".", 1)[0] + ".255",
                                                self._beacon_port))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(BEACON_EVERY)

    def _udp_recv_loop(self) -> None:
        while not self._stop and self._udp:
            try:
                data, addr = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                o = json.loads(data.decode("utf-8", "ignore"))
            except Exception:
                continue
            if o.get("svc") != "hive-mesh":
                continue
            if o.get("uid") == self.uid and (o.get("dev") or "") == self.dev:
                continue                      # 自己发的广播，忽略
            gid = str(o.get("gid") or "")
            if not gid:
                continue
            # 不管是不是我的群，先记下来 —— 「只输密码」的人要靠它去搜群。
            # 广播里只有 gid / 验证标签 / 群名，没有密钥，所以记下来也不泄密。
            with _LOCK:
                self.beacons[gid] = {
                    "gid": gid,
                    "vt": str(o.get("vt") or ""),
                    "gn": str(o.get("gn") or ""),
                    "host": addr[0],
                    "port": int(o.get("port") or 0),
                    "uid": str(o.get("uid") or ""),
                    "name": str(o.get("name") or ""),
                    "ts": time.time(),
                }
                if gid not in self.groups:
                    continue                  # 不是我的群：只记，不连
                mine = self.vts.get(gid) or ""
                theirs = str(o.get("vt") or "")
                if mine and theirs and mine != theirs:
                    continue                  # 同群号但密钥不同（群主换过密码），别乱连
                for p in self.peers:
                    if p.alive and p.uid == o.get("uid"):
                        break
                else:
                    host, port = addr[0], int(o.get("port") or 0)
                    if port:
                        threading.Thread(target=self.connect,
                                         args=(host, port, gid), daemon=True).start()

    # ------------------------------------------------------------ 心跳/清理

    def _housekeep_loop(self) -> None:
        while not self._stop:
            time.sleep(5.0)
            now = time.time()
            dead: List[Peer] = []
            with _LOCK:
                for p in self.peers:
                    if not p.alive:
                        dead.append(p)
                        continue
                    if now - p.last_seen > PEER_TIMEOUT:
                        dead.append(p)
                        continue
                    try:
                        p.send(T_PING, {"t": now})
                    except Exception:
                        dead.append(p)
                for p in dead:
                    try:
                        self.peers.remove(p)
                    except ValueError:
                        pass
                    p.close()
                if dead:
                    self.flush()
            if dead and self.on_peer:
                try:
                    self.on_peer()
                except Exception:
                    pass
            # 自动重连：知道地址但当前没连上的人，定期再试一次。
            # 没有这一步，跨公网（收不到对方 UDP 广播）一旦断线就永远连不回去，
            # 只能重新传一次连接码 —— 群主不在、只剩两个非群主时最难受的正是这里。
            try:
                self._retry_known()
            except Exception:
                pass

    def _retry_known(self) -> None:
        """把 known 里「知道地址但没连上」的人重新连一遍（每人 20 秒最多一次）。"""
        now = time.time()
        todo: List[Tuple[str, str, int, str]] = []
        with _LOCK:
            alive = {p.uid for p in self.peers if p.alive and p.uid}
            for uid, k in self.known.items():
                if not uid or uid == self.uid or uid in alive:
                    continue
                host = str(k.get("host") or "")
                port = int(k.get("port") or 0)
                gid = str(k.get("gid") or "")
                if not host or not port or not gid or gid not in self.groups:
                    continue
                # 同 _learn：只由 uid 较小的一方重连，避免双向双连接。
                if not _prefer_out(self.uid, uid):
                    continue
                if now - float(self._retry.get(uid) or 0) < 20.0:
                    continue
                self._retry[uid] = now
                todo.append((uid, host, port, gid))
        for uid, host, port, gid in todo:
            threading.Thread(target=self.connect, args=(host, port, gid),
                             daemon=True).start()


# ---------------------------------------------------------------- 单例

_NODE: Optional[MeshNode] = None


def node() -> MeshNode:
    global _NODE
    if _NODE is None:
        _NODE = MeshNode()
    return _NODE
