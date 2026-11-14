# -*- coding: utf-8 -*-
import os
d = os.path.dirname(os.path.abspath(__file__))
for f in ("_final_clean.py", "_bye2.py"):
    p = os.path.join(d, f)
    try:
        if os.path.exists(p) and os.path.abspath(p) != os.path.abspath(__file__):
            os.remove(p)
            print("removed", f)
    except Exception as ex:
        print("fail", f, ex)
print("left:", [x for x in os.listdir(d) if x.startswith("_") and x.endswith(".py")])
