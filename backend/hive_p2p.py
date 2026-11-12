# -*- coding: utf-8 -*-
"""Hive P2P —— 群内大文件点对点直传（**数据面不经过任何服务器**）。

为什么这样做
============
用户要求：G 级别传输、必须直连互联网、连地址交换也不许过服务器。
TCP/IP 的硬约束是：两台 NAT 后面的机器互相不知道对方在哪。
所以本模块把「连接信息」做成一段**连接码**，由人手工传递（二维码 / 复制粘贴）。
连接码里只有 IP、端口、一次性 token 和群指纹，不含文件内容；
真正传文件时双方直连，**一个字节都不经过中继**。

协议
====
帧：MAGIC(4='HVp2') + TYPE(1) + LEN(4, 大端) + PAYLOAD
  HELLO  接收端→发送端：{v, gid, fid, token, have(bitmap b64)}
  META   发送端→接收端：{name, size, chunk, count, sha, missing}
  CHUNK  发送端→接收端：idx(4) + nonce(12) + 密文
  DONE   发送端→接收端：{sha}
  ERR    任一方：{msg}

加密：群密钥 + 本次传输 token → HKDF-SHA256 → AES-256-GCM。
      每块独立 nonce（= idx 的 12 字节大端），所以断点续传时每块可独立校验。

断点续传：接收端把已收块位图存进 `.state.json`；重连时放进 HELLO，
          发送端只补缺的块。传完校验整文件 SHA256。
"""

import os
import re
import io
import json
import time
import socket
import struct
import hashlib
import secrets
import threading
import base64
import urllib.request
from typing import Optional, Dict, Any, List, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

import backend.hive as H

MAGIC = b"HVp2"
PROTO = 1
CHUNK = 4 * 1024 * 1024          # 4MB/块：G 级文件也就几千块，位图很小
READ_TIMEOUT = 60.0
CONNECT_TIMEOUT = 12.0
PORT_RANGE = (47800, 47900)      # 尽量避开常见端口

T_HELLO, T_META, T_CHUNK, T_DONE, T_ERR = 1, 2, 3, 4, 5

_LOCK = threading.RLock()
_TRANSFERS: Dict[str, Dict[str, Any]] = {}
_STOP = threading.Event()


# ---------------------------------------------------------------- 路径
def p2p_dir(gid: str) -> str:
    d = os.path.join(H._gdir(gid), "p2p")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(gid: str, fid: str) -> str:
    return os.path.join(p2p_dir(gid), fid + ".state.json")


def _part_path(gid: str, fid: str) -> str:
    return os.path.join(p2p_dir(gid), fid + ".part")


# ---------------------------------------------------------------- 加密
def _key(gid: str, token: str) -> bytes:
    """群密钥 + 传输 token → 32 字节 AES 密钥。token 只存在于连接码里。"""
    base = H.group_key(gid)
    if not base:
        raise ValueError("群密钥不存在，无法加密传输")
    hk = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"hive-p2p-v1",
              info=b"p2p:" + str(token).encode("utf-8"))
    return hk.derive(base)


def _nonce(idx: int) -> bytes:
    return struct.pack(">Q", idx) + b"\x00\x00\x00\x00"


