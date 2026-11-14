import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\sadtalker"
d = os.path.join(BASE, "_all2.done")
if os.path.exists(d):
    os.remove(d)
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_st_dl2.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("pid", p.pid)
