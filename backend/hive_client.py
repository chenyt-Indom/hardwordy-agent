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
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import backend.hive as H
import backend.hive_mesh as MESH
import backend.hive_relay_link as RELAY

PROTO_VER = MESH.PROTO_VER

# 中继配置：一个服务器给所有群共用。存在 data/hive/relay.json（权限 600）。
# ★ 密钥只在本机文件里，接口只回显掩码 —— 避免前端/日志把密钥漏出去。
RELAY_CFG = os.path.join(H._DATA, "relay.json")
RELAY_STATE = os.path.join(H._DATA, "relay_state.json")


def relay_config() -> Dict[str, Any]:
    try:
        with open(RELAY_CFG, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"url": "", "token": "", "enabled": False}
    return {
        "url": str(d.get("url") or "").strip(),
        "token": str(d.get("token") or "").strip(),
        "enabled": bool(d.get("enabled", True)),
    }


def mask_relay(d: Dict[str, Any]) -> Dict[str, Any]:
    """给前端看的版本：永远不含完整密钥。"""
    tok = str(d.get("token") or "")
    return {
        "url": d.get("url") or "",
        "enabled": bool(d.get("enabled", True)),
        "token_set": bool(tok),
        "token_hint": (tok[:3] + "***") if tok else "",
    }


def set_relay_config(url: str = "", token: str = "", enabled: bool = True,
                     keep_token: bool = True) -> Dict[str, Any]:
    """写中继配置。token 留空且 keep_token=True 时保留原密钥。"""
    old = relay_config()
    tok = (token or "").strip() or (old.get("token") or "" if keep_token else "")
    d = {"url": (url or "").strip(), "token": tok, "enabled": bool(enabled)}
    os.makedirs(H._DATA, exist_ok=True)
    tmp = RELAY_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RELAY_CFG)
    try:
        os.chmod(RELAY_CFG, 0o600)      # 只有本机当前用户能读
    except Exception:
        pass
    return mask_relay(d)


