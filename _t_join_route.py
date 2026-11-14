# -*- coding: utf-8 -*-
"""路由层验证：把 punch:// 整串粘进地址框，确认不再出现 getaddrinfo failed。"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.getcwd())

from backend.main import hive_join  # noqa: E402

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


print("=== 1. 你截图里的地址（punch:// 整串粘进来）===")
r1 = hive_join({"password": "ABCD-EFGH-JKMN",
                "host": "punch://219.136.70.51:12510"})
e1 = r1.get("error") or ""
print("  -> %s" % e1[:110])
chk("不再出现 getaddrinfo failed", "getaddrinfo" not in e1)
chk("不再出现「punch://」被当主机名", "连不上 punch://" not in e1)

print("=== 2. punch:// 指向死端口 ===")
r2 = hive_join({"password": "ABCD-EFGH-JKMN",
                "host": "punch://127.0.0.1:1"})
e2 = r2.get("error") or ""
print("  -> %s" % e2[:110])
chk("错误可读且与打洞有关",
    "getaddrinfo" not in e2 and ("打洞" in e2 or "超时" in e2))

print("=== 3. 普通 IP:端口 仍然走 TCP（不能改坏）===")
r3 = hive_join({"password": "ABCD-EFGH-JKMN", "host": "127.0.0.1:1"})
e3 = r3.get("error") or ""
print("  -> %s" % e3[:110])
chk("TCP 路径报的是连接失败/超时",
    "getaddrinfo" not in e3 and ("连不上" in e3 or "超时" in e3 or "拒绝" in e3))

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(0 if bad == 0 else 1)
