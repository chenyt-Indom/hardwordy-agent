import sys, os, importlib, glob, subprocess

for m in ["websocket", "websockets", "requests", "uvicorn", "playwright"]:
    try:
        importlib.import_module(m)
        print("OK  ", m)
    except Exception as e:
        print("MISS", m, type(e).__name__)

for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
          r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]:
    print("edge:", os.path.isfile(p), p)

# 检查 main.py 启动方式
mp = r"D:\local-multimodal-src\backend\main.py"
tail = open(mp, encoding="utf-8", errors="ignore").read()[-1200:]
print("---- main.py tail ----")
print(tail[-600:])
