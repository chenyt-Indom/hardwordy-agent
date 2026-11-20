# -*- coding: utf-8 -*-
"""临时：证明「regress 子进程无输出」这条失败与本次改动无关（基线复现）。用完即删。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def git(*a):
    r = subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


print("status before:", git("status", "--porcelain")[1][:400])
rc, out = git("stash", "push", "--", "backend/main.py")
print("stash rc=%s" % rc, out.strip()[:200])
try:
    r = subprocess.run([PY, "-c",
                        "import sys;sys.path.insert(0,'.');from backend import regress;"
                        "r=regress.run_disk(scope='quick',timeout=120);"
                        "print('BASELINE ok=',r.get('ok'),r.get('summary'))"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(r.stdout.strip()[-600:])
    print("stderr:", (r.stderr or "").strip()[-300:])
finally:
    rc2, out2 = git("stash", "pop")
    print("pop rc=%s" % rc2, out2.strip()[:300])
print("status after:", git("status", "--porcelain")[1][:400])
