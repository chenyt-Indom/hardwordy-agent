# -*- coding: utf-8 -*-
"""中继 + 客户端端到端测试。

验证：连上中继 / 收到别人的消息 / 自己发的对方能收到 /
     中继只存密文 / seq 由中继定序 / 断线重连 / 离线补发。
"""
import asyncio
import json
import os
import shutil
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import websockets

import backend.hive as H
import backend.hive_client as HC
from backend import hive_relay as R

FAIL = []


def chk(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + f"  {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


# ---------------------------------------------------------------- 环境
DATA = os.path.join(ROOT, "_hive_e2e_data")
RELAY = os.path.join(ROOT, "_hive_e2e_relay")
for d in (DATA, RELAY):
    shutil.rmtree(d, ignore_errors=True)
H._DATA = DATA
H._GROUPS = os.path.join(DATA, "groups")
H._PROFILE = os.path.join(DATA, "profile.json")
H._CFG = os.path.join(DATA, "relay.json")
R._DATA = RELAY
os.makedirs(RELAY, exist_ok=True)

PORT = 18765
URL = f"ws://127.0.0.1:{PORT}"
ready = threading.Event()


def run_relay():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def main():
        async with websockets.serve(R.handler, "127.0.0.1", PORT,
                                    max_size=8 * 1024 * 1024):
            ready.set()
            await asyncio.Future()

    loop.run_until_complete(main())


threading.Thread(target=run_relay, daemon=True).start()
chk("中继启动", ready.wait(6), URL)

# ---------------------------------------------------------------- 建群
g = H.create_group("E2E 协作群", owner_name="老王")
gid = g["gid"]
key = H.group_key(gid)
chk("建群", bool(gid), gid)
chk("拿到群密钥", key is not None and len(key) == 32)

# ---------------------------------------------------------------- 客户端连接
got = []


def on_msg(g, m):
    got.append(m)


cli = HC.client()
cli.on_message = on_msg
st = cli.start(URL, gid)
chk("客户端连上中继", st.get("connected") is True, json.dumps(
    {k: st.get(k) for k in ("connected", "gid", "url")}, ensure_ascii=False))


# ---------------------------------------------------------------- 模拟另一个人
def peer_send(text, uid="u_peer_li", name="小李"):
    async def _run():
        async with websockets.connect(URL, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"t": "join", "gid": gid, "uid": uid,
                                      "name": name, "since": 0}, ensure_ascii=False))
            await ws.recv()  # joined
            body = {"name": name, "type": "text", "text": text,
                    "from": uid, "ts": time.time()}
            mid = "m_peer_" + str(int(time.time() * 1000))
            await ws.send(json.dumps({"t": "msg", "gid": gid, "mid": mid,
                                      "blob": H._seal(key, body),
                                      "ts": time.time()}, ensure_ascii=False))
            await asyncio.sleep(0.4)
    asyncio.run(_run())


print("== 1. 收到别人发的消息 ==")
peer_send("我这边把接口写好了")
time.sleep(0.5)
chk("收到 1 条", len(got) == 1, f"{len(got)} 条")
if got:
    chk("内容正确", got[0]["text"] == "我这边把接口写好了", got[0]["text"])
    chk("发送人正确", got[0]["name"] == "小李", got[0]["name"])
    chk("中继分配了 seq", got[0]["seq"] > 0, f"seq={got[0]['seq']}")
chk("已落盘到本地日志", any(m["text"] == "我这边把接口写好了" for m in H.read_messages(gid)))

print("== 2. 自己发的对方能收到 ==")
received_by_peer = []


def peer_listen(seconds=2.0):
    async def _run():
        async with websockets.connect(URL, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"t": "join", "gid": gid, "uid": "u_peer_wang",
                                      "name": "小张", "since": 0}, ensure_ascii=False))
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(raw)
                if m.get("t") == "msg":
                    body = H._open(key, m.get("blob") or "")
                    if body:
                        received_by_peer.append(body)
    asyncio.run(_run())


th = threading.Thread(target=peer_listen, daemon=True)
th.start()
time.sleep(0.5)
r = cli.send(gid, "收到，我改前端")
chk("发送接口返回成功", r.get("ok") is True and not r.get("queued"), json.dumps(r, ensure_ascii=False))
th.join(timeout=3)
chk("对方收到了我的消息", any(b.get("text") == "收到，我改前端" for b in received_by_peer),
    f"{[b.get('text') for b in received_by_peer]}")

print("== 3. 中继只看得到密文 ==")
files = [f for f in os.listdir(RELAY) if f.endswith(".jsonl")]
chk("中继有落盘", len(files) == 1, str(files))
raw_all = ""
for f in files:
    raw_all += open(os.path.join(RELAY, f), encoding="utf-8").read()
chk("中继文件不含明文", "接口写好了" not in raw_all and "我改前端" not in raw_all)
chk("中继文件是密文", '"blob": "1:' in raw_all)
chk("中继知道 seq", '"seq": 1' in raw_all and '"seq": 2' in raw_all)

print("== 4. 断线重连 ==")
cli.stop()
time.sleep(0.3)
chk("停止后 disconnected", cli.status()["connected"] is False)
st2 = cli.start(URL, gid)
chk("重新连上", st2.get("connected") is True)
got.clear()
peer_send("重连之后这条能收到吗")
time.sleep(0.5)
chk("重连后能收消息", any("重连之后" in m.get("text", "") for m in got), f"{len(got)} 条")

print("== 5. 离线队列 ==")
cli.stop()
time.sleep(0.2)
r2 = cli.send(gid, "离线时发的消息")
chk("离线时进队列", r2.get("queued") is True, json.dumps(r2, ensure_ascii=False))
chk("队列里有 1 条", cli.status()["queued"] == 1)
got.clear()
cli.start(URL, gid)
time.sleep(1.0)
chk("重连后队列清空", cli.status()["queued"] == 0, f"{cli.status()['queued']}")
chk("离线消息补发后收到回执", any("离线时发的" in m.get("text", "") for m in got),
    f"{[m.get('text') for m in got]}")

cli.stop()
shutil.rmtree(DATA, ignore_errors=True)
shutil.rmtree(RELAY, ignore_errors=True)

print()
print("=" * 50)
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")
