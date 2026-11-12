"""Hive 中继部署助手 —— 探测 / 诊断 / 生成部署包 / LLM 定制步骤。

设计上刻意分成三件互不依赖的事：

1. **探测（probe）**：分五层逐级往下走 —— URL 格式 → DNS 解析 → TCP 连通 →
   HTTP /health → WebSocket 握手。哪一层断了就停在哪一层，并给出**这一层专属**
   的修复建议。这比"连不上"三个字有用得多：DNS 失败是域名写错，连接被拒是服务
   没起，超时是防火墙/安全组，TLS 报错是证书。

2. **部署包（make_bundle）**：生成一个 zip，里面是完整可跑的中继服务 + 三种部署
   方式（systemd / Docker / nginx 反代）+ 跨 WiFi 方案（Tailscale / Cloudflare
   Tunnel）。群主下载、上传、跑一条命令就行。

3. **LLM 定制（llm_guide）**：把探测结果和群主的服务器信息交给模型，让它按实际
   环境（Ubuntu/CentOS、有没有域名、有没有证书）写具体步骤，并解释失败原因。
   模型**不参与**探测和部署本身 —— 那是确定性的活，不该交给模型猜。

跨 WiFi 联机是这个模块里最容易被误解的部分，说清楚：
- **同一个 WiFi**：不需要中继，群主的局域网 IP 直连就行。
- **不同 WiFi（含 4G）**：两台机器在公网上互相看不见，必须有人帮忙"搭桥"。
  桥可以是 (a) 自己的云服务器跑本中继，(b) Tailscale 组虚拟局域网（免费、最省
  事、装一次就行），(c) Cloudflare Tunnel（群主机器暴露成临时公网地址）。
  (b)(c) 都不需要自己买服务器 —— 这就是"不依赖自己的服务器也能联机"的实现方式。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import socket
import ssl
import time
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "hive")
_CFG = os.path.join(_DATA, "relay.json")
_LAST = os.path.join(_DATA, "relay_last.json")

_RELAY_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hive_relay.py")


# ---------------------------------------------------------------- 小工具

def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def normalize_url(u: str) -> str:
    """把群主随手填的东西整理成 ws(s):// 地址。

    接受：1.2.3.4:8765 / http://x / https://x / ws://x / wss://x / 纯域名
    """
    s = (u or "").strip()
    if not s:
        return ""
    if s.startswith("wss://") or s.startswith("ws://"):
        return s.rstrip("/")
    if s.startswith("https://"):
        return "wss://" + s[8:].rstrip("/")
    if s.startswith("http://"):
        return "ws://" + s[7:].rstrip("/")
    # 没写协议：有域名/带斜杠的当 wss，纯 IP:端口 当 ws
    if "/" in s:
        return "wss://" + s.rstrip("/")
    return "ws://" + s.rstrip("/")


def parse_ws_url(u: str) -> Tuple[str, str, int, str]:
    """ws(s):// → (scheme, host, port, path)。"""
    m = re.match(r"^(wss?)://([^/:]+)(?::(\d+))?(/.*)?$", u or "")
    if not m:
        raise ValueError("地址格式不对，应该像 ws://1.2.3.4:8765 或 wss://域名/hive")
    scheme = m.group(1)
    host = m.group(2)
    default_port = 443 if scheme == "wss" else 8765
    port = int(m.group(3) or default_port)
    path = m.group(4) or ""
    return scheme, host, port, path


def _health_url(scheme: str, host: str, port: int, path: str) -> str:
    """拼 /health 的 HTTP 地址。wss 走 443 + 路径前缀，ws 走原端口。"""
    base = "https" if scheme == "wss" else "http"
    if scheme == "wss":
        p = path.rstrip("/") + "/health" if path else "/health"
        if port in (443,):
            return f"{base}://{host}{p}"
        return f"{base}://{host}:{port}{p}"
    return f"{base}://{host}:{port}/health"


# ---------------------------------------------------------------- 错误诊断

