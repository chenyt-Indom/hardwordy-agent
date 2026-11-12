import json
import os
import sys

sys.path.insert(0, ".")
from backend import music

mid = "piano_20261112_231613_2487"
with open(music._score_path(mid), encoding="utf-8") as f:
    sc = json.load(f)

tr = sc.get("tracks") or {}
print("tracks:", list(tr.keys()))
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pn(p):
    return "%s%d" % (NAMES[int(p) % 12], int(p) // 12 - 1)


for k, v in tr.items():
    if not isinstance(v, list):
        continue
    notes = [n for n in v if isinstance(n, dict) and "p" in n]
    if not notes:
        continue
    seq = [pn(n["p"]) for n in notes[:24]]
    print("%-10s n=%-4d %s" % (k, len(notes), " ".join(seq)))
