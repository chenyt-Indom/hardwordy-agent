# -*- coding: utf-8 -*-
"""临时测试：中继群目录（跨网凭密码找群）。跑完删掉。"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import hive_relay_server as S          # noqa: E402
from backend import hive_relay_link as L           # noqa: E402
from backend import ws_min                         # noqa: E402

PORT = 18793
TOK = "dirtoken456"
tmp = tempfile.mkdtemp()
srv = S.RelayServer("127.0.0.1", PORT, tmp, TOK)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.6)

R = []
PW = "ABCD-EFGH-JKMN"
GID = "gdir1234"
VT = "vt-tag-abc"

# ---- 1. 裸 TCP 目录 publish
r = L._dir_call("127.0.0.1:%d" % PORT, TOK, "dir_publish",
                {"fp": L.password_fp(PW), "gid": GID, "vt": VT, "name": "测试群"})
R.append(("TCP dir_publish", r.get("ok")))

# ---- 2. 裸 TCP 目录 lookup（用同一个密码）
r = L._dir_call("127.0.0.1:%d" % PORT, TOK, "dir_lookup",
                {"fp": L.password_fp(PW)})
hit = (r.get("raw") or {}).get("group") or {}
R.append(("TCP dir_lookup 命中", hit.get("gid") == GID))

# ---- 3. 错密码查不到
r = L._dir_call("127.0.0.1:%d" % PORT, TOK, "dir_lookup",
                {"fp": L.password_fp("ZZZZ-ZZZZ-ZZZZ")})
hit2 = (r.get("raw") or {}).get("group") or {}
R.append(("错密码查不到", not hit2.get("gid")))

# ---- 4. WebSocket 目录 lookup（模拟走 nginx）
p = L.parse_url("ws://127.0.0.1:%d/hive-relay" % PORT)
s = None
try:
    s = L._open_sock(p, 5.0)
    got = L._recv_pkt(s)
    nonce = json.loads(got[1].decode())["nonce"]
    import hashlib
    import hmac as _h
    mac = _h.new(TOK.encode(), (nonce + "|dir").encode(), hashlib.sha256).hexdigest()
    L._send_pkt(s, L.K_CTRL, json.dumps(
        {"op": "dir_lookup", "fp": L.password_fp(PW), "mac": mac}).encode())
    got = L._recv_pkt(s)
    rr = json.loads(got[1].decode())
    R.append(("WS dir_lookup 命中", (rr.get("group") or {}).get("gid") == GID))
finally:
    if s:
        try:
            s.close()
        except Exception:
            pass

# ---- 5. 错密钥被拒
r = L._dir_call("127.0.0.1:%d" % PORT, "wrong-token", "dir_lookup",
                {"fp": L.password_fp(PW)})
R.append(("错中继密钥被拒", not r.get("ok")))

# ---- 6. 空 fp 被拒
r = L._dir_call("127.0.0.1:%d" % PORT, TOK, "dir_lookup", {"fp": ""})
R.append(("空 fp 被拒", not r.get("ok")))

# ---- 7. health 里有目录数
r = L._dir_call("127.0.0.1:%d" % PORT, TOK, "dir_lookup", {"fp": L.password_fp(PW)})
R.append(("目录条目存在", bool((r.get("raw") or {}).get("group"))))

# ---- 8. 密码指纹稳定性（格式/大小写/连字符不同，指纹应一致）
a = L.password_fp("abcd-efgh-jkmn")
b = L.password_fp("ABCD EFGH JKMN")
c = L.password_fp("ABCDEFGHJKMN")
R.append(("指纹规范化一致", a == b == c))

# ---- 9. 不同密码指纹不同
R.append(("指纹区分度", L.password_fp(PW) != L.password_fp("AAAA-AAAA-AAAA")))

ok = 0
for k, v in R:
    print("  %-24s %s" % (k, "PASS" if v else "FAIL"))
    ok += 1 if v else 0
print("\n%d/%d 通过" % (ok, len(R)))

srv.shutdown()
time.sleep(0.3)
sys.exit(0 if ok == len(R) else 1)