def classify(err: str, url: str = "") -> Dict[str, Any]:
    """把底层异常翻译成「人话 + 可执行的下一步」。"""
    e = (err or "").lower()
    host = ""
    try:
        host = parse_ws_url(url)[1]
    except Exception:
        pass

    if "格式不对" in (err or ""):
        return {
            "code": "bad_url", "title": "地址格式不对",
            "reason": "看起来不是 ws:// 或 wss:// 地址。",
            "advice": ["按 ws://服务器IP:8765 填写",
                       "挂了域名的填 wss://你的域名/hive"],
        }
    if ("name or service not known" in e or "nodename nor servname" in e
            or "getaddrinfo" in e or "no address associated" in e
            or "temporary failure in name resolution" in e):
        return {
            "code": "dns", "title": "域名解析失败",
            "reason": f"本机解析不出 {host or '这个域名'} 的 IP —— 域名可能拼错、"
                      "没解析、或者本机断网了。",
            "advice": [f"在浏览器打开 https://{host} 看能不能开（打不开就是域名问题）",
                       "确认域名已经解析到服务器 IP（ping 一下试试）",
                       "如果服务器只有 IP，直接填 ws://IP:8765 别用域名"],
        }
    if "refused" in e or "actively refused" in e:
        return {
            "code": "refused", "title": "端口开着但没人接（连接被拒）",
            "reason": "DNS 和网络是通的，但那个端口上没有服务在监听 —— "
                      "要么中继没启动，要么端口填错了。",
            "advice": ["上服务器执行 systemctl status hive-relay 看服务活着没",
                       "执行 ss -lntp | grep 8765 看端口有没有在听",
                       "如果用的是 80/443 以外的端口，确认填的端口号一致"],
        }
    if ("timed out" in e or "timeout" in e or "超时" in (err or "")) \
            and host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        # 本机回环不可能被防火墙挡：超时基本就是端口上没服务
        return {
            "code": "refused", "title": "本机这个端口没有服务在监听",
            "reason": f"连接 {host} 的端口超时。本机回环地址不会被防火墙挡，"
                      "基本可以确定是那个端口上没有服务在监听。",
            "advice": ["确认中继进程在跑：ps aux | grep hive_relay",
                       "确认端口号填的和启动参数一致",
                       "先 curl http://127.0.0.1:端口/health 试试"],
        }
    if "timed out" in e or "timeout" in e or "超时" in (err or ""):
        return {
            "code": "timeout", "title": "连接超时（被挡住了）",
            "reason": "数据包发出去没回音。最常见是防火墙或云服务器安全组没放行端口。",
            "advice": ["云控制台的安全组里放行这个端口（入方向 TCP）",
                       "服务器上执行：firewall-cmd --add-port=8765/tcp（CentOS）"
                       " 或 ufw allow 8765（Ubuntu）",
                       "同一 WiFi 内先试局域网 IP，排除公网问题"],
        }
    if "certificate" in e or "ssl" in e or "tls" in e or "wrong version number" in e:
        return {
            "code": "tls", "title": "证书 / TLS 握手失败",
            "reason": "wss 需要有效的 HTTPS 证书。自签名证书、证书过期、"
                      "或者端口上其实是明文 ws 都会报这个。",
            "advice": ["用 ws:// 直连端口先排除是不是证书问题",
                       "确认证书域名和访问的域名一致",
                       "用 Let's Encrypt 免费签一张：certbot --nginx -d 你的域名"],
        }
    if "403" in e or "401" in e or "unauthorized" in e or "forbidden" in e:
        return {
            "code": "auth", "title": "被拒绝（401/403）",
            "reason": "服务在跑，但握手被拒。可能是反向代理的鉴权或路径不匹配。",
            "advice": ["检查 nginx 里 location /hive 有没有额外 auth 配置",
                       "确认地址后面的路径和 nginx 的 location 一致"],
        }
    if ("websocket" in e and ("handshake" in e or "invalid" in e)) or "not a websocket" in e:
        return {
            "code": "not_ws", "title": "端口通了，但对面不是中继服务",
            "reason": "TCP 能连上，但握手不认 —— 可能是别的服务占了这个端口，"
                      "或者反向代理没转发 Upgrade 头。",
            "advice": ["nginx 必须带这两行：proxy_set_header Upgrade $http_upgrade; "
                       "proxy_set_header Connection $connection_upgrade;",
                       "确认这个端口没被别的程序占用"],
        }
    return {
        "code": "unknown", "title": "连不上",
        "reason": (err or "未知错误")[:200],
        "advice": ["先确认服务器上中继进程在跑（systemctl status hive-relay）",
                   "再确认端口放行、地址端口没写错"],
    }


