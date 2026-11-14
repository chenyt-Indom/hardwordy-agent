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
import random
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional

import backend.hive as H
import backend.hive_mesh as MESH
from backend import ws_min

PROTO = 1
HDR = struct.Struct(">IB")
K_CTRL = 0
K_FRAME = 1
K_SEQ = 2
MAX_PKT = 9 * 1024 * 1024

# 打洞参数
# 10 台以上的群如果同时两两打洞，会瞬间发起几十次握手把 NAT 打满。
# 所以限制同时进行的打洞数，其余的排队（Semaphore 会阻塞等待）。
PUNCH_CONCURRENCY = 4
PUNCH_TIMEOUT = 4.5        # 单次打洞等待（用户选的 3~5 秒档）
PUNCH_RETRY_AFTER = 25.0   # 失败后多久允许重试

# ---------------------------------------------------------------- 多线路保障
# 一条线路挂了还有别的路，用户不用手改配置。
# 顺序 = 优先级：主地址 → 443(带 TLS) → 8080 → 80 → 裸 TCP 端口。
BACKUP_PORTS = (443, 8080, 80)
BACKUP_PATHS = ("/hive-relay", "/hive")
CONNECT_TIMEOUT = 4.0      # 单条候选的建连超时
CONNECT_TIMEOUT_LAST = 1.5 # 最后一条（裸端口，常被安全组挡）给短一点
BACKOFF_BASE = 1.0         # 全部候选都不通时的首次等待
BACKOFF_MAX = 8.0          # 退避上限（原来是 30 秒，太慢）
HB_EVERY = 4.0             # 心跳检查粒度（秒）
# 实测：服务器端重启后，原阈值 45 秒导致客户端 30 秒内还以为在线。
# 正常时每 4 秒就有一个 pong 回来，16 秒一个包都没有 = 链路已经死了。
# 收紧到 16 秒的代价极小（重连会按 seq 补发，不丢消息），收益是快 3 倍恢复。
HB_IDLE = 16.0             # 这么久没有任何入站包 → 判定死链，主动断开重连
STABLE_SECONDS = 5.0       # 连上后活过这么久才算「稳定」，否则按失败计
HS_TIMEOUT = 6.0           # 握手总超时（含 WS 升级 + challenge + ready）
CAND_COOLDOWN = 180.0      # 一条候选连续失败后，冷却多久不再优先试它
CAND_COOLDOWN_HARD = 3600.0  # 「对面不是我们中继」这类，冷 1 小时
CAND_FAIL_LIMIT = 2        # 连续失败几次进入冷却


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


def candidates_for(url: str, extra: Any = None) -> List[Dict[str, Any]]:
    """给一个地址，派生出一条主线路 + 若干备用线路。

    为什么必须派生：服务器可能只开了 80、也可能只开了 443；nginx 路径
    可能是 /hive-relay、也可能是 /hive。实测这台服务器三条都通：
        ws://host/hive-relay        36ms   （80）
        wss://host/hive-relay       67ms   （443）
        ws://host:8080/hive         46ms   （8080）
    任何一条断掉，其他条还能连上，用户不用手改配置。

    顺序就是优先级：显式配置的额外地址 → 主地址 → 443 → 8080 → 80 → 裸端口。
    裸 TCP 端口放最后，因为它最常被云安全组挡住（实测 18765 超时）。
    """
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u:
            return
        p = parse_url(u)
        if not p.get("ok"):
            return
        key = (p["host"], p["port"], bool(p.get("ws")), p.get("path"))
        if key in seen:
            return
        seen.add(key)
        p["url"] = u
        out.append(p)

    if isinstance(extra, (list, tuple)):
        for u in extra:
            add(str(u))
    add(url)
    p = parse_url(url)
    if not p.get("ok"):
        return out
    host = p["host"]
    if p.get("ws"):
        for port in BACKUP_PORTS:
            for path in BACKUP_PATHS:
                scheme = "wss" if port == 443 else "ws"
                add("%s://%s:%s%s" % (scheme, host, port, path))
    # 裸 TCP 直连端口：安全组常挡，但万一放行了就是最快的一条（无 WS 开销）
    add("hive4://%s:18765" % host)
    return out


