# -*- coding: utf-8 -*-
import os
import subprocess

root = os.path.dirname(os.path.abspath(__file__))
for f in ["frontend/app.js"]:
    r = subprocess.run(["node", "--check", os.path.join(root, f)],
                       capture_output=True, text=True)
    print(f, "rc=", r.returncode)
    if r.returncode:
        print(r.stderr[:800])
