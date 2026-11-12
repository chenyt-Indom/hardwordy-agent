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
        # uid -> {uid, name, joined, online, last_seen, v, caps}
        # ⚠️ 离线成员**不立刻删** —— 删了界面上就分不清「他离线了」
        #    和「这个人从没进过群」，也没法显示「最后在线 3 分钟前」。
        self.members: Dict[str, Dict[str, Any]] = {}
        self.seq = _load_seq(gid)

    def join(self, ws: WebSocketServerProtocol, uid: str, name: str,
             ver: int = 1, caps: Any = None) -> None:
        self.clients.add(ws)
        old = self.members.get(uid) or {}
        self.members[uid] = {
            "uid": uid,
            "name": name,
            "joined": old.get("joined") or time.time(),
            "last_seen": time.time(),
            "online": True,
            "v": int(ver or 1),          # 对方客户端协议版本
            "caps": list(caps or [])[:16],
        }

    def leave(self, ws: Any, uid: str) -> None:
        self.clients.discard(ws)
        CONN_UID.pop(ws, None)
        # 还有别的连接挂着这个 uid 才保留（同一人可能开了两个窗口）
        still = any(CONN_UID.get(c) == uid for c in self.clients)
        if not still:
            m = self.members.get(uid)
            if m:
                m["online"] = False
                m["last_seen"] = time.time()
            # 超过 7 天的离线成员才真正移除，避免 members 无限增长
            cutoff = time.time() - 7 * 86400
            for k in [k for k, v in self.members.items()
                      if not v.get("online")
                      and (v.get("last_seen") or 0) < cutoff]:
                self.members.pop(k, None)

    def presence(self) -> List[Dict[str, Any]]:
        """给界面用的成员快照：在线/离线 + 版本，按在线优先排序。"""
        ms = list(self.members.values())
        ms.sort(key=lambda x: (not x.get("online"), x.get("name") or ""))
        return ms

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
# 连接 → 用户 ID。websockets 17 的 ServerConnection 不适合挂自定义属性，
# 所以用独立映射表（leave 时按它判断"这个用户还有没有别的连接"）。
CONN_UID: Dict[Any, str] = {}


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
                CONN_UID[ws] = uid
                room = _room(gid)
                # 版本协商：老客户端不带 v，按 v1 处理（完全兼容）；
                # 比中继新的客户端也照收，只是标记出来让界面提示。
                ver = int(m.get("v") or 1)
                room.join(ws, uid, name, ver, m.get("caps"))
                since = int(m.get("since") or 0)
                hist = _read_since(gid, since)
                await ws.send(json.dumps({
                    "t": "joined", "gid": gid, "seq": room.seq,
                    "members": room.presence(),
                    "msgs": hist,
                    "proto": PROTO_VER,            # 中继自己支持的协议版本
                    "relay_ver": _RELAY_VER,
                    "your_v": ver,
                }, ensure_ascii=False))
                await room.broadcast({
                    "t": "presence", "members": room.presence(),
                }, skip=ws)
                print(f"[join] {gid} {name}({uid}) v{ver} 在线 {len(room.clients)} 人")

            elif t == "msg" and room is not None:
                # 消息体是密文，服务器不解析、只定序
                blob = m.get("blob") or ""
                if not blob:
                    await ws.send(json.dumps(
                        {"t": "error", "code": "empty_msg", "msg": "消息体是空的"},
                        ensure_ascii=False))
                    continue
                async with _LOCK:
                    room.seq += 1
                    rec = {
                        "seq": room.seq,
                        "mid": (m.get("mid") or "")[:40],
                        "uid": uid,
                        "blob": blob,
                        "ts": float(m.get("ts") or time.time()),
                        "v": int(m.get("v") or 1),   # 发送者的协议版本
                    }
                    _append(room.gid, rec)
                    _save_seq(room.gid, room.seq)
                await room.broadcast({"t": "msg", **rec}, skip=None)

            elif t == "stream" and room is not None:
                # @hd 执行进度帧：**只转发、不落盘、不定序**。
                # 为什么不定序：一次任务会推几百个增量帧，如果也走 seq，
                #   正式消息的 seq 会被进度帧吃掉，增量拉取就乱了。
                # 为什么不落盘：进度是过程数据，最终会落一条完整的 ai 消息，
                #   过程帧存下来只会让消息日志膨胀几十倍。
                # 中继依然看不到内容（blob 是密文），只负责转发。
                if not m.get("blob"):
                    continue
                await room.broadcast({
                    "t": "stream",
                    "gid": room.gid,
                    "mid": (m.get("mid") or "")[:40],
                    "blob": m.get("blob"),
                    "ts": float(m.get("ts") or time.time()),
                    "v": int(m.get("v") or 1),
                }, skip=None)

            elif t == "ping":
                # 客户端用心跳测延迟。回 ts 让对面算 RTT。
                await ws.send(json.dumps({
                    "t": "pong", "ts": time.time(),
                    "echo": m.get("ts"), "proto": PROTO_VER,
                }))

            elif t == "hello":
                # 只探活、不加入房间（用于"这个地址通不通"的轻量检查）
                await ws.send(json.dumps({
                    "t": "welcome", "svc": "hive-relay",
                    "proto": PROTO_VER, "relay_ver": _RELAY_VER,
                    "rooms": len(ROOMS), "ts": time.time(),
                }, ensure_ascii=False))

            else:
                # ⚠️ 不认识的类型**不踢连接、不静默丢弃**。
                # 可能是更新版本的客户端发了中继还不懂的东西 ——
                # 回一条 error 让对方知道，但连接保留，其余功能照常。
                await ws.send(json.dumps({
                    "t": "error", "code": "unknown_type",
                    "msg": f"不认识的消息类型「{t}」"
                           f"（中继 v{_RELAY_VER} / proto {PROTO_VER}）",
                }, ensure_ascii=False))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[err] {e}")
    finally:
        if room is not None:
            room.leave(ws, uid)
            await room.broadcast({"t": "presence", "members": room.presence()})
            print(f"[leave] {room.gid} {uid} 在线 {len(room.clients)} 人")