# ---------------------------------------------------------------- 帧
def _send_frame(sock: socket.socket, typ: int, payload: bytes) -> None:
    sock.sendall(MAGIC + struct.pack(">BI", typ, len(payload)) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        got = sock.recv(n - len(buf))
        if not got:
            raise ConnectionError("对端断开连接")
        buf += got
    return bytes(buf)


def _recv_frame(sock: socket.socket):
    head = _recv_exact(sock, 9)
    if head[:4] != MAGIC:
        raise ValueError("不是 Hive P2P 协议")
    typ, ln = struct.unpack(">BI", head[4:9])
    if ln > 64 * 1024 * 1024:
        raise ValueError("帧过大，拒绝")
    return typ, _recv_exact(sock, ln) if ln else b""


# ---------------------------------------------------------------- 位图
def _bm_new(count: int) -> bytearray:
    return bytearray((count + 7) // 8)


def _bm_set(bm: bytearray, i: int) -> None:
    bm[i >> 3] |= (1 << (i & 7))


def _bm_has(bm: bytearray, i: int) -> bool:
    return bool(bm[i >> 3] & (1 << (i & 7)))


def _bm_missing(bm: bytearray, count: int) -> List[int]:
    return [i for i in range(count) if not _bm_has(bm, i)]


def _bm_b64(bm: bytearray) -> str:
    return base64.b64encode(bytes(bm)).decode("ascii")


def _bm_from_b64(s: str) -> bytearray:
    try:
        return bytearray(base64.b64decode(s or ""))
    except Exception:
        return bytearray()


# ---------------------------------------------------------------- 传输登记
def _reg(tid: str, **kw) -> Dict[str, Any]:
    with _LOCK:
        rec = {
            "tid": tid, "state": "waiting", "done": 0, "total": 0,
            "speed": 0.0, "eta": None, "error": "", "ts": time.time(),
            "paused": False, "peers": 0,
        }
        rec.update(kw)
        _TRANSFERS[tid] = rec
        return rec


def _upd(tid: str, **kw) -> None:
    with _LOCK:
        if tid in _TRANSFERS:
            _TRANSFERS[tid].update(kw)


_PLAIN = (str, int, float, bool, type(None), dict, list)


def _public(r: Dict[str, Any]) -> Dict[str, Any]:
    """只留能 JSON 序列化的字段。

    ⚠️ 内部状态里放着 `threading.Event`（用于暂停/取消），直接返回给
       FastAPI 会在序列化时炸掉 —— 必须在这里筛一遍。
    """
    return {k: v for k, v in r.items() if isinstance(v, _PLAIN)}


def get(tid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        r = _TRANSFERS.get(tid)
        return _public(r) if r else None


def list_all(gid: str = "") -> List[Dict[str, Any]]:
    with _LOCK:
        out = [_public(v) for v in _TRANSFERS.values()]
    if gid:
        out = [r for r in out if r.get("gid") == gid]
    return sorted(out, key=lambda r: r.get("ts", 0), reverse=True)


class _Speed:
    """滑动窗口测速 —— 瞬时速度抖动太大，用最近 2 秒的平均值。"""

    def __init__(self, win: float = 2.0):
        self.win = win
        self.samples: List[tuple] = []

    def add(self, n: int) -> float:
        now = time.time()
        self.samples.append((now, n))
        self.samples = [(t, v) for t, v in self.samples if now - t <= self.win]
        if len(self.samples) < 2:
            return 0.0
        span = max(1e-6, self.samples[-1][0] - self.samples[0][0])
        return sum(v for _, v in self.samples) / span


# ---------------------------------------------------------------- UPnP
def _ssdp_find() -> Optional[str]:
    """SSDP 找 IGD（家用路由器）的控制地址。找不到返回 None。"""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    ).encode("ascii")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.5)
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        deadline = time.time() + 2.5
        while time.time() < deadline:
            try:
                data, _ = s.recvfrom(8192)
            except socket.timeout:
                break
            txt = data.decode("utf-8", "ignore")
            m = re.search(r"(?im)^location:\s*(\S+)", txt)
            if m:
                return m.group(1).strip()
    except Exception:
        return None
    finally:
        s.close()
    return None


def _soap(loc: str, service: str, action: str, args: str) -> str:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:%s xmlns:u=\"%s\">%s</u:%s></s:Body></s:Envelope>"
        % (action, service, args, action)
    ).encode("utf-8")
    req = urllib.request.Request(loc, data=body, method="POST")
    req.add_header("Content-Type", 'text/xml; charset="utf-8"')
    req.add_header("SOAPAction", '"%s#%s"' % (service, action))
    with urllib.request.urlopen(req, timeout=6) as r:
        return r.read().decode("utf-8", "ignore")


