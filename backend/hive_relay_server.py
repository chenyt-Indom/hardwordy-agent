"""Hive 中继服务器 —— 跑在公网服务器上，只做三件事。

它做什么
--------
1. **认人**：HMAC 挑战应答验证中继密钥。密钥本身**从不在网络上传输**，
   抓包也拿不到（服务器发随机数，客户端回 HMAC）。
2. **转发**：把任意成员的一帧密文**立即**写给同群所有在线成员。
   不是轮询、不是定时拉取 —— 收到就转，延迟就是一次 TCP 往返。
3. **补发**：每帧分配单调递增的 seq 并落盘。成员重新上线时报「我看到哪了」，
   服务器把缺的补给他。所以掉线期间的消息不会丢。

它看不到什么
------------
**看不到任何聊天内容。** 转发的是 mesh 层的 AES-256-GCM 密文，
群密钥只在成员之间传递，服务器上没有。服务器也不认识发送者是谁。

为什么零依赖
------------
服务器上只需要 `python3` 就能跑，不用 pip install 任何东西 ——
少一个依赖就少一个「部署时卡住」的理由。协议是自定义的定长头 + JSON，
不是 WebSocket（见 deploy/HIVE中继部署手册.md 里的说明）。

协议（双向都是 [4B 长度][1B 类型][载荷]）
----------------------------------------
客户端 → 服务器
    0 + JSON  {"op":"hello","gid":...,"mac":...,"since":N}
    0 + JSON  {"op":"ping"}
    1 + mesh 帧原始字节（[1B typ][4B rk][4B len][body]，密文）
服务器 → 客户端
    0 + JSON  {"op":"challenge","nonce":...}   握手第一步
    0 + JSON  {"op":"ready","seq":N}
    0 + JSON  {"op":"pong"} / {"op":"err","msg":...}
    2 + [8B seq][mesh 帧原始字节]               转发 / 补发
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time

PROTO = 1
MAX_PKT = 9 * 1024 * 1024        # 单包上限（mesh 帧 8MB + 9 字节帧头，留余量）
HIST_MAX = 5000                  # 内存里保留多少帧用于补发（约够一天的小群）
HDR = struct.Struct(">IB")       # 4B 长度 + 1B 类型

K_CTRL = 0                       # 控制 JSON
K_FRAME = 1                      # 客户端发来的 mesh 帧
K_SEQ = 2                        # 服务器下发的「带 seq 的 mesh 帧」

log = logging.getLogger("hive_relay")


# ---------------------------------------------------------------- 底层收发

def _recv_exact(sock: socket.socket, n: int):
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


# ---------------------------------------------------------------- 群房间

class Room:
    """一个群 = 一个房间。按 sha256(gid) 前 4 字节命名落盘文件。"""

    def __init__(self, gid: str, data_dir: str, max_log_mb: int = 64):
        self.gid = gid
        self.rk = hashlib.sha256(gid.encode("utf-8")).digest()[:4]
        self.lock = threading.Lock()
        self.conns: set = set()
        self.seq = 0
        self.hist: list = []          # [(seq, frame_bytes)]，只留最近 HIST_MAX 条
        self.max_log = max_log_mb * 1024 * 1024
        self.path = os.path.join(data_dir, self.rk.hex() + ".log")
        self._load()

    # -------------------------------------------------- 落盘

    def _load(self) -> None:
        """启动时恢复 seq 水位和最近的历史，用于给新上线的人补发。"""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except Exception as e:
            log.warning("读历史失败 %s: %s", self.path, e)
            return
        off = 0
        rec = struct.Struct(">QI")
        while off + rec.size <= len(data):
            try:
                seq, ln = rec.unpack(data[off:off + rec.size])
            except Exception:
                break
            off += rec.size
            if ln <= 0 or off + ln > len(data):
                break
            self.hist.append((seq, data[off:off + ln]))
            self.seq = max(self.seq, seq)
            off += ln
        if len(self.hist) > HIST_MAX:
            self.hist = self.hist[-HIST_MAX:]
        log.info("房间 %s 恢复 seq=%d 历史=%d 条", self.rk.hex(), self.seq, len(self.hist))

    def _append(self, seq: int, frame: bytes) -> None:
        try:
            with open(self.path, "ab") as f:
                f.write(struct.pack(">QI", seq, len(frame)))
                f.write(frame)
                f.flush()
        except Exception as e:
            log.warning("写历史失败: %s", e)

    def _maybe_compact(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_log:
                return
        except Exception:
            return
        keep = self.hist[-HIST_MAX:]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                for s, fr in keep:
                    f.write(struct.pack(">QI", s, len(fr)))
                    f.write(fr)
            os.replace(tmp, self.path)
            self.hist = keep
            log.info("房间 %s 日志已压缩到 %d 条", self.rk.hex(), len(keep))
        except Exception as e:
            log.warning("压缩失败: %s", e)

    # -------------------------------------------------- 收发

    def publish(self, sender, frame: bytes) -> int:
        """给同房间除发送者外的所有人**立即**投递。返回投递人数。"""
        with self.lock:
            self.seq += 1
            seq = self.seq
            self.hist.append((seq, frame))
            if len(self.hist) > HIST_MAX:
                self.hist = self.hist[-HIST_MAX:]
            targets = [c for c in self.conns if c is not sender and c.alive]
        self._append(seq, frame)
        self._maybe_compact()
        payload = struct.pack(">Q", seq) + frame
        n = 0
        for c in targets:
            if c.push(K_SEQ, payload):
                n += 1
        return n

    def replay(self, conn, since: int) -> int:
        """把 seq > since 的帧补发给他。"""
        with self.lock:
            todo = [(s, fr) for s, fr in self.hist if s > since]
        for s, fr in todo:
            conn.push(K_SEQ, struct.pack(">Q", s) + fr)
        return len(todo)


# ---------------------------------------------------------------- 连接

class Conn:
    """一条客户端连接。写用独立线程 + 队列，避免慢客户端卡住所有人。"""

    QMAX = 2048

    def __init__(self, sock: socket.socket, addr, srv: "RelayServer"):
        self.sock = sock
        self.addr = addr
        self.srv = srv
        self.gid = ""
        self.alive = True
        self.q: "queue.Queue" = queue.Queue(maxsize=self.QMAX)
        self.dropped = 0
        self.sent = 0
        self.recv = 0
        self.since = 0
        self.joined_at = time.time()
        self._w = threading.Thread(target=self._writer, daemon=True)
        self._w.start()

    def push(self, kind: int, payload: bytes) -> bool:
        if not self.alive:
            return False
        try:
            self.q.put_nowait((kind, payload))
            return True
        except queue.Full:
            # 慢客户端：丢最旧的一帧而不是断开 —— 断开会让他重连再补，
            # 反而制造更大的流量尖峰。丢帧由 mesh 层的 mid 去重兜底。
            try:
                self.q.get_nowait()
                self.q.put_nowait((kind, payload))
                self.dropped += 1
            except Exception:
                pass
            return False

    def _writer(self) -> None:
        while self.alive:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            kind, payload = item
            if not _send_pkt(self.sock, kind, payload):
                break
            self.sent += len(payload)
        self.close()

    def ctrl(self, obj: dict) -> bool:
        return self.push(K_CTRL, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def graceful_close(self, timeout: float = 1.2) -> None:
        """先把队列里剩下的包发完再关连接。

        ⚠️ 为什么需要它：拒绝连接时会 `ctrl({"op":"err"})` 然后 return，
        紧接着 finally 里就把 socket 关了 —— 队列里的 err 包**还没发出去**。
        客户端看到的现象是「连上了、一句话没说就断了」，完全不知道是密钥错。
        实测就是这个原因（错误密钥那条用例收不到任何回包）。
        """
        if not self.alive:
            return
        try:
            self.q.put_nowait(None)
        except Exception:
            pass
        try:
            self._w.join(timeout)
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.q.put_nowait(None)
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.srv.leave(self)


# ---------------------------------------------------------------- 服务器

class RelayServer:
    def __init__(self, host: str, port: int, data_dir: str, token: str,
                 max_log_mb: int = 64):
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.token = token.encode("utf-8") if token else b""
        self.max_log_mb = max_log_mb
        self.rooms: dict = {}
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.stop_flag = False
        self._srv = None
        self.stats = {"conns": 0, "auth_fail": 0, "frames": 0, "bytes": 0,
                      "replayed": 0}
        os.makedirs(data_dir, exist_ok=True)

    # -------------------------------------------------- 房间

    def room(self, gid: str) -> Room:
        with self.lock:
            r = self.rooms.get(gid)
            if r is None:
                r = Room(gid, self.data_dir, self.max_log_mb)
                self.rooms[gid] = r
            return r

    def leave(self, conn: Conn) -> None:
        if not conn.gid:
            return
        r = self.rooms.get(conn.gid)
        if r:
            with r.lock:
                r.conns.discard(conn)
        log.info("断开 %s 群=%s", conn.addr, (conn.gid or "")[:8])

    # -------------------------------------------------- 主循环

    def serve_forever(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(64)
        s.settimeout(1.0)
        self._srv = s
        log.info("中继已启动 %s:%d  鉴权=%s  数据=%s",
                 self.host, self.port, "开" if self.token else "关（不推荐）", self.data_dir)
        while not self.stop_flag:
            try:
                c, addr = s.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            c.settimeout(60.0)
            try:
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            threading.Thread(target=self._handle, args=(c, addr), daemon=True).start()
        try:
            s.close()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop_flag = True
        with self.lock:
            rooms = list(self.rooms.values())
        for r in rooms:
            # ⚠️ 必须在**锁外**关连接：close() 会回调 srv.leave()，而 leave()
            # 又要拿同一把 r.lock —— 持锁调用就是死锁（实测进程卡住不退出）。
            with r.lock:
                conns = list(r.conns)
            for c in conns:
                c.close()

    # -------------------------------------------------- 单连接

    def _handle(self, sock: socket.socket, addr) -> None:
        conn = Conn(sock, addr, self)
        self.stats["conns"] += 1
        try:
            self._serve_conn(conn)
        except Exception as e:
            log.debug("连接异常 %s: %s", addr, e)
        finally:
            conn.graceful_close(0.8)

    def _serve_conn(self, conn: Conn) -> None:
        nonce = os.urandom(16).hex()
        if not conn.ctrl({"op": "challenge", "nonce": nonce, "ver": PROTO}):
            return
        got = _recv_pkt(conn.sock)
        if not got:
            return
        kind, body = got
        if kind != K_CTRL:
            conn.ctrl({"op": "err", "msg": "握手格式不对"})
            return
        try:
            hello = json.loads(body.decode("utf-8"))
        except Exception:
            conn.ctrl({"op": "err", "msg": "握手不是 JSON"})
            return

        op = str(hello.get("op") or "")
        if op == "health":
            conn.ctrl(self.health())
            return
        if op != "hello":
            conn.ctrl({"op": "err", "msg": "先 hello"})
            return

        gid = str(hello.get("gid") or "").strip()
        if not gid or len(gid) > 128:
            conn.ctrl({"op": "err", "msg": "gid 不合法"})
            return

        # 鉴权：mac = HMAC-SHA256(token, nonce + "|" + gid)
        if self.token:
            want = hmac.new(self.token,
                            (nonce + "|" + gid).encode("utf-8"),
                            hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, str(hello.get("mac") or "")):
                self.stats["auth_fail"] += 1
                conn.ctrl({"op": "err", "msg": "中继密钥不对"})
                log.warning("鉴权失败 %s", conn.addr)
                return

        room = self.room(gid)
        conn.gid = gid
        try:
            conn.since = int(hello.get("since") or 0)
        except Exception:
            conn.since = 0

        with room.lock:
            room.conns.add(conn)
            room_seq = room.seq
        conn.ctrl({"op": "ready", "seq": room_seq, "ver": PROTO,
                   "peers": max(0, len(room.conns) - 1)})
        log.info("加入 %s 群=%s 人数=%d since=%d", conn.addr, gid[:8],
                 len(room.conns), conn.since)

        if conn.since >= 0:
            n = room.replay(conn, conn.since)
            if n:
                self.stats["replayed"] += n

        while conn.alive and not self.stop_flag:
            got = _recv_pkt(conn.sock)
            if not got:
                break
            kind, payload = got
            if kind == K_CTRL:
                try:
                    c = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if c.get("op") == "ping":
                    conn.ctrl({"op": "pong", "t": time.time()})
                elif c.get("op") == "health":
                    conn.ctrl(self.health())
                continue
            if kind != K_FRAME:
                continue
            # ★ 核心：收到就立刻转发，不做任何解析
            self.stats["frames"] += 1
            self.stats["bytes"] += len(payload)
            conn.recv += len(payload)
            room.publish(conn, payload)

    # -------------------------------------------------- 状态

    def health(self) -> dict:
        with self.lock:
            rooms = list(self.rooms.values())
        return {
            "op": "health", "ok": True, "ver": PROTO,
            "uptime": round(time.time() - self.started_at, 1),
            "rooms": len(rooms),
            "clients": sum(len(r.conns) for r in rooms),
            "frames": self.stats["frames"],
            "replayed": self.stats["replayed"],
            "auth_fail": self.stats["auth_fail"],
        }


# ---------------------------------------------------------------- 入口

def _read_token(path: str, inline: str) -> str:
    if inline:
        return inline.strip()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            log.error("读密钥文件失败 %s: %s", path, e)
    env = os.environ.get("HIVE_RELAY_TOKEN", "").strip()
    return env


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hive 群聊中继服务器")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default="./hive_data")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--token", default="", help="直接给密钥（不推荐，会留在进程列表里）")
    ap.add_argument("--max-log-mb", type=int, default=64)
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    token = _read_token(a.token_file, a.token)
    if not token:
        log.warning("没有配置中继密钥 —— 任何人都能连上来。生产环境请用 --token-file。")

    srv = RelayServer(a.host, a.port, a.data, token, a.max_log_mb)

    def _stop(signum, frame):
        log.info("收到信号 %s，准备退出", signum)
        srv.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except Exception:
            pass

    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
