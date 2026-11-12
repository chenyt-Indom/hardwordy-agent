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
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import backend.hive as H

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore

PROTO_VER = 3
MESH_PORT = 8899
MAX_FRAME = 8 * 1024 * 1024      # 单帧上限 8MB（消息/进度帧够用；大文件走 P2P 传输）
TTL_DEFAULT = 3                   # 泛洪最大跳数
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


def _public_ip() -> str:
    """尽力探公网 IP（失败就返回空，不阻塞主流程）。"""
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=2.5) as r:
            ip = r.read(64).decode("utf-8", "ignore").strip()
            if ip.count(".") == 3:
                return ip
    except Exception:
        pass
    return ""


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
        self.groups: Set[str] = set()
        self.peers: List[Peer] = []
        self.seen: Dict[str, Set[str]] = {}       # gid -> 见过的 mid
        self.known: Dict[str, Dict[str, Any]] = {}  # uid -> {host, port, name, gid}
        self.outbox: List[Dict[str, Any]] = []    # 断线时排队的消息
        self._srv: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self._stop = False
        self._last_err = ""
        self._started_at = 0.0
        self.on_msg = None      # Callable[[str, dict], None]
        self.on_stream = None   # Callable[[str, dict], None]
        self.on_cancel = None   # Callable[[str, dict], None]
        self.on_file = None     # Callable[[str, dict], None]  附件分片
        self.on_peer = None     # Callable[[], None]

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
        for g in (groups or []):
            self.groups.add(g)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._beacon_loop, daemon=True).start()
        threading.Thread(target=self._housekeep_loop, daemon=True).start()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop = True
        with _LOCK:
            for p in list(self.peers):
                try:
                    p.send(T_BYE, {"uid": H.profile().get("user_id", "")})
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
        self.started = False
        self.groups.clear()
        return self.status()

    def add_group(self, gid: str) -> None:
        with _LOCK:
            self.groups.add(gid)

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
            }

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
            peer.ver = int(info.get("ver") or 0)
            peer.port = int(info.get("port") or 0)
            peer.last_seen = time.time()

            me = H.profile()
            peer.send(T_HELLO, {
                "uid": me.get("user_id", ""),
                "name": me.get("name", ""),
                "ver": PROTO_VER,
                "port": self.port,
                "gid": gid,
                "known": self._known_list(),
            })
            with _LOCK:
                # 同一 uid 已经有连接了就不重复加（避免双向连成两条）
                for p in self.peers:
                    if p.alive and p.gid == gid and p.uid and p.uid == peer.uid:
                        peer.close()
                        return
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
                peer.ver = int(obj.get("ver") or 0)
                peer.port = int(obj.get("port") or 0)
            for k in (obj.get("known") or []):
                if isinstance(k, dict) and k.get("uid"):
                    self._learn(k, gid)
            return
        if typ == T_PEERS:
            for k in (obj.get("known") or []):
                if isinstance(k, dict) and k.get("uid"):
                    self._learn(k, gid)
            return
        if typ == T_SYNC:
            mode = obj.get("mode")
            if mode == "req":
                # 对方报了他最大的 ts，我把更新的补发过去
                since = float(obj.get("since") or 0)
                for m in H.read_messages(gid, 0, 500):
                    if float(m.get("ts") or 0) > since:
                        peer.send(T_MSG, {"msg": m, "ttl": 0})
            elif mode == "hello":
                since = float(obj.get("since") or 0)
                peer.send(T_SYNC, {"mode": "req", "since": since})
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

    def _announce_peers(self, gid: str) -> None:
        """把「我认识谁」广播出去。

        延迟 0.5 秒再发：对方刚连上时自己的 peers 列表里可能还没登记完，
        立刻发会漏掉刚刚这条连接的双方。
        """
        time.sleep(0.5)
        if self._stop:
            return
        known = self._known_list()
        self._fanout(gid, T_PEERS, {"known": known})

    def _known_list(self) -> List[Dict[str, Any]]:
        me = H.profile().get("user_id", "")
        out = []
        with _LOCK:
            out.append({"uid": me, "host": lan_ip(), "port": self.port,
                        "name": H.profile().get("name", ""), "gid": ""})
            for uid, k in self.known.items():
                if uid == me:
                    continue
                out.append({"uid": uid, "host": k.get("host", ""),
                            "port": k.get("port", 0), "name": k.get("name", ""),
                            "gid": k.get("gid", "")})
        return out

    def _learn(self, k: Dict[str, Any], gid: str) -> None:
        """记住一个人的地址；如果还不认识，主动连过去（peer exchange 的核心）。"""
        uid = str(k.get("uid") or "")
        host = str(k.get("host") or "")
        port = int(k.get("port") or 0)
        me = H.profile().get("user_id", "")
        if not uid or uid == me or not host or not port:
            return
        with _LOCK:
            self.known[uid] = {"host": host, "port": port,
                               "name": k.get("name", ""), "gid": gid}
            for p in self.peers:
                if p.alive and p.uid == uid:
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
        me = H.profile()
        try:
            if not _send_frame(s, T_HELLO, _rk(gid), key, {
                "uid": me.get("user_id", ""),
                "name": me.get("name", ""),
                "ver": PROTO_VER,
                "port": self.port,
                "gid": gid,
                "known": self._known_list(),
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

    def connect_code(self, gid: str) -> Dict[str, Any]:
        """生成跨网络用的连接码。"""
        if not self.started:
            return {"ok": False, "error": "本机 P2P 网络还没启动"}
        pub = _public_ip()
        lip = lan_ip()
        return {
            "ok": True,
            "gid": gid,
            "port": self.port,
            "lan_ip": lip,
            "public_ip": pub,
            "lan_code": "hive3://%s:%s" % (lip, self.port),
            "wan_code": ("hive3://%s:%s" % (pub, self.port)) if pub else "",
            "note": ("公网地址没探到，跨网络需要路由器端口映射，"
                     "或改用 Tailscale 这类组网工具") if not pub else
                    "把这个码发给对方，他粘进来就能连上（数据不经任何服务器）",
        }

    def parse_code(self, code: str) -> Dict[str, Any]:
        c = (code or "").strip()
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
        return {"ok": True, "host": host.strip("[]"), "port": port_i}

    # ------------------------------------------------------------ UDP 发现

    def _beacon_loop(self) -> None:
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            u.bind(("", 0))
            u.settimeout(0.5)
            self._udp = u
        except Exception as e:
            self._last_err = "UDP 广播启动失败：%s" % e
            return
        threading.Thread(target=self._udp_recv_loop, daemon=True).start()
        me = H.profile()
        while not self._stop:
            try:
                with _LOCK:
                    groups = list(self.groups)
                for g in groups:
                    pkt = json.dumps({
                        "svc": "hive-mesh", "ver": PROTO_VER,
                        "rk": _rk(g).hex(), "gid": g,
                        "port": self.port, "uid": me.get("user_id", ""),
                        "name": me.get("name", ""), "ts": time.time(),
                    }).encode("utf-8")
                    try:
                        self._udp.sendto(pkt, ("255.255.255.255", 45231))
                        self._udp.sendto(pkt, (lan_ip().rsplit(".", 1)[0] + ".255", 45231))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(BEACON_EVERY)

    def _udp_recv_loop(self) -> None:
        me = H.profile().get("user_id", "")
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
            if o.get("uid") == me:
                continue                      # 自己发的广播，忽略
            gid = str(o.get("gid") or "")
            with _LOCK:
                if gid not in self.groups:
                    continue                  # 不是同一个群
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


# ---------------------------------------------------------------- 单例

_NODE: Optional[MeshNode] = None


def node() -> MeshNode:
    global _NODE
    if _NODE is None:
        _NODE = MeshNode()
    return _NODE
