# -*- coding: utf-8 -*-
import os

root = os.path.dirname(os.path.abspath(__file__))
for f in ["_chk_audio.py", "_chk_js.py"]:
    p = os.path.join(root, f)
    if os.path.exists(p):
        try:
            os.remove(p)
            print("removed", f)
        except Exception as e:
            print("fail", f, e)
try:
    os.remove(os.path.abspath(__file__))
    print("removed self")
except Exception as e:
    print("self remove failed:", e)
