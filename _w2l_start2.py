import subprocess
import os

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
done = os.path.join(BASE, "_dl.done")
if os.path.exists(done):
    os.remove(done)
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_w2l_mtdl.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("started mt-download pid", p.pid)