# ---------------------------------------------------------------- 探测

def _tcp_check(host: str, port: int, timeout: float) -> Tuple[bool, float, str]:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.time() - t0) * 1000, ""
    except Exception as e:
        return False, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}"


def _http_health(url: str, timeout: float) -> Tuple[Optional[dict], str]:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Hive-Probe/1.1"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(4096)
        try:
            return json.loads(raw.decode("utf-8", "replace")), ""
        except Exception:
            return {"raw": raw[:200].decode("utf-8", "replace")}, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _ssl_ctx(url: str):
    """wss + 纯 IP 时跳过证书校验；ws 返回 None。

    和 hive_client._ssl_for 同一套判断。放两份是因为探测要能在
    「客户端还没连」的时候独立跑 —— 探测本身也得过证书这关，
    否则界面上会报一个其实和"通不通"无关的证书错误。
    """
    if not (url or "").startswith("wss://"):
        return None
    try:
        host = url.split("://", 1)[1].split("/", 1)[0]
        if host.startswith("["):
            host = host.split("]", 1)[0].strip("[")
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        is_ip = False
        if host.count(".") == 3:
            try:
                is_ip = all(0 <= int(x) <= 255 for x in host.split("."))
            except Exception:
                is_ip = False
        elif ":" in host:
            is_ip = True
        ctx = ssl.create_default_context()
        if is_ip:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except Exception:
        return None


def _ws_check(url: str, timeout: float) -> Tuple[bool, str]:
    """真握手一次，连上就关。不 join、不发消息，不污染房间状态。"""
    try:
        import websockets
    except Exception as e:
        return False, f"缺少 websockets 库: {e}"

    async def _do() -> Tuple[bool, str]:
        try:
            async with websockets.connect(url, open_timeout=timeout,
                                          ping_interval=None,
                                          max_size=4 * 1024 * 1024,
                                          ssl=_ssl_ctx(url)):
                return True, ""
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_do())
        finally:
            loop.close()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def candidates(u: str) -> List[str]:
    """从一个「随手填的地址」推出所有可能的可用地址，按可能性排序。

    **为什么需要它**（2026-11-13 实际踩到）：
    群主通常只知道一个 IP，并不知道自己的服务器上中继到底是哪种部署形态
    —— 独立进程监听 8765？挂在 hardwordy 应用里走 /hive？nginx 反代到根路径？
    让他猜是没道理的。更糟的是 `ws://1.2.3.4` 这种写法看着"填了"，
    实际按 ws 默认端口连 80，而 80 通常被 nginx 301 到 https，
    WebSocket 不跟随重定向，于是永远连不上。

    所以：写得完整的（带端口或带路径）就照用户的来；只给了主机的，
    把这三种常见形态都列出来，交给 probe_auto 逐个试。
    """
    s = (u or "").strip()
    if not s:
        return []

    host = ""
    port = ""
    path = ""

    m = re.match(r"^(wss?|https?)://([^/:]+)(?::(\d+))?(/.*)?$", s)
    if m:
        host = m.group(2)
        port = m.group(3) or ""
        path = (m.group(4) or "").strip("/")
    else:
        m2 = re.match(r"^([^/:]+)(?::(\d+))?(/.*)?$", s)
        if not m2:
            return [normalize_url(s)]
        host = m2.group(1)
        port = m2.group(2) or ""
        path = (m2.group(3) or "").strip("/")

    if path:
        return [normalize_url(s)]          # 写了路径，尊重用户

    if port:
        # 带了端口但没写路径：最常见的是「局域网直连群主电脑」，
        # 而本应用的内置中继在 /hive 上，所以先试带路径的。
        scheme = "wss" if (m and m.group(1) in ("wss", "https")) else "ws"
        return [
            f"{scheme}://{host}:{port}/hive",
            f"{scheme}://{host}:{port}",
        ]

    # 只给了主机：三种部署形态，按实测常见程度排
    return [
        f"wss://{host}/hive",     # 挂在应用里 / nginx 反代到 /hive（443，最稳）
        f"ws://{host}:8765",      # 独立中继进程默认端口
        f"wss://{host}",          # 直接反代到根路径
    ]


