# -*- coding: utf-8 -*-
"""清理本轮测试脚本与临时数据，并检查真实群数据目录状态。"""
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

root = os.path.dirname(os.path.abspath(__file__))

# 1. 删测试脚本
for f in ("_hive_test.py", "_hive_e2e.py", "_hive_routes_test.py",
          "_hive_api_test.py", "_js_check.py", "_js_check2.py"):
    p = os.path.join(root, f)
    if os.path.exists(p):
        os.remove(p)
        print("已删除", f)

# 2. 删测试数据目录
for d in ("_hive_e2e_data", "_hive_e2e_relay", "_hive_api_data"):
    p = os.path.join(root, d)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        print("已删除目录", d)

# 3. 检查真实群数据
real = os.path.join(root, "data", "hive")
print("\n真实群数据目录:", real)
if os.path.isdir(real):
    for dirpath, dirnames, filenames in os.walk(real):
        rel = os.path.relpath(dirpath, real)
        for fn in filenames:
            print("  ", os.path.join(rel, fn) if rel != "." else fn)
    prof = os.path.join(real, "profile.json")
    if os.path.exists(prof):
        print("  本机身份文件存在")
else:
    print("   还不存在（第一次用群聊时会自动创建）")

# 4. 确认代码文件都在
print("\n代码文件:")
for f in ("backend/hive.py", "backend/hive_client.py", "backend/hive_relay.py"):
    p = os.path.join(root, f)
    print(f"  {f}: {'OK ' + str(os.path.getsize(p)) + ' 字节' if os.path.exists(p) else '缺失！'}")
