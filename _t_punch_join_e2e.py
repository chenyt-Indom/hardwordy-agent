# -*- coding: utf-8 -*-
"""端到端：A 建群 → 清掉本地记录（模拟「另一台机器」）→ B 拿 punch:// 地址入群。"""
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
vt = _g.get("vt") or ""
print("测试群 %s 密码 %s" % (gid, pw))

a = None
try:
    a = HM.MeshNode()
    a.start(0, [])
    a.add_group(gid, vt=vt, gname="打洞测试群")
    time.sleep(0.4)
    chk("A 打洞通道就绪", bool(a.punch),
        "udp=%s" % (a.punch.port if a.punch else None))

    # 关键：删掉本机记录，模拟「群在另一台机器上」
    H.delete_group(gid, force=True)
    time.sleep(0.2)
    chk("本地记录已清除（模拟另一台机器）", gid not in H.all_group_ids(),
        "groups=%d" % len(H.all_group_ids()))

    b = HC.client()
    bnode = HC.MESH.node()
    bnode.start(0, [])
    time.sleep(0.4)

    addr = "punch://127.0.0.1:%d" % a.punch.port
    print("  B 用地址 %s 入群" % addr)
    pc = bnode.parse_code(addr)
    chk("parse_code 拆出 punch 模式",
        pc.get("ok") and pc.get("mode") == "punch", str(pc))
    t0 = time.time()
    r = b.join_by_password(pw, "B", pc["host"], pc["port"], pc["mode"])
    dt = time.time() - t0
    print("  -> ok=%s via=%s warn=%s %.2fs"
          % (r.get("ok"), r.get("via"), r.get("warn") or "", dt))
    chk("B 入群成功", r.get("ok"), r.get("error") or "")
    chk("来源是「指定地址」（不是本地/局域网）",
        r.get("via") == "addr", str(r.get("via")))
    chk("入群后打洞连接成功（无 warn）", not r.get("warn"),
        str(r.get("warn") or ""))

    time.sleep(0.6)
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
