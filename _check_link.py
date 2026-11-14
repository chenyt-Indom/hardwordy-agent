# -*- coding: utf-8 -*-
"""编译 + 关键逻辑静态自检（不需要起服务）。"""
import py_compile
import sys

ok = True
for f in ("backend/hive_relay_link.py",):
    try:
        py_compile.compile(f, doraise=True)
        print("[OK] compile %s" % f)
    except Exception as e:
        ok = False
        print("[FAIL] compile %s -> %s" % (f, e))

sys.path.insert(0, ".")
try:
    import backend.hive_relay_link as R
    print("[OK] import backend.hive_relay_link")
except Exception as e:
    ok = False
    print("[FAIL] import -> %s: %s" % (type(e).__name__, e))
    raise SystemExit(1)

# 1) 候选派生
c = R.candidates_for("ws://139.199.69.88/hive-relay")
print("\n候选线路（共 %d 条）：" % len(c))
for i, p in enumerate(c):
    print("  %d. %-40s ws=%-5s tls=%-5s path=%s"
          % (i + 1, p.get("url"), p.get("ws"), p.get("tls"), p.get("path")))

# 2) 去重
c2 = R.candidates_for("ws://139.199.69.88/hive-relay",
                      ["wss://139.199.69.88:443/hive-relay", "abc"])
print("\n去重检查：%d 条 -> %d 条（额外地址去重生效）" % (len(c), len(c2)))

# 3) 裸 TCP 主地址不会派生 WS 备份
c3 = R.candidates_for("hive4://1.2.3.4:18765")
print("裸 TCP 主地址 -> %d 条：%s" % (len(c3), [x.get("url") for x in c3]))

# 4) 空/非法地址
print("非法地址 -> %d 条" % len(R.candidates_for("")))
print("乱写地址 -> %d 条" % len(R.candidates_for("不是地址")))

# 5) 常量
print("\n常量：CONNECT_TIMEOUT=%s BACKOFF_MAX=%s HB_IDLE=%s STABLE=%s"
      % (R.CONNECT_TIMEOUT, R.BACKOFF_MAX, R.HB_IDLE, R.STABLE_SECONDS))

print("\n结果：%s" % ("全部通过" if ok else "有失败"))
