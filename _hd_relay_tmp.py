# -*- coding: utf-8 -*-
"""中继端到端：stream/cancel 帧转发、不落盘、不占 seq。"""
import os, sys, json, asyncio, tempfile, time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import websockets
from backend import hive_relay as R
from backend import hive as H

PASS, FAIL = [], []
def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [XX] ") + name + (("  " + str(extra)) if extra and not cond else ""))

async def main():
    tmp = tempfile.mkdtemp(prefix="hive_relay_")
    R.set_data_dir(tmp)
    port = 8791
    server = await websockets.serve(R.handler, "127.0.0.1", port,
                                    process_request=R._process_request)
    url = f"ws://127.0.0.1:{port}"
    gid = "g_streamtest"
    key = H._new_key() if hasattr(H, "_new_key") else os.urandom(32).hex()

    a = await websockets.connect(url)
    b = await websockets.connect(url)
    await a.send(json.dumps({"t": "join", "gid": gid, "uid": "u_a", "name": "A", "v": 2}))
    ra = json.loads(await a.recv())
    await b.send(json.dumps({"t": "join", "gid": gid, "uid": "u_b", "name": "B", "v": 2}))
    rb = json.loads(await b.recv())
    # 收掉 presence 广播
    try:
        while True:
            await asyncio.wait_for(a.recv(), timeout=0.15)
    except Exception:
        pass

    print("\n[1] stream 帧转发")
    blob = H._seal(key, {"mid": "hd_1", "phase": "thinking", "delta": "我在想",
                         "task": {"asker": "A"}, "seq_in": 1})
    await a.send(json.dumps({"t": "stream", "gid": gid, "mid": "hd_1", "blob": blob}))
    got = None
    try:
        while True:
            m = json.loads(await asyncio.wait_for(b.recv(), timeout=1.0))
            if m.get("t") == "stream":
                got = m
                break
    except Exception:
        pass
    chk("B 收到了 stream 帧", got is not None, got)
    body = H._open(key, got["blob"]) if got else None
    chk("密文能解开", isinstance(body, dict), body)
    chk("mid 保留", body and body.get("mid") == "hd_1", body)
    chk("phase 保留", body and body.get("phase") == "thinking", body)
    chk("delta 保留", body and body.get("delta") == "我在想", body)
    chk("中继侧看不到明文", got and "我在想" not in json.dumps(got, ensure_ascii=False))

    print("\n[2] stream 不占 seq、不落盘")
    seq_before = R.ROOMS[gid].seq if gid in R.ROOMS else 0
    for i in range(5):
        bl = H._seal(key, {"mid": "hd_1", "phase": "content", "delta": f"第{i}段"})
        await a.send(json.dumps({"t": "stream", "gid": gid, "mid": "hd_1", "blob": bl}))
    await asyncio.sleep(0.3)
    seq_after = R.ROOMS[gid].seq if gid in R.ROOMS else 0
    chk("5 个进度帧没有推高 seq", seq_after == seq_before, (seq_before, seq_after))
    hist = R._read_since(gid, 0)
    chk("进度帧没写进历史", len(hist) == 0, len(hist))

    print("\n[3] cancel 帧转发")
    cbl = H._seal(key, {"mid": "hd_1", "by": "u_a"})
    await a.send(json.dumps({"t": "cancel", "gid": gid, "mid": "hd_1", "blob": cbl}))
    gotc = None
    try:
        while True:
            m = json.loads(await asyncio.wait_for(b.recv(), timeout=1.0))
            if m.get("t") == "cancel":
                gotc = m
                break
    except Exception:
        pass
    chk("B 收到了 cancel 帧", gotc is not None, gotc)
    cb = H._open(key, gotc["blob"]) if gotc else None
    chk("cancel 的 by 保留", cb and cb.get("by") == "u_a", cb)

    print("\n[4] 进度帧不影响正式消息")
    await a.send(json.dumps({"t": "msg", "gid": gid, "mid": "m_1",
                             "blob": H._seal(key, {"name": "A", "type": "text", "text": "正式消息"})}))
    gotm = None
    try:
        while True:
            m = json.loads(await asyncio.wait_for(b.recv(), timeout=1.0))
            if m.get("t") == "msg":
                gotm = m
                break
    except Exception:
        pass
    chk("正式消息正常到达", gotm is not None, gotm)
    chk("正式消息 seq 从 1 开始（没被进度帧占号）", gotm and gotm.get("seq") == 1, gotm.get("seq") if gotm else None)

    print("\n[5] 空 blob 的 stream 被忽略，不断连")
    await a.send(json.dumps({"t": "stream", "gid": gid, "mid": "x", "blob": ""}))
    await asyncio.sleep(0.15)
    chk("连接仍然活着", a.state.name == "OPEN" or not a.closed, a.state)

    await a.close()
    await b.close()
    server.close()
    await server.wait_closed()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

asyncio.run(main())
print("\n" + "=" * 50)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项:", FAIL)
