# -*- coding: utf-8 -*-
"""总回归：语法 + 编译 + 群产物相关全部单测。"""
import os
import sys
import subprocess
import py_compile

root = os.getcwd()
bad = []

for f in ("backend/main.py", "backend/hive.py", "backend/hive_mesh.py",
          "backend/hive_client.py", "backend/workspace.py", "backend/tools.py",
          "backend/avatar_lipsync.py", "backend/avatar25d.py"):
    try:
        py_compile.compile(os.path.join(root, f), doraise=True)
    except Exception as e:
        bad.append("compile %s: %s" % (f, e))
print("编译检查: %s" % ("全通过" if not bad else "有失败"))

r = subprocess.run(["node", "--check", os.path.join(root, "frontend", "app.js")],
                   capture_output=True, text=True, timeout=90)
print("node --check rc =", r.returncode)
if r.returncode:
    bad.append("node --check: " + r.stderr[-400:])

# 路由数量
from backend import main
routes = len([x for x in main.app.routes])
print("路由数:", routes)
for p in ("/api/hive/{gid}/artifacts", "/api/hive/{gid}/artifacts/open"):
    found = any(getattr(x, "path", "") == p for x in main.app.routes)
    print("  %s -> %s" % (p, "已注册" if found else "缺失"))
    if not found:
        bad.append("route missing " + p)

# 跑各个子测试
for script in ("_qa_art1.py", "_qa_art2.py", "_qa_art5.py",
               "_qa_art6.py", "_qa_art7.py"):
    p = os.path.join(root, script)
    if not os.path.isfile(p):
        continue
    r = subprocess.run([sys.executable, p], capture_output=True, text=True,
                       timeout=300, cwd=root)
    ok = r.returncode == 0
    print("%-14s rc=%s" % (script, r.returncode))
    if not ok:
        bad.append(script + " 失败\n" + (r.stdout or "")[-800:] + (r.stderr or "")[-500:])

print("\n==== 总结 ====")
if bad:
    print("失败 %d 项：" % len(bad))
    for b in bad:
        print(" -", b)
    sys.exit(1)
print("全部通过")
