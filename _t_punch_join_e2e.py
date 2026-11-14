# -*- coding: utf-8 -*-
"""端到端：A 建群 → B 拿 punch:// 地址按密码入群 → 打洞连上。
测完自动删掉测试群，不污染你的数据。"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.getcwd())

from backend import hive as H          # noqa: E402
from backend import hive_client as HC  # noqa: E402
from backend import hive_mesh as HM    # noqa: E402

ok = 0
bad = 0


def chk(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s %s" % (name, extra))
    else:
        bad += 1
        print("  FAIL  %s %s" % (name, extra))


g = H.create_group("打洞测试群", "A")
gid = g["gid"]
pw = g["password"]
_g = H.load_group(gid) or {}
print("测试群 %s 密码 %s" % (gid, pw))

a = None
try:
    # A：群主，另起一个节点
    a = HM.MeshNode()
    a.start(0, [])
    a.add_group(gid, vt=g["vt"], gname="打洞测试群")
    time.sleep(0.4)
    chk("A 打洞通道就绪", bool(a.punch),
        "udp=%s" % (a.punch.port if a.punch else None))

    # B：用全局单例，走真实 join_by_password
    b = HC.client()
    bnode = HM.MESH.node()
    bnode.start(0, [])
    time.sleep(0.4)

    addr = "punch://127.0.0.1:%d" % a.punch.port
    print("  B 用地址 %s 入群" % addr)
    t0 = time.time()
    # 走路由同一套：先 parse_code 拆解，再交给 join_by_password
    pc = bnode.parse_code(addr)
    chk("parse_code 拆出 punch 模式",
        pc.get("ok") and pc.get("mode") == "punch", str(pc))
    r = b.join_by_password(pw, "B", pc["host"], pc["port"], pc["mode"])
    dt = time.time() - t0
    print("  -> ok=%s via=%s warn=%s %.2fs"
          % (r.get("ok"), r.get("via"), r.get("warn") or "", dt))
    chk("B 入群成功", r.get("ok"), r.get("error") or "")
    chk("来源标记为指定地址", r.get("via") == "addr", str(r.get("via")))
    chk("入群后打洞连接成功（没有 warn）", not r.get("warn"),
        str(r.get("warn") or ""))

    time.sleep(0.5)
    peers = bnode.peers_of(gid)
    chk("B 侧看到 A 的直连 peer", len(peers) >= 1, "peers=%d" % len(peers))
finally:
    try:
        if a:
            a.stop()
    except Exception:
        pass
    try:
        HC.client().stop()
    except Exception:
        pass
    try:
        H.delete_group(gid, force=True)
        print("  已删除测试群 %s" % gid)
    except Exception as e:
        print("  删除测试群失败：%s" % e)

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
