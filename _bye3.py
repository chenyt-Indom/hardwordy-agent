# -*- coding: utf-8 -*-
import os
d = os.path.dirname(os.path.abspath(__file__))
me = os.path.abspath(__file__)
for f in os.listdir(d):
    p = os.path.join(d, f)
    if f.startswith("_") and f.endswith(".py") and os.path.abspath(p) != me:
        try: os.remove(p)
        except Exception: pass
os.remove(me)
