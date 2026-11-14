import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\sadtalker"
done = os.path.join(BASE, "_all.done")
if os.path.exists(done):
    os.remove(done)
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_st_dl.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("sadtalker download pid", p.pid)
