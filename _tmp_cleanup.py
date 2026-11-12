import os
import subprocess

for f in ("_tmp_zhiguo.py",):
    try:
        os.remove(f)
        print("removed", f)
    except OSError as e:
        print("fail", f, e)

me = os.path.abspath(__file__)
subprocess.Popen(["cmd", "/c", 'ping -n 2 127.0.0.1 >nul & del /f /q "%s"' % me])
print("cleanup scheduled")