def probe_auto(raw: str, timeout: float = 4.0) -> Dict[str, Any]:
    """先算出候选地址，逐个探测，返回第一个通的。

    探测日志会**合并**：前端能看到「试了哪些地址、哪个成功」，
    而不是只看到最后一个失败原因 —— 否则用户会以为"试的最后一个就是答案"。
    """
    cands = candidates(raw)
    if not cands:
        return probe(raw, timeout)
    if len(cands) == 1:
        return probe(cands[0], timeout)

    head: List[Dict[str, Any]] = [{
        "level": "info",
        "text": f"「{raw}」没写端口/路径，自动尝试 {len(cands)} 种可能的地址",
        "t": 0,
    }]
    first_fail: Optional[Dict[str, Any]] = None

    for c in cands:
        head.append({"level": "info", "text": f"→ 试 {c}", "t": 0})
        r = probe(c, timeout=timeout)
        if r.get("ok"):
            r["steps"] = head + (r.get("steps") or [])
            r["auto"] = True
            r["tried"] = cands
            r["steps"].append({
                "level": "ok",
                "text": f"选定 {c}（其余 {len(cands) - 1} 个不用试了）",
                "t": 0,
            })
            return r
        if first_fail is None:
            first_fail = r

    out = first_fail or {"ok": False, "url": cands[0], "health": None,
                         "error": None, "latency_ms": 0}
    out["steps"] = head + (out.get("steps") or [])
    out["tried"] = cands
    out["auto"] = True
    out["steps"].append({
        "level": "err",
        "text": f"试过的 {len(cands)} 个地址都不通。第一个失败原因见上（其余类似）。",
        "t": 0,
    })
    return out


