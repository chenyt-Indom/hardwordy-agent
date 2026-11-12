# 临时探测脚本：WSS 握手测试（用完即删）
import asyncio
import json
import ssl
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import websockets

HOST = "139.199.69.88"
CANDS = [
    "wss://139.199.69.88/hive",
    "wss://139.199.69.88/",
    "wss://139.199.69.88/ws",
    "ws://139.199.69.88/hive",
    "ws://139.199.69.88:8765",
]


async def try_conn(url, verify=True):
    ctx = None
    if url.startswith("wss://"):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(url, open_timeout=6, close_timeout=3, ssl=ctx) as ws:
            # 发一个 ping 帧，看服务端怎么回
            await ws.send(json.dumps({"t": "ping", "v": 2}))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                return True, f"handshake OK, reply={str(msg)[:200]}"
            except asyncio.TimeoutError:
                return True, "handshake OK (no reply to ping)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:220]}"


async def main():
    for url in CANDS:
        print(f"\n=== {url} (verify cert) ===")
        ok, msg = await try_conn(url, verify=True)
        print(("  OK   " if ok else "  FAIL ") + msg)
        if url.startswith("wss://") and not ok:
            print(f"  --- retry with cert verification OFF ---")
            ok2, msg2 = await try_conn(url, verify=False)
            print(("  OK   " if ok2 else "  FAIL ") + msg2)


asyncio.run(main())
