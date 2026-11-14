# -*- coding: utf-8 -*-
import os, shutil, glob
root = os.path.dirname(os.path.abspath(__file__))
for p in glob.glob(os.path.join(root, "deploy", "__pycache__")):
    shutil.rmtree(p, ignore_errors=True)
for p in glob.glob(os.path.join(root, "_*.py")):
    if os.path.basename(p) == "_final_clean.py":
        continue
    try: os.remove(p)
    except Exception: pass
print("deploy/:", sorted(os.listdir(os.path.join(root, "deploy"))))
print("根目录残留 _*.py:", [os.path.basename(x) for x in glob.glob(os.path.join(root, "_*.py"))])
