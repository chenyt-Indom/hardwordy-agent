import sys

sys.path.insert(0, ".")
from backend import music

keep = {"gufeng_20261112_231817_5127", "gufeng_20261112_231819_3048"}
drop = [
    "piano_20261112_231613_2487",
    "gufeng_20261112_231715_0536",
    "piano_20261112_231717_6636",
    "gufeng_20261112_231809_3527",
]
for mid in drop:
    if mid in keep:
        continue
    r = music.delete(mid)
    print(mid, "->", r.get("removed"))

print("\n保留:")
for m in music.list_music(limit=10):
    if m["id"] in keep:
        print("  %s  %.1fs  %s" % (m["id"], m.get("duration") or 0, m["url"]))
