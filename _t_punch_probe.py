# -*- coding: utf-8 -*-
"""验证 punch:// 地址入群：解析 + 打洞探群 + 自己地址拦截。"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from backend import hive_mesh as HM  # noqa: E402

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


print("=== 1. parse_code 各种写法（这就是 getaddrinfo 的根因）===")
cases = [
    ("punch://219.136.70.51:12510", "219.136.70.51", 12510, "punch"),
    ("udp://1.2.3.4:999", "1.2.3.4", 999, "punch"),
    ("hive3://192.168.1.5:8899", "192.168.1.5", 8899, "tcp"),
    ("192.168.1.5:8899", "192.168.1.5", 8899, "tcp"),
    ("[::1]:8899", "::1", 8899, "tcp"),
]
for s, eh, ep, em in cases:
    r = HM.MeshNode.parse_code(None, s)
    chk(s, r.get("ok") and r.get("host") == eh
        and r.get("port") == ep and r.get("mode") == em, str(r))

r = HM.MeshNode.parse_code(None, "1.2.3.4")
chk("只写 IP（无端口）应报错而不是当主机名", not r.get("ok"), str(r))

print("=== 2. 真实两节点：打洞探群 ===")
a = HM.MeshNode()
a.start(0, [])
b = HM.MeshNode()
b.start(0, [])
time.sleep(0.6)
chk("A 打洞通道启动", bool(a.punch), "udp_port=%s" % (a.punch.port if a.punch else None))
chk("B 打洞通道启动", bool(b.punch))

# 给 A 造一个群（probe 只回 gid/vt/gn，不涉及密钥）
a.add_group("gtest123", vt="VTAGTEST", gname="测试群")
time.sleep(0.2)

t0 = time.time()
pr = b.probe_punch("127.0.0.1", a.punch.port)
dt = time.time() - t0
chk("打洞探群成功", pr.get("ok"), "%.2fs %s" % (dt, pr.get("error") or ""))
if pr.get("ok"):
    gs = pr.get("groups") or []
    chk("拿到 A 的群列表", any(g.get("gid") == "gtest123" for g in gs),
        str(gs))

print("=== 3. 拿自己地址入群应被拦下 ===")
own = a.punch_addr()
if own.get("ok"):
    ip, _, pt = own["addr"].rpartition(":")
    r2 = a.probe_punch(ip, int(pt))
    chk("自己地址被拦", (not r2.get("ok"))
        and "你自己的地址" in (r2.get("error") or ""), str(r2.get("error"))[:60])
else:
    print("  SKIP  本机 STUN 没拿到映射（%s）" % own.get("error"))

print("=== 4. 打洞到不存在的端口：应给清楚提示，不能是 getaddrinfo ===")
r3 = b.probe_punch("127.0.0.1", 1, timeout=3.0)
chk("失败有可读提示", (not r3.get("ok")) and "getaddrinfo" not in
    (r3.get("error") or ""), str(r3.get("error"))[:70])

try:
    a.stop()
    b.stop()
except Exception:
    pass

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
