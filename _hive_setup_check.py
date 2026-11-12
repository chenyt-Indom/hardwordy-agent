"""前端语法校验 + 后端新路由注册检查。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ok = True

print("1. JS 语法检查")
for f in ["frontend/app.js", "frontend/artifacts.js"]:
    p = os.path.join(ROOT, f)
    try:
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                           timeout=60)
        if r.returncode == 0:
            print(f"  OK   {f}")
        else:
            ok = False
            print(f"  FAIL {f}\n{r.stderr[:600]}")
    except Exception as e:
        ok = False
        print(f"  FAIL {f}   {type(e).__name__}: {e}")

print()
print("2. Python 语法检查")
for f in ["backend/main.py", "backend/hive_deploy.py", "backend/hive_relay.py"]:
    p = os.path.join(ROOT, f)
    try:
        src = open(p, "r", encoding="utf-8").read()
        compile(src, p, "exec")
        print(f"  OK   {f}  ({len(src.splitlines())} 行)")
    except SyntaxError as e:
        ok = False
        print(f"  FAIL {f}   第 {e.lineno} 行: {e.msg}")

print()
print("3. 后端导入 + 路由注册")
sys.path.insert(0, ROOT)
try:
    from backend import main as M
    print("  OK   backend.main 导入成功")
    want = ["/api/hive/relay/probe", "/api/hive/relay/bundle",
            "/api/hive/relay/bundle/download", "/api/hive/relay/last",
            "/api/hive/relay/guide", "/api/hive/create"]
    paths = {}
    for r in M.app.routes:
        p = getattr(r, "path", "")
        if p:
            paths.setdefault(p, set()).add(",".join(sorted(getattr(r, "methods", []) or [])))
    for w in want:
        if w in paths:
            print(f"  OK   {w}  [{paths[w]}]")
        else:
            ok = False
            print(f"  FAIL {w} 未注册")
except Exception as e:
    ok = False
    print(f"  FAIL 导入失败：{type(e).__name__}: {e}")

print()
print("4. HTML 关键元素")
html = open(os.path.join(ROOT, "frontend/index.html"), encoding="utf-8").read()
for i in ["hiveSetup", "hsName", "hsRelay", "hsProbe", "hsBundle", "hsGuide",
          "hsCreate", "hsLog", "hsLast", "hsUseLast", "hsClose", "hsLastUrl"]:
    if f'id="{i}"' in html:
        print(f"  OK   #{i}")
    else:
        ok = False
        print(f"  FAIL #{i} 缺失")

print()
print("5. HTML 标签配平（粗查）")
import re
opens = len(re.findall(r"<div\b", html))
closes = len(re.findall(r"</div>", html))
print(f"  <div> {opens} 个 / </div> {closes} 个 "
      + ("OK" if opens == closes else "⚠️ 不配平"))
if opens != closes:
    ok = False

print()
print("=" * 50)
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