_START_TS = time.time()
_RELAY_VER = "1.2"
# 中继支持的协议版本。客户端 join 时带自己的 v，双方各自比较，
# 不一致也不拒绝 —— 只提示，能通的照样通（跨版本互通的底线）。
PROTO_VER = 2


async def _process_request(connection: Any, request: Any) -> Any:
    """处理 HTTP 请求：只提供 /health，用来让客户端验证「中继是否活着」。

    普通 WS 握手（Upgrade: websocket）直接放行，返回 None 交给 websockets 自己处理。
    """
    try:
        path = (getattr(request, "path", "") or "").split("?")[0]
        if path not in ("/health", "/healthz", "/"):
            return None
        hdrs = str(getattr(request, "headers", "")).lower()
        if "upgrade" in hdrs and "websocket" in hdrs:
            return None          # 这是 WS 握手，交给 websockets
        body = json.dumps({
            "ok": True, "svc": "hive-relay", "ver": _RELAY_VER,
            "proto": PROTO_VER,
            "rooms": len(ROOMS), "uptime": round(time.time() - _START_TS, 1),
            "ts": time.time(),
        }, ensure_ascii=False).encode("utf-8")
        try:
            from websockets.datastructures import Headers
            from websockets.http11 import Response
            h = Headers()
            h["Content-Type"] = "application/json; charset=utf-8"
            h["Access-Control-Allow-Origin"] = "*"
            h["Content-Length"] = str(len(body))
            return Response(200, "OK", h, body)
        except Exception:
            return connection.respond(200, body.decode("utf-8"))
    except Exception:
        return None


def set_data_dir(path: str) -> None:
    """给「内置中继」用：把落盘目录指到应用自己的 data 下。"""
    global _DATA
    if path:
        _DATA = path
        try:
            os.makedirs(_DATA, exist_ok=True)
        except Exception:
            pass


class FastAPIWSAdapter:
    """把 starlette 的 WebSocket 包装成 websockets 的用法。

    为什么要这层：`handler()` 是按 websockets 库写的（`async for raw in ws`
    + `await ws.send()`），而 FastAPI 的 WebSocket 是 `receive_text()` /
    `send_text()`。有了这个薄壳，同一份 handler 既能给独立中继进程用，
    也能挂进本应用的 `/hive` 路由 —— 不用维护两套转发逻辑。

    2026-11-13：加它是为了解决「云服务器上只跑了应用、没跑中继」这个现实问题：
    把中继挂进应用，就只占一个 443、不用开 8765、不用改 nginx。
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: Any):
        self._ws = ws

    # 要能当 dict key / 放进 set（CONN_UID 和 Room.clients 都靠它）
    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: Any) -> bool:
        return self is other

    async def send(self, data: str) -> None:
        await self._ws.send_text(data)

    async def recv(self) -> str:
        return await self._ws.receive_text()

    def __aiter__(self) -> "FastAPIWSAdapter":
        return self

    async def __anext__(self) -> str:
        # 断开时 starlette 抛 WebSocketDisconnect；handler 里只捕
        # websockets 的 ConnectionClosed，所以这里统一转成 StopAsyncIteration，
        # 让 for 循环正常结束（效果等价：连接没了，退出循环）。
        try:
            return await self._ws.receive_text()
        except Exception:
            raise StopAsyncIteration

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass


async def handle_fastapi(ws: Any, data_dir: str = "") -> None:
    """FastAPI 路由的入口：适配后直接复用 handler()。"""
    if data_dir:
        set_data_dir(data_dir)
    await handler(FastAPIWSAdapter(ws))


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
                                ping_timeout=20,
                                process_request=_process_request):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
