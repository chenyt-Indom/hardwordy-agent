import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
# 删掉损坏的文件
p = os.path.join(BASE, "4x_BigFace.pth")
if os.path.exists(p):
    os.remove(p)
d = os.path.join(BASE, "_bf_ok.txt")
if os.path.exists(d):
    os.remove(d)
proc = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_bf_dl2.py"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=0x00000008)
print("pid", proc.pid)
