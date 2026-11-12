# -*- coding: utf-8 -*-
"""检查群聊路由是否真的注册上（避免"导入不报错、全 404"）。"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import app

paths = sorted({getattr(r, "path", "") for r in app.routes})
hive = [p for p in paths if "/api/hive" in p]
ws = [p for p in paths if p == "/ws/hive"]

print("=== 群聊 REST 路由 ===")
for p in hive:
    print("  ", p)
print(f"\n共 {len(hive)} 条，期望 14 条")
print("=== WebSocket ===")
print("  ", ws or "缺失！")

# 路由处理函数是否可调用
bad = []
for r in app.routes:
    p = getattr(r, "path", "")
    if "/api/hive" in p:
        ep = getattr(r, "endpoint", None)
        if ep is None or not callable(ep):
            bad.append(p)

print("\n=== 结果 ===")
print("路由齐全" if len(hive) >= 14 else f"⚠️ 只有 {len(hive)} 条")
print("ws/hive 存在" if ws else "⚠️ ws/hive 缺失")
print("处理函数都可调用" if not bad else f"⚠️ 异常: {bad}")