def _wan_service(loc: str) -> Optional[str]:
    """从设备描述里找 WANIPConnection / WANPPPConnection 的 serviceType。"""
    try:
        with urllib.request.urlopen(loc, timeout=6) as r:
            xml = r.read().decode("utf-8", "ignore")
    except Exception:
        return None
    for st in re.findall(r"<serviceType>([^<]+)</serviceType>", xml):
        if "WANIPConnection" in st or "WANPPPConnection" in st:
            return st.strip()
    return None


def upnp_map(port: int, ttl: int = 7200) -> Dict[str, Any]:
    """尝试用 UPnP 把端口映射到公网。失败也**不影响**局域网/组网使用。"""
    loc = _ssdp_find()
    if not loc:
        return {"ok": False, "why": "路由器不支持 UPnP 或已关闭"}
    svc = _wan_service(loc)
    if not svc:
        return {"ok": False, "why": "路由器没有 WAN 连接服务"}
    try:
        ip = re.search(r"<NewExternalIPAddress>([^<]*)<", _soap(
            loc, svc, "GetExternalIPAddress", "")).group(1).strip()
    except Exception:
        ip = ""
    args = (
        "<NewRemoteHost></NewRemoteHost>"
        "<NewExternalPort>%d</NewExternalPort>"
        "<NewProtocol>TCP</NewProtocol>"
        "<NewInternalPort>%d</NewInternalPort>"
        "<NewInternalClient>%s</NewInternalClient>"
        "<NewEnabled>1</NewEnabled>"
        "<NewPortMappingDescription>HiveP2P</NewPortMappingDescription>"
        "<NewLeaseDuration>%d</NewLeaseDuration>" % (port, port, _lan_ip(), ttl)
    )
    try:
        _soap(loc, svc, "AddPortMapping", args)
    except Exception as e:
        return {"ok": False, "why": "路由器拒绝映射（%s）" % str(e)[:60], "ip": ip}
    return {"ok": True, "ip": ip, "port": port}


def upnp_unmap(port: int) -> None:
    loc = _ssdp_find()
    if not loc:
        return
    svc = _wan_service(loc)
    if not svc:
        return
    try:
        _soap(loc, svc, "DeletePortMapping",
              "<NewRemoteHost></NewRemoteHost><NewExternalPort>%d</NewExternalPort>"
              "<NewProtocol>TCP</NewProtocol>" % port)
    except Exception:
        pass


