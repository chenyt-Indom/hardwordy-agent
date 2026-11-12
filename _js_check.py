# -*- coding: utf-8 -*-
"""用 node --check 校验前端 JS 语法（语法错会导致整个模块不执行）。"""
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

root = os.path.dirname(os.path.abspath(__file__))
ok = True
for rel in ("frontend/app.js", "frontend/artifacts.js"):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        print(f"{rel}: 不存在，跳过")
        continue
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(f"{rel}: rc={r.returncode}")
    if r.returncode != 0:
        ok = False
        print((r.stderr or "")[:3000])
    else:
        print(f"  行数 {sum(1 for _ in open(p, encoding='utf-8'))}")

print("\n语法全部通过" if ok else "\n有语法错误，见上")
