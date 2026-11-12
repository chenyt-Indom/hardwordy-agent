"""Hive 客户端 —— 连中继、收发消息、断线自动重连。

运行在**应用自己的进程里**（不占额外端口），和单机模式共用同一个 FastAPI。

几个刻意的设计：

1. **后台线程 + asyncio**：FastAPI 是同步/异步混合的，这里用一个独立线程跑
   asyncio 事件循环，前端要发消息时用 `call_soon_threadsafe` 塞进去，互不阻塞。
2. **seq 以中继为准**：消息发出去后不立刻落盘，等中继回执（带全局 seq）再写，
   这样多台机器看到的顺序完全一致。
3. **断线不丢**：连不上时消息进内存队列，重连后按原 mid 补发；中继侧按 mid 去重。
4. **不解析内容**：客户端只做「加密 → 发」「收 → 解密 → 落盘」，没有别的逻辑。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import backend.hive as H

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None


class HiveClient:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._running = False
        self._connected = False
        self._gid = ""
        self._url = ""
        self._since: Dict[str, int] = {}
        self._queue: List[Dict[str, Any]] = []   # 待补发
        self._seen: set = set()                  # 已落盘的 mid，用于去重
        self._members: List[Dict[str, Any]] = []
        self._last_error = ""
        self._last_error_at = 0.0
        self._retry = 0
        self.on_message: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.on_status: Optional[Callable[[Dict[str, Any]], None]] = None
        self._lock = threading.RLock()

        # ---- 跨版本 / 状态显示相关（2026-11-13）----
        # state 是给界面直接用的四态，比「connected 布尔」信息量大：
        #   idle      没配中继，本机模式
        #   connecting  正在连（首次连接或重连中）
        #   online    连上了，能收发
        #   offline   曾经连上过、现在断了（会自动重试）
        #   error     连不上，且拿到了明确原因（DNS/端口/协议不符…）
        self._state = "idle"
        self._proto = H.PROTO_VER          # 本机协议版本
        self._relay_proto = 0              # 中继报告的协议版本（0=不知道）
        self._relay_ver = ""               # 中继软件版本号，如 1.2
        self._latency = 0.0                # 最近一次心跳 RTT（毫秒）
        self._last_recv = 0.0              # 最后一次收到任何帧的时间
        self._connected_at = 0.0           # 本次连上的时刻
        self._last_ping = 0.0
        self._ever_connected = False

    # ------------------------------------------------------------ 状态

    def _calc_state(self) -> str:
        if not self._running:
            return "idle"
        if self._connected:
            return "online"
        # 连过又断了 → offline（会自动重连）；从没连上且报过错 → error
        if self._ever_connected or self._retry <= 1:
            return "offline"
        return "error"

    def _compat(self) -> str:
        """本机协议版本 vs 中继协议版本。

        不一致**不影响通讯**（协议是只增不改的），只是让用户知道
        「对面比我老/新，某些新功能可能没有」。
        """
        if not self._relay_proto:
            return "unknown"
        if self._relay_proto == self._proto:
            return "same"
        return "relay_older" if self._relay_proto < self._proto else "relay_newer"

    def status(self) -> Dict[str, Any]:
        online = [m for m in self._members if m.get("online")]
        now = time.time()
        return {
            "running": self._running,
            "connected": self._connected,
            "state": self._calc_state(),
            "url": self._url,
            "gid": self._gid,
            "members": self._members,
            "online_count": len(online),
            "total_count": len(self._members),
            "queued": len(self._queue),
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "retry": self._retry,
            # ---- 跨版本 ----
            "proto": self._proto,
            "relay_proto": self._relay_proto,
            "relay_ver": self._relay_ver,
            "compat": self._compat(),
            # ---- 健康度 ----
            "latency_ms": round(self._latency, 1),
            "last_recv_ts": self._last_recv,
            "connected_secs": round(now - self._connected_at, 0) if self._connected_at else 0,
        }

    def _emit_status(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.status())
            except Exception:
                pass

    # ------------------------------------------------------------ 启停

    def start(self, url: str, gid: str) -> Dict[str, Any]:
        """连接中继。重复调用会先停掉旧的连接。"""
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "没有填中继地址"}
        if websockets is None:
            return {"ok": False, "error": "缺少 websockets 库"}
        if self._running and url == self._url and gid == self._gid:
            return {"ok": True, "already": True, **self.status()}
        self.stop()
        self._url, self._gid = url, gid
        self._running = True
        self._last_error = ""
        self._retry = 0
        self._thread = threading.Thread(target=self._run, name="hive-client", daemon=True)
        self._thread.start()
        # 等最多 3 秒，让状态尽快反映到界面
        for _ in range(30):
            if self._connected:
                break
            time.sleep(0.1)
        return {"ok": True, **self.status()}

    def stop(self) -> Dict[str, Any]:
        self._running = False
        loop = self._loop
        if loop and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), loop)
            except Exception:
                pass
        self._connected = False
        self._ws = None
        self._emit_status()
        return {"ok": True}

    # ------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._forever())
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._connected = False

    async def _forever(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self._url, ping_interval=20, ping_timeout=20,
                    max_size=8 * 1024 * 1024, open_timeout=8,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._retry = 0
                    backoff = 1.0
                    me = H.profile()
                    since = max(self._since.values()) if self._since else 0
                    await ws.send(json.dumps({
                        "t": "join", "gid": self._gid,
                        "uid": me["user_id"], "name": me["name"],
                        "since": since,
                    }, ensure_ascii=False))
                    self._emit_status()
                    await self._flush_queue(ws)
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle(ws, m)
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                self._connected = False
                self._emit_status()
            else:
                self._connected = False
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, ws, m: Dict[str, Any]) -> None:
        t = m.get("t")
        if t == "joined":
            self._members = m.get("members") or []
            self._connected = True
            for rec in (m.get("msgs") or []):
                self._store(rec)
            self._emit_status()
        elif t == "presence":
            self._members = m.get("members") or []
            self._emit_status()
        elif t == "msg":
            self._store(m)
        elif t == "pong":
            pass
        elif t == "error":
            self._last_error = str(m.get("msg") or "")
            self._emit_status()

    # ------------------------------------------------------------ 落盘 / 回调

    def _store(self, rec: Dict[str, Any]) -> None:
        """把中继发来的密文解开，写进本地日志，并通知前端。"""
        mid = rec.get("mid") or ""
        if mid and mid in self._seen:
            return
        gid = self._gid
        key = H.group_key(gid)
        body = H._open(key, rec.get("blob") or "") if key else None
        if not isinstance(body, dict):
            return
        msg = {
            "seq": rec.get("seq") or 0,
            "id": mid or ("m_" + str(rec.get("seq"))),
            "from": rec.get("uid") or body.get("from") or "?",
            "name": body.get("name") or "成员",
            "type": body.get("type") or "text",
            "text": body.get("text") or "",
            "ts": rec.get("ts") or time.time(),
        }
        if body.get("extra"):
            msg.update(body["extra"])
        with self._lock:
            if mid:
                self._seen.add(mid)
            try:
                H._append_raw(gid, msg)
                g = H.load_group(gid)
                if g and (rec.get("seq") or 0) > (g.get("seq") or 0):
                    g["seq"] = rec["seq"]
                    H._write_json(H._group_path(gid), g)
            except Exception:
                pass
        if self.on_message:
            try:
                self.on_message(gid, msg)
            except Exception:
                pass

    # ------------------------------------------------------------ 发送

    def send(self, gid: str, text: str, mtype: str = "text",
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        me = H.profile()
        key = H.group_key(gid)
        if not key:
            return {"ok": False, "error": "这个群没有密钥，无法加密发送"}
        mid = "m_" + str(int(time.time() * 1000))[-9:] + str(abs(hash(text)) % 10000)
        body = {"name": me["name"], "type": mtype, "text": text,
                "from": me["user_id"], "ts": time.time()}
        if extra:
            body["extra"] = extra
        rec = {"t": "msg", "gid": gid, "mid": mid,
               "blob": H._seal(key, body), "ts": time.time()}

        if not self._connected or not self._loop or not self._ws:
            # 离线：先进队列，重连后补发；同时本地先落一条（标 pending）
            with self._lock:
                self._queue.append(rec)
            msg = {
                "seq": -1, "id": mid, "from": me["user_id"], "name": me["name"],
                "type": mtype, "text": text, "ts": time.time(), "pending": True,
            }
            if extra:
                msg.update(extra)
            try:
                H._append_raw(gid, msg)
            except Exception:
                pass
            return {"ok": True, "queued": True, "mid": mid, "msg": msg}

        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(rec, ensure_ascii=False)), self._loop
            ).result(timeout=5)
        except Exception as e:
            with self._lock:
                self._queue.append(rec)
            return {"ok": True, "queued": True, "error": str(e), "mid": mid}
        return {"ok": True, "mid": mid, "sent": True}

    async def _flush_queue(self, ws) -> None:
        with self._lock:
            pending, self._queue = self._queue, []
        for rec in pending:
            try:
                await ws.send(json.dumps(rec, ensure_ascii=False))
            except Exception:
                with self._lock:
                    self._queue.append(rec)
                break


# 全局单例
_client = HiveClient()


def client() -> HiveClient:
    return _client
