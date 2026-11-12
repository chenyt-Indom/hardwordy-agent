import json
import os
import sys

sys.path.insert(0, ".")
from backend import music

mid = "piano_20261112_231613_2487"
d = music._base()
sp = music._score_path(mid)
print("score path:", sp, "exists:", os.path.isfile(sp))
if os.path.isfile(sp):
    with open(sp, encoding="utf-8") as f:
        sc = json.load(f)
    print("score keys:", list(sc.keys())[:12])
    # 找主旋律轨
    for k in sc.keys():
        v = sc[k]
        if isinstance(v, list) and v and isinstance(v[0], dict):
            print("  track:", k, "notes:", len(v), "sample:", v[:4])
