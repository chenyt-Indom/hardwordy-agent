# -*- coding: utf-8 -*-
import os, sys
me = os.path.abspath(__file__)
d = os.path.dirname(me)
left = [f for f in os.listdir(d) if f.startswith("_") and f.endswith(".py")]
print("before:", left)
try:
    os.remove(me)
    print("self removed")
except Exception as ex:
    print("self remove failed:", ex)
left = [f for f in os.listdir(d) if f.startswith("_") and f.endswith(".py")]
print("after:", left)
