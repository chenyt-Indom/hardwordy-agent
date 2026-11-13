"""临时：校验 run.py 语法，跑完即删。"""
import py_compile
import sys

for f in ("run.py",):
    try:
        py_compile.compile(f, doraise=True)
        print("OK", f)
    except Exception as e:
        print("FAIL", f, repr(e))
        sys.exit(1)

# 顺带确认改动后的关键常量能正确求值
import os
os.environ.pop("MM_HOST", None)
src = open("run.py", encoding="utf-8").read()
print("has_MM_HOST_env:", "MM_HOST" in src)
print("default_0000:", 'os.environ.get("MM_HOST") or "0.0.0.0"' in src)
print("url_loopback:", 'URL = f"http://127.0.0.1:{PORT}/"' in src)
