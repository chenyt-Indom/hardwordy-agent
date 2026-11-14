import subprocess
import os

# 杀掉卡住的下载进程
subprocess.run(["powershell", "-NoProfile", "-Command",
                "Stop-Process -Id 68740 -Force -ErrorAction SilentlyContinue"],
               capture_output=True, text=True, timeout=30)
print("killed stuck downloader")
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_w2l_fix.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("fixer pid", p.pid)
