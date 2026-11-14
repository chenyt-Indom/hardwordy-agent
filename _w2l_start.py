import subprocess, time, os, sys
base = r"D:\local-multimodal-src\data\models\wav2lip"
done = os.path.join(base, "_dl.done")
if os.path.exists(done):
    os.remove(done)
p = subprocess.Popen(["C:\\Python314\\python.exe", r"D:\local-multimodal-src\_w2l_dl.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
print("started pid", p.pid)
time.sleep(4)
part = os.path.join(base, "wav2lip_gan.pth.part")
print("%.1f MB" % (os.path.getsize(part) / 1e6 if os.path.exists(part) else 0))
