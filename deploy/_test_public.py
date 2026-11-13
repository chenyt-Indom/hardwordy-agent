# -*- coding: utf-8 -*-
"""临时：从公网走 nginx 的 WebSocket 通道，测群目录 publish/lookup。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import hive_relay_link as L      # noqa: E402

URL = "ws://139.199.69.88/hive-relay"
TOK = "itG"   # 只用来判断前缀，真实密钥从 relay.json 读
import json                                   # noqa: E402

cfg = json.load(open(os.path.join(ROOT, "data", "hive", "relay.json"),
                     encoding="utf-8"))
TOKEN = cfg["token"]

PW = "TEST-DIR9-ZZZZ"
GID = "gpublic999"

R = []


def add(name, ok, extra=""):
    R.append((name, bool(ok), extra))


# 1. 公网 WS 能握手 + 取 health
r = L._dir_call(URL, TOKEN, "dir_lookup", {"fp": L.password_fp("NONE-NONE-NONE")})
add("公网 WS 可达", r.get("ok"), str(r.get("error") or "")[:80])

# 2. publish
r = L.dir_publish(URL, TOKEN, PW, GID, "vt-pub-1", "公网测试群")
add("publish", r.get("ok"), str(r.get("error") or "")[:80])

# 3. lookup 命中
r = L.dir_lookup(URL, TOKEN, PW)
hit = (r.get("raw") or {}).get("group") or {}
add("lookup 命中 gid", hit.get("gid") == GID, str(hit)[:80])
add("lookup 带回 vt", hit.get("vt") == "vt-pub-1")
add("lookup 带回群名", hit.get("name") == "公网测试群")

# 4. 错密码查不到
r = L.dir_lookup(URL, TOKEN, "WRON-GPAS-SWRD")
add("错密码查不到", not ((r.get("raw") or {}).get("group") or {}).get("gid"))

# 5. 错密钥被拒
r = L.dir_lookup(URL, "bad-token-xxx", PW)
add("错密钥被拒", not r.get("ok"))

# 6. 密码格式归一（大小写/连字符）
r = L.dir_lookup(URL, TOKEN, PW.lower().replace("-", " "))
hit = (r.get("raw") or {}).get("group") or {}
add("密码格式归一后仍命中", hit.get("gid") == GID)

# 7. probe 也走 WS
p = L.probe(URL, TOKEN, timeout=8)
add("probe 走 WS", p.get("ok"), str(p.get("error") or "")[:80])
if p.get("ok"):
    add("probe 看到目录数", "dir" in (p.get("health") or {}),
        str((p.get("health") or {}).get("dir")))

ok = sum(1 for _, v, _ in R if v)
for n, v, e in R:
    print("  %-26s %s %s" % (n, "PASS" if v else "FAIL", e if not v else ""))
print("\n%d/%d 通过" % (ok, len(R)))
sys.exit(0 if ok == len(R) else 1)
