# -*- coding: utf-8 -*-
"""验证 open_ports.py 的无凭据降级路径 + 清理临时文件"""
import sys, io, os, subprocess, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

root = os.path.dirname(os.path.abspath(__file__))

print("=== 1. 编译检查 ===")
r = subprocess.run([sys.executable, "-m", "py_compile", "deploy/open_ports.py"],
                   cwd=root, capture_output=True, text=True)
print("  rc =", r.returncode, r.stderr[:200] if r.stderr else "OK")

print("\n=== 2. 无凭据时的降级路径 ===")
r = subprocess.run([sys.executable, "deploy/open_ports.py", "--list"],
                   cwd=root, capture_output=True, text=True, timeout=60)
print("  rc =", r.returncode)
out = (r.stdout or "") + (r.stderr or "")
print("  输出前 8 行：")
for line in out.splitlines()[:8]:
    print("   ", line)

print("\n=== 3. --help 可用 ===")
r = subprocess.run([sys.executable, "deploy/open_ports.py", "--help"],
                   cwd=root, capture_output=True, text=True, timeout=30)
print("  rc =", r.returncode, "| 含 --port:", "--port" in (r.stdout or ""))

print("\n=== 4. 清理临时脚本 ===")
removed = []
for f in glob.glob(os.path.join(root, "_*.py")):
    name = os.path.basename(f)
    if name in ("_cleanup2.py",):
        continue
    try:
        os.remove(f)
        removed.append(name)
    except Exception as ex:
        print(f"  跳过 {name}: {ex}")
print(f"  已删除 {len(removed)} 个：{removed}")

print("\n=== 5. deploy/ 目录最终内容 ===")
for f in sorted(os.listdir(os.path.join(root, "deploy"))):
    p = os.path.join(root, "deploy", f)
    print(f"  {f}  ({os.path.getsize(p)} bytes)")
