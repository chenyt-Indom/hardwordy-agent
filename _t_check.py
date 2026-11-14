"""只做语法编译检查，不删文件、不启动外部程序。"""
import os, sys, py_compile

ROOT = os.path.dirname(os.path.abspath(__file__))
for f in ["backend/avatar2.py", "backend/main.py", "backend/t2i.py",
          "backend/avatar_mouth.py", "backend/avatar_lipsync.py"]:
    p = os.path.join(ROOT, f)
    try:
        py_compile.compile(p, doraise=True)
        print("OK  ", f)
    except Exception as e:
        print("FAIL", f, e)

# 路由自检
try:
    from backend import main
    routes = [r.path for r in main.app.routes]
    print("\nroutes:", len(routes))
    for k in ["/api/avatar2/close_mouth", "/api/avatar2/closed_preview",
              "/api/avatar2/closed_apply", "/api/avatar2/closed_drop"]:
        print("  %-34s %s" % (k, k in routes))
except Exception as e:
    print("import main FAIL:", e)
