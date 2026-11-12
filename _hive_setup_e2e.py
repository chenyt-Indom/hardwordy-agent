"""端到端：起真实中继 -> 探测 -> 生成部署包 -> 带地址建群 -> 清理。"""
import asyncio
import os
import shutil
import sys
import threading
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import websockets
import backend.hive_relay as R
from backend import main as M
from backend import hive, hive_client, hive_deploy

PASS, FAIL = [], []


def ck(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"   {info}" if info else ""))


PORT = 8801
_loop = asyncio.new_event_loop()


def _run_relay():
    asyncio.set_event_loop(_loop)

    async def go():
        async with websockets.serve(R.handler, "127.0.0.1", PORT,
                                    ping_interval=20, ping_timeout=20,
                                    process_request=R._process_request):
            await asyncio.Future()
    _loop.run_until_complete(go())


threading.Thread(target=_run_relay, daemon=True).start()
time.sleep(1.2)
print("本地中继已起在 127.0.0.1:%d" % PORT)

print()
print("1. /api/hive/relay/probe —— 真实探测")
r = M.hive_relay_probe({"url": f"ws://127.0.0.1:{PORT}"})
ck("probe 返回 ok", r.get("ok") is True, str(r.get("error")))
ck("拿到 health", bool(r.get("health")), str(r.get("health"))[:60])
ck("带步骤日志", len(r.get("steps") or []) >= 4, f"{len(r.get('steps') or [])} 步")
for s in (r.get("steps") or []):
    print(f"    [{s['level']:4s}] {s['t']:>4}ms {s['text']}")

print()
print("2. /api/hive/relay/probe —— 错误地址")
r2 = M.hive_relay_probe({"url": f"ws://127.0.0.1:8899"})
ck("错误端口 ok=False", r2.get("ok") is False)
ck("错误分类 refused", (r2.get("error") or {}).get("code") == "refused",
   str((r2.get("error") or {}).get("code")))
ck("错误带建议", len((r2.get("error") or {}).get("advice") or []) > 0)

print()
print("3. /api/hive/relay/bundle + 下载")
b = M.hive_relay_bundle({"os": "Ubuntu 22.04", "domain": "hive.test.com"})
ck("生成成功", b.get("ok") is True, str(b.get("error")))
ck("返回下载地址", b.get("download") == "/api/hive/relay/bundle/download")
p = b.get("path") or ""
ck("zip 落盘", os.path.exists(p), p)
if os.path.exists(p):
    with zipfile.ZipFile(p) as z:
        ck("zip 完整可读", len(z.namelist()) == 10, f"{len(z.namelist())} 个文件")
        ck("含跨 WiFi 方案", "cross-wifi/tailscale.md" in z.namelist())

print()
print("4. /api/hive/create 带中继地址")
d = M.hive_create({"name": "中继验证群", "owner_name": "测试群主",
                   "relay_url": f"ws://127.0.0.1:{PORT}"})
ck("建群成功", d.get("ok") is True, str(d.get("error")))
gid = (d.get("group") or {}).get("gid") or ""
ck("拿到群号", bool(gid), gid)
rel = d.get("relay") or {}
ck("relay.configured", rel.get("configured") is True, str(rel))
ck("relay.connected 已连上", rel.get("connected") is True, str(rel.get("error")))

print()
print("5. 群信息与中继状态")
g = hive.load_group(gid) or {}
ck("群名正确", g.get("name") == "中继验证群", str(g.get("name")))
ck("群主是本机", g.get("owner") == hive.profile()["user_id"])
cfg = hive.relay_config()
ck("中继配置已保存", cfg.get("url") == f"ws://127.0.0.1:{PORT}", str(cfg.get("url")))
last = M.hive_relay_last()
ck("上次地址已记住", (last.get("last") or {}).get("url") == f"ws://127.0.0.1:{PORT}",
   str((last.get("last") or {}).get("url")))

st = hive_client.client().status()
ck("客户端已连接", st.get("connected") is True, str(st.get("last_error")))

print()
print("6. 清理")
try:
    hive_client.client().stop()
except Exception:
    pass
try:
    shutil.rmtree(os.path.join(ROOT, "data", "hive", gid), ignore_errors=True)
    print("  已删除测试群目录", gid)
except Exception as e:
    print("  删群目录失败", e)
hive.set_relay("", "", False)
hive_deploy.forget_relay()
print("  已清空中继配置与上次记录")
ck("清理后配置为空", hive.relay_config().get("url") == "")

print()
print("=" * 56)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
