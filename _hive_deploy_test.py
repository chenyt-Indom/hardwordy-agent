"""验证 hive_deploy：URL 解析 / 错误分类 / 真实探测 / 部署包生成。"""
import asyncio
import os
import sys
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets
import backend.hive_relay as R
import backend.hive_deploy as D

PASS, FAIL = [], []


def ck(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"   {info}" if info else ""))


print("=" * 60)
print("1. URL 规范化与解析")
cases = [
    ("1.2.3.4:8765", "ws://1.2.3.4:8765"),
    ("http://a.com", "ws://a.com"),
    ("https://a.com", "wss://a.com"),
    ("ws://x:1/", "ws://x:1"),
    ("a.com/hive", "wss://a.com/hive"),
    ("", ""),
]
for src, want in cases:
    got = D.normalize_url(src)
    ck(f"normalize({src!r}) -> {got}", got == want, f"期望 {want}")

try:
    s, h, p, path = D.parse_ws_url("wss://example.com/hive")
    ck("wss 默认端口 443", (s, h, p, path) == ("wss", "example.com", 443, "/hive"),
       f"{s} {h} {p} {path}")
    s, h, p, path = D.parse_ws_url("ws://1.2.3.4:8765")
    ck("ws 默认端口 8765", p == 8765, str(p))
    s, h, p, path = D.parse_ws_url("wss://a.com:8443/hive")
    ck("自定义端口 8443", p == 8443, str(p))
except Exception as e:
    ck("parse_ws_url", False, str(e))

print()
print("2. 错误分类（每种底层异常 -> 人话）")
err_cases = [
    ("[Errno 111] Connection refused", "refused"),
    ("getaddrinfo failed", "dns"),
    ("The operation timed out", "timeout"),
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "tls"),
    ("InvalidStatus: HTTP 403", "auth"),
    ("WebSocket handshake error, invalid status line", "not_ws"),
    ("something weird", "unknown"),
]
for msg, want in err_cases:
    c = D.classify(msg, "ws://a.com:1")
    ck(f"classify({msg[:32]!r}) -> {c['code']}", c["code"] == want, f"期望 {want}")
    if c["code"] == want:
        ck(f"  {want} 带建议", len(c.get("advice") or []) > 0,
           f"{len(c.get('advice') or [])} 条")

print()
print("3. 启动真实中继并探测")

PORT = 8799
_loop = asyncio.new_event_loop()


def _run_relay():
    asyncio.set_event_loop(_loop)

    async def main():
        async with websockets.serve(R.handler, "127.0.0.1", PORT,
                                    ping_interval=20, ping_timeout=20,
                                    process_request=R._process_request):
            await asyncio.Future()

    _loop.run_until_complete(main())


th = threading.Thread(target=_run_relay, daemon=True)
th.start()
time.sleep(1.2)

# 先单独验 /health（不走 WS）
import urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
        body = r.read().decode()
    import json as _j
    hj = _j.loads(body)
    ck("/health 返回合法 JSON", hj.get("ok") is True and hj.get("svc") == "hive-relay",
       body[:80])
    ck("/health 带版本和房间数", "ver" in hj and "rooms" in hj, str(hj))
except Exception as e:
    ck("/health 可访问", False, f"{type(e).__name__}: {e}")

res = D.probe(f"ws://127.0.0.1:{PORT}", timeout=5)
ck("probe 成功", res["ok"] is True, str(res.get("error")))
ck("probe 拿到 health", bool(res.get("health")), str(res.get("health"))[:80])
ck("probe 延迟合理(<3000ms)", res["latency_ms"] < 3000, f"{res['latency_ms']}ms")
print("  探测步骤：")
for s in res["steps"]:
    print(f"    [{s['level']:4s}] {s['t']:>5}ms  {s['text']}")

print()
print("4. 探测失败路径")
res_bad = D.probe("ws://127.0.0.1:8798", timeout=2)
ck("错误端口 -> 失败", res_bad["ok"] is False)
ck("错误端口 -> 分类为 refused", (res_bad.get("error") or {}).get("code") == "refused",
   str(res_bad.get("error", {}).get("code")))
ck("失败时也带步骤", len(res_bad["steps"]) >= 2, f"{len(res_bad['steps'])} 步")

res_dns = D.probe("ws://no-such-host-abc123xyz.invalid:8765", timeout=2)
ck("假域名 -> dns 或 timeout",
   (res_dns.get("error") or {}).get("code") in ("dns", "timeout", "unknown"),
   str((res_dns.get("error") or {}).get("code")))

res_empty = D.probe("", timeout=2)
ck("空地址 -> skipped 而非失败", res_empty.get("skipped") is True and res_empty["ok"])

print()
print("5. 部署包生成")
try:
    b = D.make_bundle({"os": "Ubuntu 22.04", "domain": "hive.example.com"})
    ck("生成 zip", os.path.exists(b["path"]), b["path"])
    ck("体积合理(>10KB)", b["size"] > 10_000, f"{b['size']} 字节")
    need = ["hive_relay.py", "deploy.sh", "requirements.txt",
            "systemd/hive-relay.service", "Dockerfile", "docker-compose.yml",
            "nginx/hive.conf", "cross-wifi/tailscale.md",
            "cross-wifi/cloudflare.md", "README.md"]
    with zipfile.ZipFile(b["path"]) as z:
        names = z.namelist()
        for n in need:
            ck(f"包含 {n}", n in names)
        relay = z.read("hive_relay.py").decode()
        ck("包内中继含 /health", "_process_request" in relay)
        sh = z.read("deploy.sh").decode()
        ck("deploy.sh 有 shebang", sh.startswith("#!/usr/bin/env bash"))
        ck("deploy.sh 装 systemd", "systemctl" in sh)
        rd = z.read("README.md").decode()
        ck("README 写了群主填的信息", "Ubuntu 22.04" in rd)
        ck("README 有跨 WiFi 表格", "Tailscale" in rd and "Cloudflare" in rd)
except Exception as e:
    ck("make_bundle", False, f"{type(e).__name__}: {e}")

print()
print("6. 上次地址记忆")
D.remember_relay("ws://1.2.3.4:8765", "测试服务器")
lr = D.last_relay()
ck("记住上次地址", lr.get("url") == "ws://1.2.3.4:8765", str(lr.get("url")))
ck("带备注", lr.get("note") == "测试服务器")
D.forget_relay()
ck("忘掉后为空", D.last_relay() == {}, str(D.last_relay()))

print()
print("=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