def _lan_ip() -> str:
    """取本机在局域网里的地址（不会真的发包，只是让系统选路由）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 连接码
def make_code(gid: str, fid: str, host: str, port: int, token: str) -> str:
    """连接码：一段 base64，可做成二维码或直接复制发给对方。

    内容只有 群ID / 文件ID / 地址 / 一次性 token / 协议版本 —— **没有文件内容**。
    """
    payload = json.dumps({
        "v": PROTO, "gid": gid, "fid": fid,
        "h": host, "p": int(port), "t": token,
        "fp": (H.group_key(gid) or b"")[:4].hex(),
    }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "HIVE1-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def parse_code(code: str) -> Dict[str, Any]:
    s = str(code or "").strip()
    if s.startswith("HIVE1-"):
        s = s[6:]
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    try:
        raw = base64.urlsafe_b64decode(s + pad)
        d = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("连接码格式不对，请完整复制")
    for k in ("gid", "fid", "h", "p", "t"):
        if k not in d:
            raise ValueError("连接码缺少字段：%s" % k)
    return d


# ---------------------------------------------------------------- 发送端
def _sender_worker(tid: str, gid: str, fid: str, path: str,
                   token: str, srv: socket.socket, stop_evt: threading.Event,
                   disp_name: str = "") -> None:
    key = _key(gid, token)
    size = os.path.getsize(path)
    count = max(1, (size + CHUNK - 1) // CHUNK)
    # ⚠️ 不能在这里查索引：共享「本机任意文件」时它并不在群索引里
    name = disp_name or os.path.basename(path)
    sha = _file_sha(path)
    _upd(tid, total=size, count=count, name=name, state="listening", sha=sha)

    srv.settimeout(1.0)
    while not stop_evt.is_set() and not _STOP.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_serve_peer,
                         args=(tid, gid, fid, path, key, conn, addr, name,
                               size, count, sha, stop_evt),
                         daemon=True).start()
    try:
        srv.close()
    except Exception:
        pass


def _serve_peer(tid: str, gid: str, fid: str, path: str, key: bytes,
                conn: socket.socket, addr, name: str, size: int,
                count: int, sha: str, stop_evt: threading.Event) -> None:
    """一个接收端 = 一个线程。发送端可以同时喂多个成员（这就是「同时分发」）。"""
    aead = AESGCM(key)
    with _LOCK:
        n = (_TRANSFERS.get(tid) or {}).get("peers", 0) + 1
    _upd(tid, peers=n, state="transferring")
    spd = _Speed()
    sent_total = 0
    try:
        conn.settimeout(READ_TIMEOUT)
        typ, body = _recv_frame(conn)
        if typ != T_HELLO:
            raise ValueError("握手帧不对")
        hello = json.loads(body.decode("utf-8"))
        if hello.get("gid") != gid or hello.get("fid") != fid:
            raise ValueError("连接码和文件不匹配")
        have = _bm_from_b64(hello.get("have") or "")
        if len(have) != len(_bm_new(count)):
            have = _bm_new(count)
        missing = _bm_missing(have, count)

        _send_frame(conn, T_META, json.dumps({
            "name": name, "size": size, "chunk": CHUNK,
            "count": count, "sha": sha, "missing": len(missing),
        }, ensure_ascii=False).encode("utf-8"))

        base = sum(1 for i in range(count) if _bm_has(have, i))
        sent_total = base * CHUNK
        with open(path, "rb") as f:
            for idx in missing:
                while ((_TRANSFERS.get(tid) or {}).get("paused")
                       or stop_evt.is_set() or _STOP.is_set()):
                    if stop_evt.is_set() or _STOP.is_set():
                        raise ConnectionAbortedError("已取消")
                    time.sleep(0.2)
                f.seek(idx * CHUNK)
                data = f.read(CHUNK)
                if not data:
                    break
                ct = aead.encrypt(_nonce(idx), data, None)
                _send_frame(conn, T_CHUNK, struct.pack(">I", idx) + ct)
                sent_total += len(data)
                v = spd.add(len(data))
                eta = None
                if v > 1:
                    eta = int(max(0, size - sent_total) / v)
                _upd(tid, done=min(sent_total, size), speed=v, eta=eta)

        _send_frame(conn, T_DONE, json.dumps({"sha": sha}).encode("utf-8"))
        _upd(tid, state="done", done=size, speed=0.0, eta=0)
    except ConnectionAbortedError:
        _upd(tid, state="canceled", speed=0.0)
    except Exception as e:
        with _LOCK:
            cur = (_TRANSFERS.get(tid) or {}).get("state")
        if cur not in ("done", "canceled"):
            _upd(tid, state="error", error=str(e)[:160], speed=0.0)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _LOCK:
            n = max(0, (_TRANSFERS.get(tid) or {}).get("peers", 1) - 1)
        _upd(tid, peers=n)


def _file_sha(path: str, limit_mb: int = 0) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def share(gid: str, fid: str, port: int = 0, want_upnp: bool = True,
          src_path: str = "", name: str = "") -> Dict[str, Any]:
    """开始共享一个文件，返回连接码。同一文件重复调用会复用已有监听。

    src_path 非空时共享**本机任意文件**：不复制、不读进内存，直接按偏移量读原文件。
    这是发 G 级文件的唯一可行路径 —— 走上传接口要先 base64 进内存，几个 G 直接炸。
    """
    if src_path:
        path = os.path.abspath(src_path)
        if not os.path.isfile(path):
            raise ValueError("找不到这个文件：" + src_path)
        disp = name or os.path.basename(path)
        meta = {"name": disp, "size": os.path.getsize(path), "fid": fid}
    else:
        meta = H.get_file_meta(gid, fid)
        path = H.file_path(gid, fid)
        if not os.path.exists(path):
            raise ValueError("文件不在本机")
    size = os.path.getsize(path)

    with _LOCK:
        for tid, r in _TRANSFERS.items():
            if (r.get("gid") == gid and r.get("fid") == fid
                    and r.get("dir") == "send" and r.get("port")
                    and r.get("state") in ("listening", "transferring", "paused")):
                return {
                    "ok": True, "tid": tid, "code": r.get("code"),
                    "host": r.get("host"), "port": r.get("port"),
                    "lan_ip": _lan_ip(), "size": size, "reused": True,
                    "upnp": r.get("upnp") or {},
                }

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound = 0
    for p in ([port] if port else range(PORT_RANGE[0], PORT_RANGE[1])):
        try:
            srv.bind(("0.0.0.0", p))
            bound = srv.getsockname()[1]
            break
        except OSError:
            continue
    if not bound:
        srv.close()
        raise ValueError("端口都被占用了，稍后再试")
    srv.listen(16)

    token = secrets.token_urlsafe(12)
    tid = "p_" + secrets.token_hex(6)
    stop_evt = threading.Event()

    up = {"ok": False, "why": "未尝试"}
    if want_upnp:
        try:
            up = upnp_map(bound)
        except Exception as e:
            up = {"ok": False, "why": str(e)[:80]}

    host = (up.get("ip") or "").strip() or _lan_ip()
    code = make_code(gid, fid, host, bound, token)
    _reg(tid, gid=gid, fid=fid, dir="send", name=meta.get("name"),
         total=size, state="listening", port=bound, host=host,
         code=code, upnp=up, token=token, stop=stop_evt)

    threading.Thread(target=_sender_worker,
                     args=(tid, gid, fid, path, token, srv, stop_evt,
                           meta.get("name") or os.path.basename(path)),
                     daemon=True).start()
    # 局域网自动发现：同网段成员会收到「谁在共享哪个文件」
    threading.Thread(target=_beacon_sender,
                     args=(tid, gid, fid, meta.get("name") or "文件",
                           size, bound, token),
                     daemon=True).start()

    return {"ok": True, "tid": tid, "code": code, "host": host,
            "port": bound, "lan_ip": _lan_ip(), "size": size,
            "upnp": up, "reused": False}


# ---------------------------------------------------------------- 接收端
def receive(gid: str, code: str) -> Dict[str, Any]:
    """用连接码接收一个文件。断点续传靠 `.state.json` 里的位图。"""
    d = parse_code(code)
    if d.get("gid") != gid:
        raise ValueError("这个连接码属于别的群")
    fp = (H.group_key(gid) or b"")[:4].hex()
    if d.get("fp") and d["fp"] != fp:
        raise ValueError("连接码的群密钥指纹不匹配（可能不是同一个群）")

    fid = str(d["fid"])
    host, port, token = str(d["h"]), int(d["p"]), str(d["t"])

    with _LOCK:
        for tid, r in _TRANSFERS.items():
            if (r.get("gid") == gid and r.get("fid") == fid
                    and r.get("dir") == "recv"
                    and r.get("state") in ("connecting", "transferring", "paused")):
                return {"ok": True, "tid": tid, "reused": True}

    tid = "r_" + secrets.token_hex(6)
    stop_evt = threading.Event()
    _reg(tid, gid=gid, fid=fid, dir="recv", state="connecting",
         host=host, port=port, token=token, stop=stop_evt)
    threading.Thread(target=_recv_worker,
                     args=(tid, gid, fid, host, port, token, stop_evt),
                     daemon=True).start()
    return {"ok": True, "tid": tid, "reused": False}


def _recv_worker(tid: str, gid: str, fid: str, host: str, port: int,
                 token: str, stop_evt: threading.Event) -> None:
    # ⚠️ 本地状态文件名不能直接用「发送方的 fid」：共享「本机文件」时
    #    fid 是空串，两个文件就会共用同一份 .state.json / .part，
    #    第二个会继承第一个「已完成」的位图 → 直接判成功、但文件根本不存在。
    #    （只有 >MAX_FILE 的文件受影响，而这正是 P2P 的主用途。）
    #    fid 为空时改用 token：每次共享唯一，同一连接码重收仍能续传。
    skey = fid or ("t_" + str(token))
    part = _part_path(gid, skey)
    spath = _state_path(gid, skey)
    key = _key(gid, token)
    aead = AESGCM(key)
    spd = _Speed()
    sock = None
    try:
        st = H._read_json(spath, {}) or {}
        if not isinstance(st, dict):
            st = {}
        # 上次已经传完了（.part 已被移进 files 目录）：直接判定完成。
        # ⚠️ 不判这一步会出错：part 文件已不存在，后面算 SHA 时会报
        #    「文件校验失败」，让用户以为文件坏了。
        if st.get("finished"):
            # 用状态里记的 fid_out —— 共享「本机文件」时 fid 是空串，
            # 拿它去查 meta 必然查不到，会把已完成的文件误判成失败。
            out_fid = str(st.get("fid_out") or fid or "")
            try:
                m = H.get_file_meta(gid, out_fid) if out_fid else None
            except Exception:
                m = None
            if m and int(m.get("size") or 0) == int(st.get("size") or -1):
                _upd(tid, state="done", done=int(st.get("size") or 0),
                     total=int(st.get("size") or 0), name=st.get("name") or "",
                     speed=0.0, eta=0, fid_out=out_fid)
                return
        # 群密钥换了或 token 变了，旧位图作废
        if st.get("token") != token:
            st = {}
        bm = _bm_from_b64(st.get("bm") or "")
        size = int(st.get("size") or 0)
        count = int(st.get("count") or 0)
        chunk = int(st.get("chunk") or CHUNK)
        sha_want = str(st.get("sha") or "")
        name = str(st.get("name") or ("文件_" + fid[-6:]))

        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(READ_TIMEOUT)
        _upd(tid, state="handshake")
        _send_frame(sock, T_HELLO, json.dumps({
            "v": PROTO, "gid": gid, "fid": fid, "token": token,
            "have": _bm_b64(bm),
        }, ensure_ascii=False).encode("utf-8"))

        typ, body = _recv_frame(sock)
        if typ == T_ERR:
            raise ValueError(json.loads(body.decode("utf-8")).get("msg") or "对端报错")
        if typ != T_META:
            raise ValueError("对端没有返回文件信息")
        meta = json.loads(body.decode("utf-8"))
        size = int(meta["size"])
        count = int(meta["count"])
        chunk = int(meta["chunk"])
        sha_want = str(meta.get("sha") or "")
        name = str(meta.get("name") or name)

        if not bm or len(bm) != len(_bm_new(count)):
            bm = _bm_new(count)

        have_n = sum(1 for i in range(count) if _bm_has(bm, i))
        _upd(tid, state="transferring", total=size, name=name,
             done=min(size, have_n * chunk), count=count, sha=sha_want)

        if not os.path.exists(part):
            with open(part, "wb") as f:
                f.truncate(size)

        got = have_n
        last_save = time.time()
        with open(part, "r+b") as f:
            while True:
                if stop_evt.is_set() or _STOP.is_set():
                    raise ConnectionAbortedError("已取消")
                while ((_TRANSFERS.get(tid) or {}).get("paused")):
                    if stop_evt.is_set():
                        raise ConnectionAbortedError("已取消")
                    time.sleep(0.2)
                typ, body = _recv_frame(sock)
                if typ == T_DONE:
                    break
                if typ == T_ERR:
                    raise ValueError(json.loads(body.decode("utf-8")).get("msg") or "对端报错")
                if typ != T_CHUNK or len(body) < 16:
                    continue
                idx = struct.unpack(">I", body[:4])[0]
                if idx >= count or _bm_has(bm, idx):
                    continue
                data = aead.decrypt(_nonce(idx), body[4:], None)
                f.seek(idx * chunk)
                f.write(data)
                f.flush()
                _bm_set(bm, idx)
                got += 1
                v = spd.add(len(data))
                done = min(size, got * chunk)
                eta = int(max(0, size - done) / v) if v > 1 else None
                _upd(tid, done=done, speed=v, eta=eta)
                # 每块都落一次位图：断电最多重传一块（4MB），代价可接受
                if time.time() - last_save > 1.0 or got == count:
                    H._write_json(spath, {
                        "token": token, "size": size, "count": count,
                        "chunk": chunk, "sha": sha_want, "name": name,
                        "bm": _bm_b64(bm),
                    })
                    last_save = time.time()

        H._write_json(spath, {
            "token": token, "size": size, "count": count, "chunk": chunk,
            "sha": sha_want, "name": name, "bm": _bm_b64(bm),
        })

        # 整文件校验：任一块坏掉这里会不一致
        got_sha = _file_sha(part)
        if sha_want and got_sha != sha_want:
            _upd(tid, state="error", error="文件校验失败（可能传输损坏），请重传")
            return

        meta2 = H.save_file(gid, name, _read_all(part),
                            meta.get("mime") or "") if size <= H.MAX_FILE else None
        if meta2 is None:
            # 超过旧上限：直接搬进 files 目录并登记索引（os.replace，不进内存）
            meta2 = _adopt_big_file(gid, fid, name, part, size, sha_want)
        # 留一份「已完成」标记：重复点接收时直接判定完成，不必再连一次
        H._write_json(spath, {
            "token": token, "size": size, "count": count, "chunk": chunk,
            "sha": sha_want, "name": name, "bm": _bm_b64(bm), "finished": True,
            "fid_out": str(meta2.get("fid") or ""),
        })
        try:
            os.remove(part)      # 大文件已被 os.replace 移走，小文件在这删
        except Exception:
            pass
        _upd(tid, state="done", done=size, speed=0.0, eta=0, fid_out=meta2["fid"])
    except ConnectionAbortedError:
        _upd(tid, state="canceled", speed=0.0)
    except Exception as e:
        _upd(tid, state="error", error=str(e)[:160], speed=0.0)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _read_all(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _adopt_big_file(gid: str, fid: str, name: str, part: str,
                    size: int, sha: str) -> Dict[str, Any]:
    """大于旧上限的文件：直接登记进 files 索引（不经过内存）。"""
    me = H.profile()
    d = H.files_dir(gid)
    os.makedirs(d, exist_ok=True)
    safe = H._safe_name(name)
    # ⚠️ 共享「本机文件」时（share 的 fid 传空），连接码里没有 fid，
    #    这里必须自己生成一个。否则索引 key 是空字符串：
    #    前端拿到的 fid_out 为空 → 下载不了，而且第二个文件会覆盖第一个。
    #    只有 >MAX_FILE 的文件走这条分支，而这正是 P2P 的主要用途（G 级文件）。
    try:
        _idx = H._read_json(H._files_index_path(gid), {}) or {}
    except Exception:
        _idx = {}
    if not fid or fid in _idx:
        fid = "f_" + secrets.token_hex(8)
    dst = os.path.join(d, fid + "__" + safe)
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(part, dst)
    meta = {
        "fid": fid, "name": safe, "size": size, "mime": "",
        "kind": H.kind_of("", safe), "sha": (sha or "")[:16],
        "by": me["user_id"], "by_name": me["name"], "ts": time.time(),
    }
    with H._LOCK:
        idx = H._read_json(H._files_index_path(gid), {}) or {}
        idx[fid] = meta
        H._write_json(H._files_index_path(gid), idx)
    return meta


# ---------------------------------------------------------------- 控制
def pause(tid: str) -> bool:
    r = get(tid)
    if not r:
        return False
    _upd(tid, paused=True, state="paused", speed=0.0)
    return True


def resume(tid: str) -> bool:
    r = get(tid)
    if not r:
        return False
    _upd(tid, paused=False,
         state="transferring" if r.get("done") else "connecting")
    return True


def cancel(tid: str) -> bool:
    r = get(tid)
    if not r:
        return False
    ev = r.get("stop")
    if isinstance(ev, threading.Event):
        ev.set()
    _upd(tid, state="canceled", paused=False, speed=0.0)
    return True


def stop_all() -> None:
    _STOP.set()
    with _LOCK:
        for r in _TRANSFERS.values():
            ev = r.get("stop")
            if isinstance(ev, threading.Event):
                ev.set()


def local_ip() -> str:
    return _lan_ip()


# ---------------------------------------------------------------- 局域网自动发现
#
# 用户要「LLM 生成的东西同时分发到每个成员手上」。
# 公网上这做不到全自动 —— 两台 NAT 后面的机器互相不知道对方在哪，
# 连接码必须人工传一次（这是 TCP/IP 的限制，不是实现问题）。
# **但同一个局域网内可以全自动**：共享者每 2 秒发一次 UDP 广播，
# 其他人收到就知道「谁在共享哪个文件」，点一下就传。
#
# 广播内容不含文件内容，也不含群密钥，只有 gid / fid / 端口 / 一次性 token 前缀。

BEACON_PORT = 47899
BEACON_MAGIC = b"HIVEP2P1"
_BEACONS: Dict[str, Dict[str, Any]] = {}   # fid -> {gid,name,size,host,port,ts,from}
_BEACON_STOP = threading.Event()


def _beacon_sender(tid: str, gid: str, fid: str, name: str, size: int,
                   port: int, token: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({
        "gid": gid, "fid": fid, "name": name, "size": size,
        "port": port, "t": token, "from": _lan_ip(), "ts": time.time(),
        # ⚠️ from 是 IP，用来给对端连；uid 才是「谁发的」，用来过滤自己。
        #    早先只放了 from 却拿它比 user_id，永远不相等 → 自己也会看到自己的广播。
        "uid": (H.profile().get("user_id") or ""),
    }, ensure_ascii=False).encode("utf-8")
    try:
        while not _BEACON_STOP.is_set():
            with _LOCK:
                alive = (_TRANSFERS.get(tid) or {}).get("state") in (
                    "listening", "transferring", "paused")
            if not alive:
                break
            try:
                s.sendto(BEACON_MAGIC + payload, ("255.255.255.255", BEACON_PORT))
            except Exception:
                pass
            time.sleep(2.0)
    finally:
        s.close()


def _beacon_listener() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", BEACON_PORT))
    except Exception:
        return          # 端口被占（比如本机已有别的实例）：放弃自动发现，不影响手动传
    s.settimeout(1.0)
    me = ""
    try:
        me = H.profile().get("user_id") or ""
    except Exception:
        pass
    while not _BEACON_STOP.is_set():
        try:
            data, addr = s.recvfrom(8192)
        except socket.timeout:
            continue
        except Exception:
            break
        if not data.startswith(BEACON_MAGIC):
            continue
        try:
            d = json.loads(data[len(BEACON_MAGIC):].decode("utf-8"))
        except Exception:
            continue
        if me and d.get("uid") == me:
            continue          # 自己发的，忽略（比的是 uid，不是 IP）
        fid = str(d.get("fid") or "")
        if not fid:
            continue
        with _LOCK:
            _BEACONS[fid] = {**d, "seen": time.time()}


def start_discovery() -> None:
    """应用启动时调一次。失败了也不影响别的功能。"""
    threading.Thread(target=_beacon_listener, daemon=True).start()


def beacons(gid: str = "", max_age: float = 12.0) -> List[Dict[str, Any]]:
    """最近看到的局域网共享（12 秒内有效）。前端据此提示「谁在共享什么」。"""
    now = time.time()
    with _LOCK:
        out = [dict(v) for k, v in _BEACONS.items()
               if now - float(v.get("seen") or 0) <= max_age]
        for k in [k for k, v in _BEACONS.items()
                  if now - float(v.get("seen") or 0) > max_age * 4]:
            _BEACONS.pop(k, None)
    if gid:
        out = [b for b in out if b.get("gid") == gid]
    # 直接把连接码拼好给前端 —— 局域网里点一下就能收，不用手抄
    for b in out:
        try:
            b["code"] = make_code(b["gid"], b["fid"], b.get("from") or "",
                                  int(b.get("port") or 0), str(b.get("t") or ""))
        except Exception:
            b["code"] = ""
    return sorted(out, key=lambda b: b.get("seen") or 0, reverse=True)
