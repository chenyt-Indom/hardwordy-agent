# -*- coding: utf-8 -*-
import os
import subprocess

root = os.path.dirname(os.path.abspath(__file__))
r = subprocess.run(["node", "--check", os.path.join(root, "frontend/app.js")],
                   capture_output=True, text=True)
print("app.js rc=", r.returncode)
if r.returncode:
    print(r.stderr[:800])
try:
    os.remove(os.path.abspath(__file__))
except Exception as e:
    print("self remove failed:", e)
