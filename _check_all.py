# -*- coding: utf-8 -*-
"""编译 + 路由注册 + 接口逻辑自检（不起正式服务）。"""
import py_compile
import sys

ok = True
for f in ("backend/hive_relay_link.py", "backend/hive_client.py",
          "backend/main.py"):
    try:
        py_compile.compile(f, doraise=True)
        print("[OK] compile %s" % f)
    except Exception as e:
        ok = False
        print("[FAIL] compile %s -> %s" % (f, e))

if not ok:
    raise SystemExit(1)

sys.path.insert(0, ".")
try:
    import backend.main as M
    print("[OK] import backend.main（%d 条路由）" % len(M.app.routes))
except Exception as e:
    print("[FAIL] import backend.main -> %s: %s" % (type(e).__name__, e))
    raise SystemExit(1)

paths = sorted({getattr(r, "path", "") for r in M.app.routes})
need = ["/api/hive/relay", "/api/hive/relay/test", "/api/hive/relay/probe_all"]
for p in need:
    print("  %-32s %s" % (p, "已注册" if p in paths else "缺失"))
    if p not in paths:
        ok = False

# 配置读写（不真的改文件：只读）
import backend.hive_client as HC
cfg = HC.relay_config()
print("\n当前配置：url=%s  urls=%s  token_set=%s"
      % (cfg.get("url"), cfg.get("urls"), bool(cfg.get("token"))))
print("掩码输出：%s" % HC.mask_relay(cfg))

print("\n结果：%s" % ("全部通过" if ok else "有失败"))
sys.exit(0 if ok else 1)