def _set_keepalive(sock: socket.socket) -> None:
    """开 TCP keepalive —— 让「对端机器直接掉线」这种情况能被系统发现。

    光靠应用层心跳也能发现，但 keepalive 是内核级的，更底层也更省事。
    Windows 的写法跟 Linux 不一样，两边都兜着。
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass
    try:
        if hasattr(socket, "TCP_KEEPIDLE"):        # Linux / macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except Exception:
        pass
    try:
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 20000, 5000))
    except Exception:
        pass


def _wrap_tls(sock: socket.socket, host: str, port: int, timeout: float):
    """给 socket 套 TLS，返回 (新 socket, 是否校验证书)。

    先严格校验；失败再宽松一次。为什么必须这样：用户填的是**IP**，
    而证书是签给域名的（实测 443 端口 SSL 报
    `SSLCertVerificationError`），严格校验等于把这条路彻底废掉。

    宽松模式失去的是传输层的中间人防护；但聊天内容本身是
    AES-256-GCM 密文 + HMAC 鉴权，中间人既解不开也伪造不了，
    所以「能连上」比「证书链完美」在这里更重要。
    """
    import ssl
    try:
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(sock, server_hostname=host)
        return s, True
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
    # 宽松重试：重新建一条连接再包
    s2 = socket.create_connection((host, port), timeout=timeout)
    s2.settimeout(timeout)
    ctx = ssl._create_unverified_context()
    s = ctx.wrap_socket(s2, server_hostname=host)
    return s, False


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
        if p.get("ws"):
            if p.get("tls"):
                s, _verified = _wrap_tls(s, p["host"], p["port"], timeout)
                s.settimeout(timeout)
            rest = ws_min.handshake_client(
                s, "%s:%s" % (p["host"], p["port"]),
                p.get("path") or "/hive-relay")
            if rest is None:
                return {"ok": False,
                        "error": "WebSocket 握手失败（nginx 的 location 没配好？）",
                        "host": p["host"], "port": p["port"]}
            s = ws_min.WSSock(s, rest=rest, mask_out=True)
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
def password_fp(password: str) -> str:
    """密码指纹：HMAC(规范化密码, "hive-dir-v1")。

    用作群目录的键。为什么不用 gid 或 vt：入群的人**还不知道 gid**，
    而 vt 是拿 gid 派生的，所以只能拿「密码本身」派生一个指纹来查。
    单向 HMAC，服务器拿到 fp 也推不出密码（12 位密码 ≈ 1.2e18 种）。
    """
    pw = (password or "").strip().replace("-", "").replace(" ", "").upper()
    return hmac.new(pw.encode("utf-8"), b"hive-dir-v1",
                    hashlib.sha256).hexdigest()[:32]


def _open_sock(p, timeout: float = 5.0):
    """按 parse_url 的结果建连（含 WS 升级）。返回 socket 或抛异常。"""
    s = socket.create_connection((p["host"], p["port"]), timeout=timeout)
    s.settimeout(timeout)
    if p.get("ws"):
        if p.get("tls"):
            import ssl
            s = ssl.create_default_context().wrap_socket(
                s, server_hostname=p["host"])
            s.settimeout(timeout)
        rest = ws_min.handshake_client(
            s, "%s:%s" % (p["host"], p["port"]),
            p.get("path") or "/hive-relay")
        if rest is None:
            try:
                s.close()
            except Exception:
                pass
            raise RuntimeError("WebSocket 握手失败")
        s = ws_min.WSSock(s, rest=rest, mask_out=True)
    return s


def _dir_call(url: str, token: str, op: str, extra: Dict[str, Any],
              timeout: float = 5.0) -> Dict[str, Any]:
    """连一次中继、发一个目录请求、拿结果就走（不保持长连接）。"""
    p = parse_url(url)
    if not p.get("ok"):
        return p
    s = None
    try:
        s = _open_sock(p, timeout)
        got = _recv_pkt(s)
        if not got:
            return {"ok": False, "error": "中继没有响应"}
        nonce = ""
        try:
            nonce = str(json.loads(got[1].decode("utf-8")).get("nonce") or "")
        except Exception:
            pass
        mac = hmac.new(token.encode("utf-8"),
                       (nonce + "|dir").encode("utf-8"),
                       hashlib.sha256).hexdigest()
        body = dict(extra)
        body.update({"op": op, "mac": mac})
        if not _send_pkt(s, K_CTRL, json.dumps(body).encode("utf-8")):
            return {"ok": False, "error": "发送失败"}
        got = _recv_pkt(s)
        if not got:
            return {"ok": False, "error": "中继没回包"}
        r = json.loads(got[1].decode("utf-8"))
        if r.get("op") == "err":
            return {"ok": False, "error": r.get("msg") or "中继拒绝了请求"}
        return {"ok": True, "raw": r}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def dir_publish(url: str, token: str, password: str, gid: str,
                vt: str, name: str = "") -> Dict[str, Any]:
    """把群登记到中继目录（群主建群/连上中继时自动调用）。"""
    return _dir_call(url, token, "dir_publish",
                     {"fp": password_fp(password), "gid": gid,
                      "vt": vt or "", "name": name or ""})


def dir_lookup(url: str, token: str, password: str) -> Dict[str, Any]:
    """按密码指纹在中继目录里找群。"""
    return _dir_call(url, token, "dir_lookup", {"fp": password_fp(password)})



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
        # ---------------- 信令 + P2P 直连 ----------------
        # p2p: uid -> {"name","paddr","state","err","ts"}
        #   state: connecting / ok / fail / gone
        # 一旦某个成员 state == ok，他就在 mesh 里有一条独立的直连 Peer，
        # 发给他消息**不再经过服务器**。
        self.p2p: Dict[str, Dict[str, Any]] = {}
        self._p2p_lock = threading.Lock()
        self._punching: set = set()      # 正在打洞的 uid，防重复
        self._punch_sem = threading.Semaphore(PUNCH_CONCURRENCY)
        self.direct_ok = 0               # 已直连人数（给前端看）
        self.direct_fail = 0
        self.bypassed = 0                # 有多少帧完全没经过服务器
        self.signal_pub_at = 0.0
        self.signal_error = ""
        # ---------------- 多线路 + 心跳 ----------------
        self.cands: List[Dict[str, Any]] = []   # 候选线路（已解析）
        self.cand_idx = 0                        # 下次优先试哪条
        self.active_url = ""                     # 当前真正在用的那条
        self.last_ok_url = ""                    # 上次成功过的（优先复用）
        self.cand_tries = 0                      # 累计尝试次数
        self.fail_streak = 0                     # 连续「全部候选都不通」次数
        self.short_lived = 0                     # 连上就掉（<5 秒）的次数
        self.last_rx = 0.0                       # 最后一次收到任何包的时刻
        self.heartbeat_fail = 0                  # 心跳超时次数
        self._hb_thread: Optional[threading.Thread] = None
        self.tls_unverified = False              # 是否用了不校验证书的 TLS
        self._cand_fail: Dict[str, int] = {}     # 候选线路连续失败次数
        self._cand_until: Dict[str, float] = {}  # 候选线路冷却到期时刻
        self._fail_hard = False                  # 本次失败是否「对面不是中继」
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

    def start(self, url: str, gid: str, token: str = "",
              extra_urls: Any = None) -> Dict[str, Any]:
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
        self.ws = bool(p.get("ws"))
        self.tls = bool(p.get("tls"))
        self.ws_path = str(p.get("path") or "/hive-relay")
        self.gid = gid
        self.token = token or ""
        self.last_seq = int(self._seqs.get(gid) or 0)
        self.error = ""
        self._stop = False
        # 候选线路：主地址 + 自动派生的备用入口
        self.cands = candidates_for(self.url, extra_urls)
        self.cand_idx = 0
        if self.last_ok_url:
            for i, c in enumerate(self.cands):
                if c.get("url") == self.last_ok_url:
                    self.cand_idx = i      # 上次哪条通，这次先试它
                    break
        self.active_url = ""
        self.fail_streak = 0
        self.short_lived = 0
        self.heartbeat_fail = 0
        self.last_rx = time.time()
        self.state = "connecting"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # 心跳：每 5 秒查一次，45 秒没有任何入站包就判定死链、主动断开重连
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()
        # 给第一次连接一点时间，让调用方拿到真实状态。
        # 7 秒 = 够把主线路 + 至少一条备线试完（单条超时 4 秒）。
        # 还没通就返回 connecting，后台会继续试，不会就此放弃。
        deadline = time.time() + 7.0
        while time.time() < deadline:
            if self.state in ("online", "error"):
                break
            time.sleep(0.05)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop = True
        self._teardown()
        self.state = "offline"
        with self._p2p_lock:
            self.p2p.clear()
            self._punching.clear()
        self.direct_ok = 0
        self.direct_fail = 0
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

    def _sleep(self, sec: float) -> None:
        """可被 stop() 打断的 sleep。"""
        end = time.time() + max(0.0, sec)
        while not self._stop and time.time() < end:
            time.sleep(0.1)

    def _loop(self) -> None:
        """连接循环：轮着试所有候选线路，任何一条通了就干活。

        跟旧版的区别（只在「连不上 / 断了」时才看得出来）：
          · 旧版只有一条路，那条挂了就一直干等，退避到 30 秒才重试一次；
          · 新版一条挂了**立刻换下一条**（实测这台服务器 3 条都通），
            全部都不通才退避，上限 8 秒，还带 ±25% 随机抖动 ——
            服务器重启时所有客户端不会同一毫秒一起冲上来。
        """
        while not self._stop:
            cands = self.cands or []
            if not cands:
                self.state = "error"
                self.error = "没有可用的服务器地址"
                self._sleep(1.0)
                continue

            ok = False
            n = len(cands)
            start = self.cand_idx % n
            # 上一轮「所有线路都不通」→ 本轮只探最优那条（上次成功的 / 主地址）。
            # 为什么：所有候选其实指向同一台服务器，服务器挂了的时候，
            # 把 7 条全试一遍纯属浪费时间（实测恢复要 19.9 秒）。只探一条，
            # 服务一回来下一轮就能接上（退避上限 8 秒内）。每 5 轮做一次全扫，
            # 防止「主地址坏了但别的能通」这种情况被漏掉。
            if self._degraded and self._sweep_skip < 4:
                self._sweep_skip += 1
                idx = 0
                for i, c in enumerate(cands):
                    if str(c.get("url") or "") == self.last_ok_url:
                        idx = i
                        break
                order = [idx]
            else:
                self._sweep_skip = 0
                order = [(start + k) % n for k in range(n)]
            # 冷却中的线路排到最后 —— 既不会每次白等一个超时，
            # 也不会因为一次抖动就永久放弃一条路（服务器哪天又开了呢）。
            now = time.time()
            fresh = [i for i in order
                     if self._cand_until.get(str(cands[i].get("url")), 0) <= now]
            cold = [i for i in order if i not in fresh]
            order = fresh + cold
            for k, idx in enumerate(order):
                if self._stop:
                    break
                p = cands[idx]
                url = str(p.get("url") or "")
                self.cand_idx = idx
                self.cand_tries += 1
                try:
                    ok = self._connect_once(p, last=(k == len(order) - 1))
                except Exception as e:
                    self.error = "%s: %s" % (type(e).__name__, e)
                    ok = False
                if ok:
                    self.active_url = url
                    self.last_ok_url = url
                    self._cand_fail.pop(url, None)
                    self._cand_until.pop(url, None)
                    self._degraded = False
                    self._sweep_skip = 0
                    break
                # 这条不通 → 记一笔，立刻换下一条，不等待。
                # 冷却分两档：「对面根本不是我们的中继」这种重试也没用的，
                # 直接冷 1 小时；网络抖动/超时只冷 3 分钟。
                self._cand_fail[url] = self._cand_fail.get(url, 0) + 1
                if self._cand_fail[url] >= CAND_FAIL_LIMIT:
                    cool = (CAND_COOLDOWN_HARD if self._fail_hard
                            else CAND_COOLDOWN)
                    self._cand_until[url] = time.time() + cool
            if self._stop:
                break

            if ok:
                t_conn = time.time()
                self._read_loop()
                lived = time.time() - t_conn
                self._teardown()
                if self._stop:
                    break
                self.state = "connecting"
                self.error = "连接断开，正在重连"
                if lived >= STABLE_SECONDS:
                    self.fail_streak = 0
                    continue      # 稳定跑过一阵才断的 → 立刻重连，不等待
                self.short_lived += 1
            else:
                self._teardown()
                if self._stop:
                    break
                self.state = "error"
                self.error = "所有线路都连不上（已试 %d 条）：%s" % (
                    n, self.error or "")

            self.fail_streak += 1
            delay = min(BACKOFF_MAX,
                        BACKOFF_BASE * (1.6 ** min(self.fail_streak - 1, 8)))
            delay *= 1.0 + random.uniform(-0.25, 0.25)
            self._sleep(delay)

    def _heartbeat_loop(self) -> None:
        """心跳：主动发 ping，同时看「多久没收到任何包」。

        为什么必须有：TCP 连接在对端机器直接断电、NAT 表项被清、云厂商
        静默丢包这些情况下**不会报错**，socket 一直「可写」，看起来在线，
        实际消息全丢。应用层心跳能在 45 秒内发现并主动断开重连。
        """
        while not self._stop:
            self._sleep(HB_EVERY)
            if self._stop:
                return
            if self.state != "online":
                continue
            now = time.time()
            idle = now - (self.last_rx or now)
            if idle > HB_IDLE:
                self.heartbeat_fail += 1
                self.error = "心跳超时（%.0f 秒没收到任何包），主动断开重连" % idle
                sock = self.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                continue
            try:
                sent = self.ping()
            except Exception:
                sent = False
            if not sent:
                # 连 ping 都发不出去 → 连接已经死了，不用等空闲超时
                self.heartbeat_fail += 1
                self.error = "心跳发送失败，连接已断，主动重连"
                sock = self.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass

    def _connect_once(self, p: Optional[Dict[str, Any]] = None,
                      last: bool = False) -> bool:
        if p is None:
            p = {"host": self.host, "port": self.port, "ws": self.ws,
                 "tls": self.tls, "path": self.ws_path}
        self.host = p["host"]
        self.port = int(p["port"])
        self.ws = bool(p.get("ws"))
        self.tls = bool(p.get("tls"))
        self.ws_path = str(p.get("path") or "/hive-relay")
        # 最后一条（裸端口）常被安全组丢包，给短一点的超时，别拖慢换线
        timeout = CONNECT_TIMEOUT_LAST if last else CONNECT_TIMEOUT
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=timeout)
        except Exception as e:
            # ⚠️ 单条线路失败**不能**置成 error —— 那样 start() 会以为
            #    整条链路废了，后面几条备用的根本来不及试。
            self.state = "connecting"
            self.error = "连不上 %s:%s（%s）" % (self.host, self.port,
                                              type(e).__name__)
            return False
        # ⚠️ 握手阶段**必须带超时**：实测 8080/hive 这条能连上、也能完成
        #    WS 升级，但对面根本不发 challenge。如果这里 settimeout(None)，
        #    _recv_pkt 会永久阻塞 —— 整条重连循环卡死，后面的备用线路
        #    一条都没机会试。这是「多线路保障」最致命的坑。
        sock.settimeout(HS_TIMEOUT)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        _set_keepalive(sock)
        self.tls_unverified = False
        self._fail_hard = False      # True = 这条不是我们的中继，别频繁重试

        # ws:// / wss:// 先升级成 WebSocket，再把 sock 包成 WSSock ——
        # 之后的 _recv_pkt / _send_pkt 完全不用改（接口和普通 socket 一致）。
        if self.ws:
            try:
                if self.tls:
                    sock, verified = _wrap_tls(sock, self.host, self.port,
                                               HS_TIMEOUT)
                    sock.settimeout(HS_TIMEOUT)
                    self.tls_unverified = not verified
                rest = ws_min.handshake_client(
                    sock, "%s:%s" % (self.host, self.port), self.ws_path)
            except Exception as e:
                self.error = "WebSocket 握手异常（%s）" % type(e).__name__
                self._fail_hard = True
                try:
                    sock.close()
                except Exception:
                    pass
                return False
            if rest is None:
                self.error = ("WebSocket 握手失败 —— 检查 nginx 里 %s 这条 "
                              "location 有没有配好" % self.ws_path)
                self._fail_hard = True
                try:
                    sock.close()
                except Exception:
                    pass
                return False
            sock = ws_min.WSSock(sock, rest=rest, mask_out=True)

        # 握手：先收 challenge，再回 HMAC
        got = _recv_pkt(sock)
        if not got:
            self.error = "中继没有响应握手（这个入口连上了，但对面不是我们的中继）"
            self._fail_hard = True
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
        try:
            sock.settimeout(None)      # 握手完成，恢复正常阻塞收包
        except Exception:
            pass
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
        self.last_rx = time.time()
        self.fail_streak = 0
        self.reconnects += 1 if self.reconnects else 0
        # 连上就报自己的打洞地址 → 服务器转告其他成员 → 双方自动直连。
        # 放在**独立线程**里：STUN 偶尔要等 1~2 秒，不能卡住握手返回。
        threading.Thread(target=self._pub_signal, daemon=True).start()
        return True

    # -------------------------------------------------- 信令 → 打洞

    def _on_signal(self, c: Dict[str, Any]) -> None:
        """服务器转来「某人的打洞地址」→ 后台去连他，连上就绕开服务器。"""
        op = c.get("op")
        if op == "signal_gone":
            uid = str(c.get("uid") or "")
            if uid:
                with self._p2p_lock:
                    self.p2p.pop(uid, None)
                self._refresh_direct()
            return
        if op == "signal_list":
            peers = c.get("peers") or []
        else:
            peers = [{"uid": c.get("uid"), "name": c.get("name"),
                      "paddr": c.get("paddr")}]
        for p in peers:
            if not isinstance(p, dict):
                continue
            uid = str(p.get("uid") or "")
            paddr = str(p.get("paddr") or "")
            if not uid or ":" not in paddr:
                continue
            with self._p2p_lock:
                cur = self.p2p.get(uid) or {}
                if cur.get("state") == "ok":
                    continue        # 已经连上了，别重连
                if (cur.get("state") == "fail"
                        and time.time() - float(cur.get("ts") or 0) < PUNCH_RETRY_AFTER):
                    continue        # 刚失败过，等一会儿再试
                if uid in self._punching:
                    continue        # 正在连
                self._punching.add(uid)
                self.p2p[uid] = {"name": str(p.get("name") or ""),
                                 "paddr": paddr, "state": "connecting",
                                 "err": "", "ts": time.time()}
            threading.Thread(target=self._try_punch, args=(uid, paddr),
                             daemon=True).start()

    def _try_punch(self, uid: str, paddr: str) -> None:
        """后台对一个成员打洞。成功 → mesh 里多一条直连 Peer。

        connect_punch() 打通后会把可靠 UDP 通道当普通 socket 交给 _serve，
        所以之后的文字、附件、流式回答全部走这条直连，**不经过服务器**。
        """
        err = ""
        try:
            host, _, port_s = paddr.rpartition(":")
            port = int(port_s)
            node = MESH.node()
            # 10 台以上的群会同时冒出几十次打洞，NAT 会扛不住 —— 限并发。
            if not self._punch_sem.acquire(timeout=PUNCH_TIMEOUT * 2):
                err = "排队超时（同时打洞的人太多，稍后自动重试）"
            else:
                try:
                    r = node.connect_punch(host, port, self.gid,
                                           timeout=PUNCH_TIMEOUT)
                finally:
                    self._punch_sem.release()
                if not r.get("ok"):
                    err = str(r.get("error") or "打洞失败")
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
        with self._p2p_lock:
            self._punching.discard(uid)
            cur = self.p2p.get(uid) or {}
            cur.update({"state": "fail" if err else "ok",
                        "err": err, "ts": time.time()})
            self.p2p[uid] = cur
        self._refresh_direct()

    def _refresh_direct(self) -> None:
        with self._p2p_lock:
            ok = sum(1 for v in self.p2p.values() if v.get("state") == "ok")
            bad = sum(1 for v in self.p2p.values() if v.get("state") == "fail")
        self.direct_ok = ok
        self.direct_fail = bad

    def _all_direct(self) -> bool:
        """房间里所有人都已直连 → 这时消息完全不经服务器。

        只要还有一个人没连上，就继续走服务器兜底，否则那个人会收不到消息。
        """
        with self._p2p_lock:
            if not self.p2p:
                return False
            if not all(v.get("state") == "ok" for v in self.p2p.values()):
                return False
            need = len(self.p2p)
        # 光看状态还不够：直连 Peer 可能已经断了但状态没来得及改。
        # 核对 mesh 里真的有这么多条活着的、同群的、非中继连接。
        try:
            live = 0
            for p in list(MESH.node().peers):
                if isinstance(p, RelayPeer) or not p.alive:
                    continue
                if p.gid == self.gid:
                    live += 1
            return live >= need
        except Exception:
            return False

    def _pub_signal(self) -> None:
        """把自己的打洞地址报给服务器。服务器只负责转告，不碰消息内容。"""
        try:
            node = MESH.node()
            a = node.punch_addr() if node.punch else {"ok": False}
        except Exception as e:  # noqa: BLE001
            a = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if not a.get("ok"):
            self.signal_error = ("拿不到公网映射地址（%s）—— 可能是公共 STUN 被挡，"
                                 "不影响服务器转发" % (a.get("error") or "无响应"))
            return
        self.signal_error = ""
        paddr = "%s:%s" % (a.get("ip"), a.get("port"))
        sock = self.sock
        if sock is None:
            return
        node = MESH.node()
        with self._send_lock:
            _send_pkt(sock, K_CTRL, json.dumps({
                "op": "signal_pub", "uid": node.uid,
                "name": node.uname, "paddr": paddr,
            }, ensure_ascii=False).encode("utf-8"))
        self.signal_pub_at = time.time()

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
            self.last_rx = time.time()
            kind, payload = got
            if kind == K_CTRL:
                try:
                    c = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if c.get("op") == "pong":
                    self.rtt_ms = round((time.time() - float(c.get("t") or 0)) * 1000, 1)
                elif c.get("op") in ("signal_list", "signal_peer", "signal_gone"):
                    try:
                        self._on_signal(c)
                    except Exception as e:  # noqa: BLE001
                        self.error = "信令处理异常：%s" % e
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
        # ★ 全员直连后，消息只走 P2P 通道，服务器**一个字节都收不到**。
        #   返回 True 是让 mesh 认为「已发出」——实际由直连 Peer 在送。
        #   只要还有一个人没连上，就继续走服务器兜底，否则他会漏消息。
        if self._all_direct():
            self.bypassed += 1
            return True
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
            # ---------------- 多线路 + 心跳 ----------------
            "active_url": self.active_url,
            "tls_unverified": self.tls_unverified,
            "candidates": [str(c.get("url") or "") for c in (self.cands or [])],
            "cand_count": len(self.cands or []),
            "cand_tries": self.cand_tries,
            "fail_streak": self.fail_streak,
            "short_lived": self.short_lived,
            "heartbeat_fail": self.heartbeat_fail,
            "last_rx_ago": (round(time.time() - self.last_rx, 1)
                            if self.last_rx else -1),
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
            # ---------------- 直连状态 ----------------
            "direct_ok": self.direct_ok,
            "direct_fail": self.direct_fail,
            "bypassed": self.bypassed,
            "all_direct": self._all_direct(),
            "signal_error": self.signal_error,
            "p2p": [{"uid": k,
                     "name": v.get("name", ""),
                     "paddr": v.get("paddr", ""),
                     "state": v.get("state", ""),
                     "err": v.get("err", "")}
                    for k, v in list(self.p2p.items())],
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