class HiveClient:
    def __init__(self) -> None:
        self.on_message = None      # Callable[[str, dict], None]
        self.on_stream = None
        self.on_cancel = None
        self.on_file = None
        self.on_status = None
        # 下面三个原来只在 _on_xxx 里用 getattr 兜着取，从没在 __init__ 声明。
        # 上层（main.py）直接赋值也能用，但声明出来更清楚，也避免拼错名字静默失效。
        self.on_peer = None         # Callable[[], None]        有人连上/断开
        self.on_members = None      # Callable[[str], None]     成员表变了
        self.on_gone = None         # Callable[[str, str], None] 群被群主删了

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
        self._relay = None          # RelayLink（走公网中继时才有）

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
        m.on_gone = self._on_gone
        self._hooked = True

    def _on_gone(self, gid: str, name: str) -> None:
        """群主把群删了：通知界面把这个群从列表里去掉。"""
        cb = getattr(self, "on_gone", None)
        if cb:
            try:
                cb(gid, name)
            except Exception:
                pass
        self._emit_status()

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
        # ⚠️ 2026-11-13 修：原来只更新内部状态，不通知上层 ——
        #    于是「有人上线 → 补发积压附件 / 前端刷新在线人数」这条链断了。
        #    （main.py 之前挂到 MESH.node().on_peer 上，会被 _hook() 覆盖掉。）
        if self.on_peer:
            try:
                self.on_peer()
            except Exception:
                pass

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
        m.add_group(gid, vt=g.get("vt") or "",
                    gname=g.get("name") or "")
        self._gid = gid
        self._url = (url or "").strip()
        self._error = ""
        self._started_at = self._started_at or time.time()

        u = self._url
        cfg = relay_config()
        # 没填地址时，用全局中继配置（「以后建群连接直接用密钥」就是靠这里）
        if not u and cfg.get("enabled") and cfg.get("url"):
            u = cfg["url"]
            self._url = u

        # 中继：hive4:// / ws:// / wss:// / tcp:// / 裸 host:port
        # （本项目用的是自定义二进制协议，写 ws:// 只是习惯，不是真的 WebSocket）
        if u and not u.startswith(("hive3://", "hive2://", "hive://")):
            try:
                lk = RELAY.link(RELAY_STATE)
                st = lk.start(u, gid, cfg.get("token") or "")
                self._relay = lk
                if st.get("online"):
                    self._state = "online"
                    self._error = ""
                    self._last_ok = time.time()
                    self._latency = float(st.get("rtt_ms") or 0)
                else:
                    self._state = "error"
                    self._error = st.get("error") or "中继连不上"
                return self.status()
            except Exception as e:
                self._state = "error"
                self._error = "%s: %s" % (type(e).__name__, e)
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
        if self._relay is not None:
            try:
                self._relay.stop()
            except Exception:
                pass
            self._relay = None
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
            "dev": st.get("dev") or "",
            "uid_conflict": st.get("uid_conflict"),
        }

    def refresh_identity(self) -> Dict[str, Any]:
        """换了 user_id 之后立刻生效（否则还会按老身份发消息）。"""
        try:
            MESH.node().refresh_identity()
        except Exception:
            pass
        self._emit_status()
        return self.status()

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

    def broadcast_members(self, gid: str) -> int:
        """把本地成员表广播出去。加群/踢人/改角色/退群后调用。"""
        self._hook()
        try:
            return MESH.node().broadcast_members(gid)
        except Exception:
            return 0

    def broadcast_gone(self, gid: str, name: str = "") -> int:
        """群主删群后通知成员：本地那份也下线（不然列表里挂着一个打不开的群）。"""
        self._hook()
        try:
            m = MESH.node()
            if not m.started:
                return 0
            return m.broadcast(gid, MESH.T_GONE, {"name": name, "ttl": MESH.TTL_DEFAULT})
        except Exception:
            return 0

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
        # ⚠️ 2026-11-13 修：附件 meta 的字段名是 `fid`，这里原来读的是 `id`，
        #    于是每次调用都返回「缺少文件 id」—— 附件从来没成功广播过，
        #    群里只有发送者本机能打开文件。实测确认（在线状态也发不出去）。
        fid = str(meta.get("fid") or meta.get("id") or "")
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

    # ---------------------------------------------------------- 密码进群

    def join_by_password(self, pw: str, name: str = "", host: str = "",
                         port: int = 0) -> Dict[str, Any]:
        """用 12 位密码进群（二维码已经删掉了）。

        找群的三个来源：本机已有记录、局域网 UDP 广播、你手填的地址。
        每个候选都拿「密码派生的密钥」去核对验证标签 —— 对上了才算密码正确，
        所以「密码错」和「没找到群」能分得清清楚楚。
        """
        m = MESH.node()
        if not m.started:
            m.start(0, [])
        try:
            pw_n = H.norm_password(pw)
        except ValueError as e:
            return {"ok": False, "stage": "format", "error": str(e)}

        cands: List[Dict[str, Any]] = []
        used = set()

        def _add(gid, vt, nm, src, h="", p=0):
            if not gid or gid in used:
                return
            key = H.derive_key(pw_n, gid)
            tag = H.verify_tag(key, gid)
            if vt and tag != vt:
                return                      # 标签对不上 = 这个群不是这把密码
            used.add(gid)
            cands.append({"gid": gid, "vt": tag, "name": nm or "",
                          "src": src, "host": h, "port": p, "key": key})

        # (a) 本机已有记录（含之前退出的群）
        for gid in H.all_group_ids():
            g = H.load_group(gid) or {}
            key = H.derive_key(pw_n, gid)
            if g.get("kfp") == H.key_fingerprint(key):
                _add(gid, H.verify_tag(key, gid), g.get("name") or "", "local")

        # (b) 局域网广播
        for b in m.seen_beacons():
            _add(b.get("gid") or "", b.get("vt") or "", b.get("gn") or "",
                 "lan", b.get("host") or "", int(b.get("port") or 0))

        # (c) 手填的地址
        net_err = ""
        probe_ok = False
        if host:
            pr = m.probe(host, int(port) if port else MESH.MESH_PORT)
            if pr.get("ok"):
                probe_ok = True
                for g in (pr.get("groups") or []):
                    _add(g.get("gid") or "", g.get("vt") or "",
                         g.get("gn") or "", "addr", host,
                         int(pr.get("port") or port or 0))
            else:
                net_err = pr.get("error") or "连不上"

        if not cands:
            if net_err:
                return {"ok": False, "stage": "network",
                        "error": net_err + "。请确认对方地址和端口，"
                                           "或者确认双方在同一 WiFi。"}
            if probe_ok:
                return {"ok": False, "stage": "password",
                        "error": "密码不对：对方在线，但他的群没有一个和这个密码匹配。"}
            return {"ok": False, "stage": "notfound",
                    "error": "没找到匹配的群。先确认密码有没有输错；"
                             "如果对方不在同一 WiFi，请再填上他的地址。"}

        hit = cands[0]
        try:
            g = H.join_with_key(hit["gid"], hit["key"], name,
                                meta={"n": hit["name"], "h": hit["host"]},
                                allow_rekey=(hit["src"] != "local"))
        except ValueError as e:
            return {"ok": False, "stage": "join", "error": str(e)}

        gg = H.load_group(hit["gid"]) or {}
        m.add_group(hit["gid"], vt=gg.get("vt") or hit["vt"],
                    gname=gg.get("name") or "")
        self._gid = hit["gid"]
        self._url = ""
        self._state = "online"
        self._error = ""
        self._last_ok = time.time()
        self._started_at = self._started_at or time.time()
        if hit["src"] == "addr" and hit["host"]:
            m.connect(hit["host"], int(hit["port"] or MESH.MESH_PORT), hit["gid"])
        return {"ok": True, "group": g, "via": hit["src"],
                "matched": len(cands), "password": H.format_password(pw_n)}

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

    def seen_beacons(self) -> List[Dict[str, Any]]:
        """局域网里听到的所有群广播（含自己不在的群）—— 密码入群靠它找群。"""
        m = MESH.node()
        return m.seen_beacons()

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
