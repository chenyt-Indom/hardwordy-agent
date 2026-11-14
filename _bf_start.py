import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
d = os.path.join(BASE, "_bf.done")
if os.path.exists(d):
    os.remove(d)
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_bf_dl.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("bigface download pid", p.pid)
