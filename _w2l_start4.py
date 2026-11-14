import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_w2l_dl2.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("wav2lip.pth download pid", p.pid)
