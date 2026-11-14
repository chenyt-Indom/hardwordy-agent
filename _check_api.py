# -*- coding: utf-8 -*-
"""接口层验证：probe_all / relay 状态字段 / 配置读写（不碰真实群）。"""
import json
import sys

sys.path.insert(0, ".")
from fastapi.testclient import TestClient
import backend.main as M

c = TestClient(M.app)
ok_all = True


def rec(name, ok, detail=""):
    global ok_all
    if not ok:
        ok_all = False
    print("  [%s] %-38s %s" % ("PASS" if ok else "FAIL", name, detail))


print("=" * 66)
print("1. /api/hive/relay（配置 + 实时状态字段）")
print("=" * 66)
r = c.get("/api/hive/relay")
rec("HTTP 200", r.status_code == 200, "code=%d" % r.status_code)
d = r.json()
live = d.get("live") or {}
cfg = d.get("config") or {}
rec("配置含 url", bool(cfg.get("url")), cfg.get("url"))
rec("配置含 urls 字段", "urls" in cfg, str(cfg.get("urls")))
rec("密钥只回显掩码", "token" not in cfg and cfg.get("token_set") is True,
    cfg.get("token_hint"))
if live:
    for f in ("active_url", "candidates", "cand_count", "heartbeat_fail",
              "tls_unverified", "last_rx_ago"):
        rec("状态字段 %s 存在" % f, f in live, str(live.get(f))[:60])
else:
    print("  [SKIP] 当前没有群在线，live 为空（正常）—— 改为直接查 status()")
    import backend.hive_relay_link as R
    st = R.RelayLink("").status()
    for f in ("active_url", "candidates", "cand_count", "heartbeat_fail",
              "tls_unverified", "last_rx_ago", "fail_streak", "short_lived"):
        rec("status() 字段 %s" % f, f in st, str(st.get(f))[:50])

print()
print("=" * 66)
print("2. /api/hive/relay/probe_all（全部线路体检）")
print("=" * 66)
r = c.post("/api/hive/relay/probe_all", json={})
rec("HTTP 200", r.status_code == 200, "code=%d" % r.status_code)
d = r.json()
print("  total=%s alive=%s" % (d.get("total"), d.get("alive")))
for it in d.get("items") or []:
    print("    %-40s %-4s %6s ms %s"
          % (it.get("url"), "通" if it.get("ok") else "断",
             it.get("ms") or 0, (it.get("error") or "")[:40]))
rec("至少 2 条线路可用", (d.get("alive") or 0) >= 2,
    "%s/%s" % (d.get("alive"), d.get("total")))
rec("每条都有 url 字段", all(x.get("url") for x in (d.get("items") or [])))

print()
print("=" * 66)
print("3. /api/hive/relay/test（单条探测，旧接口兼容）")
print("=" * 66)
r = c.post("/api/hive/relay/test", json={})
d = r.json()
rec("主地址可达", bool(d.get("ok")), "%s ms" % d.get("ms"))

print()
print("=" * 66)
print("4. 配置保存（只测解析，不真改文件：urls 传空数组=清空额外地址）")
print("=" * 66)
import backend.hive_client as HC
before = HC.relay_config()
# 直接测函数：给同样的 url/token，urls 不传 → 应保持原值
after = HC.set_relay_config(before.get("url"), "", True, keep_token=True)
rec("不传 urls 时保持原值", (after.get("urls") or []) == (before.get("urls") or []),
    str(after.get("urls")))
rec("密钥未被清空", after.get("token_set") is True, after.get("token_hint"))
rec("地址未被改动", after.get("url") == before.get("url"), after.get("url"))

print()
print("=" * 66)
print("结果：%s" % ("全部通过" if ok_all else "有失败"))
print("=" * 66)
sys.exit(0 if ok_all else 1)