def probe(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    """五层探测。每一步都记进 steps，前端拿去做滚动日志。"""
    steps: List[Dict[str, Any]] = []
    t_all = time.time()

    def add(level: str, text: str) -> None:
        steps.append({"level": level, "text": text,
                      "t": round((time.time() - t_all) * 1000)})

    raw = (url or "").strip()
    if not raw:
        add("warn", "没有填中继地址 —— 将使用本机模式（只有本机能看消息）")
        return {"ok": True, "skipped": True, "url": "", "steps": steps,
                "latency_ms": 0, "health": None, "error": None}

    norm = normalize_url(raw)
    add("info", f"地址整理为 {norm}")
    try:
        scheme, host, port, path = parse_ws_url(norm)
    except Exception as e:
        err = classify(str(e), norm)
        add("err", f"{err['title']}：{err['reason']}")
        return {"ok": False, "url": norm, "steps": steps, "health": None,
                "error": err, "latency_ms": 0}

    # 1) DNS
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        add("ok", f"DNS 解析成功：{host} → {', '.join(ips[:3])}"
                  f"（{int((time.time() - t0) * 1000)}ms）")
    except Exception as e:
        err = classify(f"getaddrinfo {e}", norm)
        add("err", f"{err['title']}：{err['reason']}")
        return {"ok": False, "url": norm, "steps": steps, "health": None,
                "error": err, "latency_ms": int((time.time() - t0) * 1000)}

    # 2) TCP
    ok, ms, terr = _tcp_check(host, port, timeout)
    if not ok:
        err = classify(terr, norm)
        add("err", f"TCP 连接失败（{host}:{port}，{int(ms)}ms）：{err['title']}")
        return {"ok": False, "url": norm, "steps": steps, "health": None,
                "error": err, "latency_ms": int(ms)}
    add("ok", f"TCP 端口通：{host}:{port}（{int(ms)}ms）")

    # 3) HTTP /health
    health = None
    hurl = _health_url(scheme, host, port, path)
    h, herr = _http_health(hurl, timeout)
    if h and h.get("ok"):
        health = h
        add("ok", f"健康检查通过：{h.get('svc', '?')} v{h.get('ver', '?')}"
                  f"，在线房间 {h.get('rooms', 0)}，已运行 {h.get('uptime', 0)}s")
    elif h:
        add("warn", f"端口有 HTTP 响应但不是 Hive 中继（{str(h)[:80]}）")
    else:
        add("warn", f"健康检查没通过（{hurl}）：{herr[:100]} —— "
                    "可能是老版本中继或反代没转发 /health，继续测 WS 握手")

    # 4) WS 握手
    ws_ok, ws_err = _ws_check(norm, timeout)
    if ws_ok:
        add("ok", "WebSocket 握手成功 —— 中继可用，消息可以贯通")
        total = int((time.time() - t_all) * 1000)
        add("ok", f"全部通过，总耗时 {total}ms")
        return {"ok": True, "url": norm, "steps": steps, "health": health,
                "error": None, "latency_ms": total}

    err = classify(ws_err, norm)
    add("err", f"WebSocket 握手失败：{err['title']}")
    add("info", "建议：" + "；".join(err["advice"][:3]))
    return {"ok": False, "url": norm, "steps": steps, "health": health,
            "error": err, "latency_ms": int((time.time() - t_all) * 1000)}


# ---------------------------------------------------------------- 上次用的地址

def last_relay() -> Dict[str, Any]:
    return _read_json(_LAST, {}) or {}


def remember_relay(url: str, note: str = "") -> Dict[str, Any]:
    rec = {"url": normalize_url(url), "note": note or "",
           "ts": time.time()}
    _write_json(_LAST, rec)
    return rec


def forget_relay() -> Dict[str, Any]:
    try:
        os.remove(_LAST)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- 部署包

_DEPLOY_SH = r"""#!/usr/bin/env bash
# Hive 中继一键部署（Ubuntu / Debian / CentOS / Rocky 都适用）
# 用法：bash deploy.sh [端口]      默认 8765
set -euo pipefail
PORT="${1:-8765}"
DIR="/opt/hive-relay"
DATA="/var/lib/hive"

echo "==> 1/5 准备目录"
mkdir -p "$DIR" "$DATA"
cp -f "$(dirname "$0")/hive_relay.py" "$DIR/"

echo "==> 2/5 安装 Python 依赖"
cd "$DIR"
python3 -m venv .venv 2>/dev/null || true
if [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q websockets
  PY="$DIR/.venv/bin/python"
else
  python3 -m pip install -q websockets
  PY="$(command -v python3)"
fi
echo "    使用解释器：$PY"

echo "==> 3/5 安装 systemd 服务"
sed -e "s#__PORT__#$PORT#g" -e "s#__DIR__#$DIR#g" -e "s#__DATA__#$DATA#g" \
    -e "s#__PY__#$PY#g" "$(dirname "$0")/systemd/hive-relay.service" \
    > /etc/systemd/system/hive-relay.service
systemctl daemon-reload
systemctl enable hive-relay >/dev/null 2>&1 || true
systemctl restart hive-relay

echo "==> 4/5 放行防火墙端口 $PORT"
if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
else
  echo "    没检测到 ufw/firewall-cmd，记得在云控制台安全组放行 $PORT/tcp"
fi

echo "==> 5/5 自检"
sleep 2
if systemctl is-active --quiet hive-relay; then
  echo "    ✅ 服务已启动"
else
  echo "    ❌ 服务没起来，看日志：journalctl -u hive-relay -n 50 --no-pager"
  exit 1
fi
if command -v curl >/dev/null 2>&1; then
  echo -n "    健康检查："
  curl -s "http://127.0.0.1:$PORT/health" || echo "（curl 失败，但服务可能在跑）"
  echo
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "===================================================="
echo " 部署完成"
echo " 客户端填： ws://${IP:-你的公网IP}:$PORT"
echo " 同一 WiFi 用上面这个内网地址即可"
echo " 不同 WiFi 需要公网 IP，或看 cross-wifi/ 里的两个方案"
echo "===================================================="
"""

_SYSTEMD = """[Unit]
Description=Hive Relay (multi-user chat relay)
After=network.target

[Service]
Type=simple
WorkingDirectory=__DIR__
ExecStart=__PY__ __DIR__/hive_relay.py --host 0.0.0.0 --port __PORT__ --data __DATA__
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir websockets
COPY hive_relay.py /app/
EXPOSE 8765
VOLUME ["/data"]
CMD ["python", "hive_relay.py", "--host", "0.0.0.0", "--port", "8765", "--data", "/data"]
"""

_COMPOSE = """services:
  hive-relay:
    build: .
    container_name: hive-relay
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - ./relay_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
"""

_NGINX = r"""# Hive 中继的 nginx 反向代理配置（wss + HTTPS）
# 用法：把 YOUR_DOMAIN 换成你的域名，放到 /etc/nginx/conf.d/hive.conf
# 证书：certbot --nginx -d YOUR_DOMAIN   会自动填好 ssl_certificate 两行

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name YOUR_DOMAIN;
    # 有 certbot 时它会自动加 301 到 https

    location /health {
        proxy_pass http://127.0.0.1:8765/health;
        proxy_set_header Host $host;
    }

    location /hive {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;   # WS 长连接别被掐
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
"""

_TAILSCALE_MD = r"""# 跨 WiFi 方案 A：Tailscale（推荐，不用买服务器）

解决的问题：你和同事在不同 WiFi（甚至一个用 4G），两台机器在公网互相看不见。
Tailscale 给你们组一个虚拟局域网，装完之后**像在同一个 WiFi 里一样**。

## 群主这边（跑中继的机器）

```bash
# 1. 装 Tailscale（Linux）
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # 会打印一个链接，浏览器登录一下

# 2. 拿到虚拟 IP（形如 100.101.102.103）
tailscale ip -4
```

然后中继照常跑（deploy.sh 已经装好了），假设端口 8765，
客户端就填：`ws://100.101.102.103:8765`

## 每个成员这边

装 Tailscale 客户端（Windows/macOS/Linux/手机都有），用**同一个账号**登录，
就能互相访问。装完在中继地址里填群主的 `100.x.x.x` 地址。

## 优缺点

- ✅ 免费（个人版 100 台设备）、不用买服务器、不用配路由器、NAT 后面也能通
- ✅ 全程加密（WireGuard），比公网明文 ws 更安全
- ⚠️ 每个成员都要装一次客户端（一次性的）
- ⚠️ 国内偶尔需要换 DERP 中继节点，但通常直连可用

## 也可以不装客户端吗？

可以，但那就得走方案 B（Cloudflare Tunnel）或自建云服务器。
"""

_CLOUDFLARE_MD = r"""# 跨 WiFi 方案 B：Cloudflare Tunnel（群主一台机器就够）

把群主机器上的 8765 端口暴露成一个公网 `https://xxx.trycloudflare.com` 地址，
别人不用装任何东西，直接连。

## 群主这边

```bash
# 1. 装 cloudflared
#    Linux:
curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/
#    Windows: 从 Cloudflare 官网下 cloudflared-windows-amd64.exe

# 2. 起隧道（不需要 Cloudflare 账号，临时域名）
cloudflared tunnel --url http://localhost:8765
```

它会打印一行：

```
https://random-words-1234.trycloudflare.com
```

## 客户端填什么

把 `https://` 换成 `wss://`，**不要加端口**：

```
wss://random-words-1234.trycloudflare.com
```

## 优缺点

- ✅ 成员零安装，浏览器级别就能连
- ✅ 自带 HTTPS，数据是加密传输的（消息本身还有一层群密钥加密）
- ⚠️ 免费临时域名**每次重启都会变**，适合临时协作；固定域名要绑 Cloudflare 账号
- ⚠️ 流量绕 Cloudflare，速度取决于线路

## 要固定域名

```bash
cloudflared tunnel login
cloudflared tunnel create hive
cloudflared tunnel route dns hive hive.你的域名.com
cloudflared tunnel run --url http://localhost:8765 hive
```
之后地址固定为 `wss://hive.你的域名.com`。
"""

_README = r"""# Hive 中继部署包

这是多人在线协作群（Hive）的中继服务。它只做三件事：**转发消息、给消息定序、
存密文日志**。它看不懂任何聊天内容 —— 消息体是 AES-GCM 密文，密钥只在群成员之间
通过二维码传递，服务器上没有。

## 先选一条路

| 你的情况 | 用哪个 | 群主填的地址 |
|---|---|---|
| 大家连同一个 WiFi（同一间办公室） | 不用中继，直接局域网 | `ws://群主内网IP:8765` |
| 不同 WiFi，不想买服务器 | **Tailscale**（见 cross-wifi/tailscale.md） | `ws://100.x.x.x:8765` |
| 不同 WiFi，成员不想装东西 | **Cloudflare Tunnel**（cross-wifi/cloudflare.md） | `wss://xxx.trycloudflare.com` |
| 有云服务器（腾讯云/阿里云） | 本包的 deploy.sh | `ws://公网IP:8765` 或 `wss://域名/hive` |

## 云服务器部署（最省事的一条命令）

```bash
# 1. 把整个包传上去
scp hive-relay-deploy.zip root@你的服务器:/root/
ssh root@你的服务器

# 2. 解压并跑
cd /root && unzip -o hive-relay-deploy.zip -d hive && cd hive
bash deploy.sh 8765
```

跑完会打印 `ws://内网IP:8765` 和自检结果。**别忘了在云控制台安全组放行 8765/TCP**。

## Docker 方式（喜欢容器的话）

```bash
docker compose up -d
curl http://127.0.0.1:8765/health    # 应该返回 {"ok":true,...}
```

## 挂域名 + HTTPS（可选，但推荐）

看 `nginx/hive.conf`，把 `YOUR_DOMAIN` 换掉，然后：

```bash
sudo cp nginx/hive.conf /etc/nginx/conf.d/
sudo certbot --nginx -d 你的域名
sudo nginx -t && sudo systemctl reload nginx
```

客户端这时填 `wss://你的域名/hive`。

## 自检

```bash
curl http://127.0.0.1:8765/health
# {"ok":true,"svc":"hive-relay","ver":"1.1","rooms":0,"uptime":12.3,...}
```

应用里点「检测」也会走同样的检查，并且会告诉你卡在哪一层。

## 常见问题

**连不上，提示「连接被拒」** → 中继没启动。`systemctl status hive-relay`

**连不上，提示「超时」** → 端口没放行。云控制台安全组 + 服务器防火墙两处都要开。

**wss 报证书错** → 用 `ws://` 先排除；或者证书域名和访问域名不一致。

**健康检查 404 但 WS 能连** → 老版本中继（1.0）没有 /health，不影响使用。
"""


def make_bundle(host_hint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成部署包 zip，返回路径和内容清单。"""
    hint = host_hint or {}
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    files: List[str] = []

    def put(name: str, data: str) -> None:
        zf.writestr(name, data.encode("utf-8"))
        files.append(name)

    # 中继本体：直接复制当前正在用的那份，保证版本一致
    try:
        with open(_RELAY_SRC, "r", encoding="utf-8") as f:
            relay_code = f.read()
    except Exception as e:
        raise RuntimeError(f"读不到中继源码 {_RELAY_SRC}: {e}")
    put("hive_relay.py", relay_code)
    put("requirements.txt", "websockets>=13\n")
    put("deploy.sh", _DEPLOY_SH)
    put("systemd/hive-relay.service", _SYSTEMD)
    put("Dockerfile", _DOCKERFILE)
    put("docker-compose.yml", _COMPOSE)
    put("nginx/hive.conf", _NGINX)
    put("cross-wifi/tailscale.md", _TAILSCALE_MD)
    put("cross-wifi/cloudflare.md", _CLOUDFLARE_MD)

    extra = ""
    if hint.get("os") or hint.get("domain") or hint.get("note"):
        extra = ("\n## 群主填写的信息\n\n"
                 f"- 服务器系统：{hint.get('os') or '未填'}\n"
                 f"- 域名：{hint.get('domain') or '未填'}\n"
                 f"- 备注：{hint.get('note') or '未填'}\n")
    put("README.md", _README + extra)

    zf.close()
    os.makedirs(_DATA, exist_ok=True)
    out = os.path.join(_DATA, "hive-relay-deploy.zip")
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    return {"path": out, "files": files, "size": os.path.getsize(out),
            "name": "hive-relay-deploy.zip"}


# ---------------------------------------------------------------- LLM 定制

_GUIDE_SYS = """你是服务器运维助手，帮用户把 Hive 中继服务部署到自己的机器上。

要求：
1. 只给**可直接复制执行**的命令，每条命令前一行写清它是干什么的。
2. 严格按用户给的环境来（系统是 CentOS 就写 firewall-cmd，Ubuntu 就写 ufw）。
3. 不确定的地方**要说明不确定**，不要编造。
4. 涉及域名/证书时，给出 certbot 的完整命令和证书路径会放在哪。
5. 如果用户是"不同 WiFi 联机"且没有云服务器，优先推荐 Tailscale 或
   Cloudflare Tunnel，不要让他去买服务器。
6. 输出用 Markdown，分步骤编号，不要客套话。
"""


def llm_guide(info: Dict[str, Any], ask) -> Dict[str, Any]:
    """让模型按群主的实际环境写部署步骤。

    ask: 回调 (system, user) -> str，由调用方注入，避免这个模块依赖 llm 层。
    """
    probe_res = info.get("probe") or {}
    err = probe_res.get("error") or {}
    parts = [
        f"服务器系统：{info.get('os') or '未说明'}",
        f"域名：{info.get('domain') or '没有'}",
        f"端口：{info.get('port') or 8765}",
        f"场景：{info.get('scene') or '云服务器'}",
    ]
    if err:
        parts.append(f"探测失败，错误类型：{err.get('code')} / {err.get('title')}")
        parts.append(f"底层报错：{err.get('reason')}")
    elif probe_res.get("ok") and probe_res.get("url"):
        parts.append(f"探测已通过：{probe_res.get('url')}，延迟 "
                     f"{probe_res.get('latency_ms')}ms")
    if info.get("note"):
        parts.append(f"补充说明：{info.get('note')}")

    user = ("请按下面的实际情况，给我部署 Hive 中继的完整步骤：\n\n"
            + "\n".join("- " + p for p in parts)
            + "\n\n部署包已经生成好了（含 hive_relay.py / deploy.sh / systemd / "
              "docker / nginx 配置 / cross-wifi 两个方案），"
              "你只需要告诉我按什么顺序执行、哪里要改成我自己的值。")
    try:
        text = ask(_GUIDE_SYS, user)
        return {"ok": True, "guide": text}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 自检

def status() -> Dict[str, Any]:
    return {
        "last": last_relay(),
        "relay_src": os.path.exists(_RELAY_SRC),
        "bundle": os.path.join(_DATA, "hive-relay-deploy.zip"),
        "bundle_exists": os.path.exists(
            os.path.join(_DATA, "hive-relay-deploy.zip")),
    }
