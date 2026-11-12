"""Hive 客户端 —— 门面层。

**这个文件已经不再连中继了。** 中继（hive_relay.py）已按需求删除，群聊改走
纯 P2P 的 `hive_mesh`：局域网自动发现、跨网络靠连接码、消息泛洪 + 去重。

保留这个文件是为了**不破坏 main.py 的调用**：路由层只认 `client()` 上的
8 个方法，换掉内部实现后路由一行都不用改。对外接口完全兼容：

    start(url, gid) / stop() / status()
    send / send_stream / send_file / send_cancel
    on_message / on_stream / on_cancel / on_file

url 的含义变了：
    "" 或 "lan"            → 局域网模式（UDP 广播自动找同群的人）
    "hive3://1.2.3.4:8899" → 主动连这个人（跨网络，人工传一次码）
    "ws://..." / "wss://..." → 旧的中继地址，明确报错提示改法
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any, Dict, List, Optional

import backend.hive as H
import backend.hive_mesh as MESH

PROTO_VER = MESH.PROTO_VER


class HiveClient:
    def __init__(self) -> None:
        self.on_message = None      # Callable[[str, dict], None]
        self.on_stream = None
        self.on_cancel = None
        self.on_file = None
        self.on_status = None

        self._state = "offline"
        self._gid = ""
        self._url = ""
        self._error = ""
        self._latency = 0.0
        self._started_at = 0.0
        self._last_ok = 0.0
        self._files: Dict[str, Dict[str, Any]] = {}   # fid -> 攒片状态
        self._flock = threading.Lock()
        self._hooked = False

    # ------------------------------------------------------------ 回调挂接

    def _hook(self) -> None:
        if self._hooked:
            return
        m = MESH.node()
        m.on_msg = self._on_msg
        m.on_stream = self._on_stream
        m.on_cancel = self._on_cancel
        m.on_file = self._on_file
        m.on_peer = self._on_peer
        m.on_members = self._on_members
        self._hooked = True

    def _on_members(self, gid: str) -> None:
        """别人同步了成员表过来 —— 让界面刷新一下成员列表。"""
        cb = getattr(self, "on_members", None)
        if cb:
            try:
                cb(gid)
            except Exception:
                pass

    def _on_msg(self, gid: str, msg: Dict[str, Any]) -> None:
        if self.on_message:
            try:
                self.on_message(gid, msg)
            except Exception:
                pass

    def _on_stream(self, gid: str, obj: Dict[str, Any]) -> None:
        if self.on_stream:
            try:
                self.on_stream(gid, obj)
            except Exception:
                pass

    def _on_cancel(self, gid: str, obj: Dict[str, Any]) -> None:
        if self.on_cancel:
            try:
                self.on_cancel(gid, obj)
            except Exception:
                pass

    def _on_file(self, gid: str, obj: Dict[str, Any]) -> None:
        """附件分片到达：攒齐了才落盘（半截文件会被当成完整文件，很坑）。"""
        fid = str(obj.get("fid") or "")
        if not fid:
            return
        total = int(obj.get("total") or 0)
        idx = int(obj.get("idx") or 0)
        if total <= 0:
            return
        with self._flock:
            st = self._files.get(fid)
            if st is None:
                st = {"parts": {}, "total": total, "meta": obj.get("meta") or {},
                      "ts": time.time(), "gid": gid}
                self._files[fid] = st
            try:
                st["parts"][idx] = base64.b64decode(obj.get("data") or "")
            except Exception:
                return
            ready = len(st["parts"]) >= st["total"]
            if ready:
                self._files.pop(fid, None)
        if ready:
            meta = st["meta"] or {}
            data = b"".join(st["parts"][i] for i in range(st["total"]))
            try:
                rec = H.store_incoming(gid, meta, data)
            except Exception:
                return
            if self.on_file:
                try:
                    self.on_file(gid, rec)
                except Exception:
                    pass

    def _on_peer(self) -> None:
        m = MESH.node()
        st = m.status()
        self._state = "online" if (m.started and st["peers"] > 0) else (
            "online" if m.started else "offline")
        self._emit_status()

    def _emit_status(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.status())
            except Exception:
                pass

    # ------------------------------------------------------------ 生命周期

    def start(self, url: str, gid: str) -> Dict[str, Any]:
        self._hook()
        g = H.load_group(gid)
        if not g:
            self._error = "群不存在"
            self._state = "error"
            return self.status()

        m = MESH.node()
        if not m.started:
            m.start(0, [gid])
        m.add_group(gid)
        self._gid = gid
        self._url = (url or "").strip()
        self._error = ""
        self._started_at = self._started_at or time.time()

        u = self._url
        if u.startswith(("ws://", "wss://")):
            # 老群信息里存着中继地址。中继已经删了，这里**自动忽略并走局域网**，
            # 不报错 —— 否则用户升级后打开旧群会看到「连不上」，以为功能坏了。
            # 用户什么都不用做，同一 WiFi 下会自动发现彼此。
            self._url = ""
            self._state = "online"
            self._last_ok = time.time()
            return self.status()

        if u.startswith(("hive3://", "hive2://", "hive://")):
            p = m.parse_code(u)
            if not p.get("ok"):
                self._state = "error"
                self._error = p.get("error", "连接码格式不对")
                return self.status()
            t0 = time.time()
            r = m.connect(p["host"], p["port"], gid)
            if not r.get("ok"):
                self._state = "error"
                self._error = r.get("error", "连不上")
                return self.status()
            self._latency = round((time.time() - t0) * 1000, 1)
            self._state = "online"
            self._last_ok = time.time()
            return self.status()

        # 局域网模式：什么都不用填，靠 UDP 广播自动发现
        self._state = "online"
        self._last_ok = time.time()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        m = MESH.node()
        m.stop()
        self._state = "offline"
        self._gid = ""
        self._url = ""
        return self.status()

    # ------------------------------------------------------------ 状态

    def status(self) -> Dict[str, Any]:
        m = MESH.node()
        st = m.status()
        peers = int(st.get("peers") or 0)
        state = self._state
        if state == "error":
            pass
        elif not m.started:
            state = "offline"
        elif peers > 0:
            state = "online"
        else:
            state = "online" if self._gid else "offline"

        # compat：用对端上报的协议版本判断，不同版本只提示不拦
        vers = [int(x.get("ver") or 0) for x in (st.get("members") or [])]
        compat = "same"
        if vers:
            if any(v < PROTO_VER for v in vers):
                compat = "older"
            elif any(v > PROTO_VER for v in vers):
                compat = "newer"

        return {
            "ok": state != "error",
            "state": state,
            "connected": peers > 0,
            "url": self._url or ("局域网" if m.started else ""),
            "gid": self._gid,
            "latency_ms": self._latency,
            "proto": PROTO_VER,
            "relay_ver": "",           # 中继已移除，字段保留避免前端报错
            "compat": compat,
            "peers": peers,
            "members": st.get("members") or [],
            "port": st.get("port") or 0,
            "lan_ip": st.get("lan_ip") or "",
            "queued": st.get("queued") or 0,
            "error": self._error,
            "mode": "mesh",
            "uptime": st.get("uptime") or 0,
        }

    # ------------------------------------------------------------ 发送

    def send(self, gid: str, text: str, mtype: str = "text",
             extra: Optional[Dict] = None,
             ref: Optional[str] = None) -> Dict[str, Any]:
        """发一条消息：先本地落盘（拿本地 seq），再泛洪给所有在线成员。"""
        self._hook()
        try:
            msg = H.send_message(gid, text, mtype=mtype, ref=ref, extra=extra)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        m = MESH.node()
        if not m.started:
            m.start(0, [gid])
        m.add_group(gid)
        n = m.broadcast(gid, MESH.T_MSG, {"msg": msg})
        self._last_ok = time.time()
        if n == 0:
            self._state = "offline"
        else:
            self._state = "online"
        return {"ok": True, "msg": msg, "peers": n, "queued": 0 if n else 1}

    def send_stream(self, gid: str, mid: str, phase: str, delta: str = "",
                    task: Optional[Dict[str, Any]] = None, seq_in: int = 0) -> Dict[str, Any]:
        """@hd 实时进度。不落盘，直接泛洪 —— 刷屏内容没必要写进日志。"""
        self._hook()
        m = MESH.node()
        if not m.started:
            return {"ok": False, "error": "网络未启动"}
        me = H.profile()
        obj = {
            "mid": mid, "phase": phase, "delta": delta or "",
            "task": task or {}, "seq": seq_in,
            "from": me.get("user_id", ""), "name": me.get("name", ""),
            "ts": time.time(), "ttl": MESH.TTL_DEFAULT,
        }
        n = m.broadcast(gid, MESH.T_STREAM, obj)
        return {"ok": True, "peers": n}

    def send_cancel(self, gid: str, mid: str, by: str = "") -> bool:
        self._hook()
        m = MESH.node()
        if not m.started:
            return False
        n = m.broadcast(gid, MESH.T_CANCEL, {"mid": mid, "by": by, "ttl": MESH.TTL_DEFAULT})
        return n > 0

    def send_file(self, gid: str, meta: Dict[str, Any], data: bytes,
                  mime: str = "", name: str = "") -> Dict[str, Any]:
        """附件分片泛洪。单文件上限由 main.py 卡，这里只负责拆包和广播。

        为什么不用大帧一次发完：mesh 的帧上限 8MB，而且一帧太大时一个丢包
        就得整帧重来。512KB 一片，丢一片补一片。
        """
        self._hook()
        m = MESH.node()
        if not m.started:
            return {"ok": False, "error": "网络未启动"}
        fid = str(meta.get("id") or "")
        if not fid:
            return {"ok": False, "error": "缺少文件 id"}
        CH = 512 * 1024
        total = max(1, (len(data) + CH - 1) // CH)
        sent = 0
        for i in range(total):
            chunk = data[i * CH:(i + 1) * CH]
            obj = {
                "fid": fid, "idx": i, "total": total,
                "meta": meta, "data": base64.b64encode(chunk).decode("ascii"),
                "ttl": MESH.TTL_DEFAULT,
            }
            if m.broadcast(gid, MESH.T_FILE, obj) > 0:
                sent += 1
            time.sleep(0.02)   # 片之间留间隔，否则中间几片会被网络缓冲吞掉
        return {"ok": True, "fid": fid, "total": total, "sent": sent}

    # ------------------------------------------------------------ 附加能力

    def connect_code(self, gid: str) -> Dict[str, Any]:
        self._hook()
        return MESH.node().connect_code(gid or self._gid)

    def connect_to(self, gid: str, code: str) -> Dict[str, Any]:
        self._hook()
        m = MESH.node()
        p = m.parse_code(code)
        if not p.get("ok"):
            return p
        return m.connect(p["host"], p["port"], gid)

    def lan_beacons(self, gid: str) -> List[Dict[str, Any]]:
        """局域网里发现的同群成员（不含自己）。"""
        m = MESH.node()
        out = []
        for p in m.peers_of(gid):
            out.append({
                "user_id": p.uid, "name": p.name or p.uid,
                "host": p.addr[0], "port": p.port,
                "ver": p.ver, "online": p.alive,
                "last_seen": p.last_seen,
            })
        return out


_CLIENT: Optional[HiveClient] = None


def client() -> HiveClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = HiveClient()
    return _CLIENT
