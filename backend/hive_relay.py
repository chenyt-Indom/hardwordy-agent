"""Hive 中继服务 —— 部署在你自己服务器上的那台（腾讯云）。

它做什么、不做什么，界限很重要：

**做**：
- 给同一个群号的人转发消息（房间广播）
- 给消息定序（分配全局 seq）—— 这是「大家进度一致」的唯一依据
- 落盘密文日志，重启后 seq 不重置、历史不丢

**不做**：
- **看不懂任何消息内容**。客户端发上来的消息体是 AES-GCM 密文，
  群密钥只在成员之间通过二维码传递，服务器上没有。
- 不存成员昵称以外的任何个人资料。

部署（腾讯云 Ubuntu/CentOS 都行）：
    pip install websockets
    python hive_relay.py --host 0.0.0.0 --port 8765 --data /var/lib/hive
    # 建议放在 nginx 后面加 TLS：wss://你的域名/hive

客户端在应用里填 `ws://你的IP:8765`（或 `wss://域名/hive`）就能连上。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any, Dict, List, Set

import websockets
from websockets.server import WebSocketServerProtocol

# ---------------------------------------------------------------- 存储

_DATA = "./relay_data"
_LOCK = asyncio.Lock()


def _gpath(gid: str) -> str:
    safe = "".join(c for c in gid if c.isalnum() or c in "-_") or "unknown"
    return os.path.join(_DATA, safe + ".jsonl")


def _seq_path(gid: str) -> str:
    return _gpath(gid) + ".seq"


def _load_seq(gid: str) -> int:
    try:
        with open(_seq_path(gid), "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _save_seq(gid: str, seq: int) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _seq_path(gid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(seq))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _seq_path(gid))


def _append(gid: str, rec: Dict[str, Any]) -> None:
    os.makedirs(_DATA, exist_ok=True)
    with open(_gpath(gid), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_since(gid: str, since: int, limit: int = 300) -> List[Dict[str, Any]]:
    path = _gpath(gid)
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("seq") or 0) > since:
                    out.append(r)
    except Exception:
        return []
    out.sort(key=lambda x: x.get("seq") or 0)
    return out[-limit:] if limit else out


# ---------------------------------------------------------------- 房间

class Room:
    """一个群 = 一个房间。"""

    def __init__(self, gid: str):
        self.gid = gid
        self.clients: Set[WebSocketServerProtocol] = set()
        self.members: Dict[str, Dict[str, Any]] = {}  # uid -> {name, joined}
        self.seq = _load_seq(gid)

    def join(self, ws: WebSocketServerProtocol, uid: str, name: str) -> None:
        self.clients.add(ws)
        self.members[uid] = {"uid": uid, "name": name, "joined": time.time()}

    def leave(self, ws: WebSocketServerProtocol, uid: str) -> None:
        self.clients.discard(ws)
        # 还有别的连接挂着这个 uid 才保留
        still = any(getattr(c, "_hive_uid", None) == uid for c in self.clients)
        if not still:
            self.members.pop(uid, None)

    async def broadcast(self, payload: Dict[str, Any], skip: Any = None) -> None:
        if not self.clients:
            return
        data = json.dumps(payload, ensure_ascii=False)
        dead = []
        for c in list(self.clients):
            if c is skip:
                continue
            try:
                await c.send(data)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)


ROOMS: Dict[str, Room] = {}


def _room(gid: str) -> Room:
    r = ROOMS.get(gid)
    if r is None:
        r = Room(gid)
        ROOMS[gid] = r
    return r


# ---------------------------------------------------------------- 连接处理

async def handler(ws: WebSocketServerProtocol) -> None:
    room: Room | None = None
    uid = ""
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t = m.get("t")

            if t == "join":
                gid = (m.get("gid") or "").strip()
                uid = (m.get("uid") or "").strip()
                name = (m.get("name") or "成员").strip()[:24]
                if not gid or not uid:
                    await ws.send(json.dumps({"t": "error", "msg": "join 缺 gid/uid"},
                                             ensure_ascii=False))
                    continue
                ws._hive_uid = uid  # type: ignore[attr-defined]
                room = _room(gid)
                room.join(ws, uid, name)
                since = int(m.get("since") or 0)
                hist = _read_since(gid, since)
                await ws.send(json.dumps({
                    "t": "joined", "gid": gid, "seq": room.seq,
                    "members": list(room.members.values()),
                    "msgs": hist,
                }, ensure_ascii=False))
                await room.broadcast({
                    "t": "presence", "members": list(room.members.values()),
                }, skip=ws)
                print(f"[join] {gid} {name}({uid}) 在线 {len(room.clients)} 人")

            elif t == "msg" and room is not None:
                # 消息体是密文，服务器不解析、只定序
                async with _LOCK:
                    room.seq += 1
                    rec = {
                        "seq": room.seq,
                        "mid": (m.get("mid") or "")[:40],
                        "uid": uid,
                        "blob": m.get("blob") or "",
                        "ts": float(m.get("ts") or time.time()),
                    }
                    _append(room.gid, rec)
                    _save_seq(room.gid, room.seq)
                await room.broadcast({"t": "msg", **rec}, skip=None)

            elif t == "ping":
                await ws.send(json.dumps({"t": "pong", "ts": time.time()}))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[err] {e}")
    finally:
        if room is not None:
            room.leave(ws, uid)
            await room.broadcast({"t": "presence",
                                  "members": list(room.members.values())})
            print(f"[leave] {room.gid} {uid} 在线 {len(room.clients)} 人")


async def main() -> None:
    global _DATA
    ap = argparse.ArgumentParser(description="Hive 中继服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default="./relay_data")
    ap.add_argument("--max-size", type=int, default=4 * 1024 * 1024,
                    help="单条消息上限（字节），默认 4MB，够放一段代码或小图")
    args = ap.parse_args()
    _DATA = args.data
    os.makedirs(_DATA, exist_ok=True)
    print(f"Hive 中继已启动 ws://{args.host}:{args.port}  数据目录 {os.path.abspath(_DATA)}")
    print("提示：服务器上放行这个端口；生产环境建议前面挂 nginx + TLS，用 wss://")
    async with websockets.serve(handler, args.host, args.port,
                                max_size=args.max_size, ping_interval=20,
                                ping_timeout=20):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
