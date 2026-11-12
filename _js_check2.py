# -*- coding: utf-8 -*-
"""单独复核 artifacts.js（上一轮 node 启动失败，疑似环境抖动）。"""
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

root = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(root, "frontend", "artifacts.js")
print("大小", os.path.getsize(p), "字节")
print("修改时间", time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(os.path.getmtime(p))))

for i in range(3):
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(f"第 {i + 1} 次 rc={r.returncode}", (r.stderr or "").strip()[:600])
    if r.returncode == 0:
        print("→ 语法正常（之前那次是 node 启动失败，非代码问题）")
        break
    time.sleep(0.6)
