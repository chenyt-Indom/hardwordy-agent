import json
import sys

sys.path.insert(0, ".")
from backend import music

melody = (
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 D4:1 E4:2 "
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 A4:2 "
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 E4:2 "
    "G4:1 E4:1 C5:2 A4:1 A4:1 A4:2 "
    "E4:1 E4:1 G4:1 A4:1 A4:1 A4:3"
)
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pn(p):
    return "%s%d" % (NAMES[int(p) % 12], int(p) // 12 - 1)


res = []
for style in ("gufeng", "piano"):
    r = music.generate(style=style, bars=15, bpm=68, vocals=False,
                       title="我的祖国-%s" % style, notes=melody)
    if not r.get("ok"):
        print(style, "FAIL", r.get("error"))
        continue
    m = r["music"]
    with open(music._score_path(m["id"]), encoding="utf-8") as f:
        sc = json.load(f)
    print("== %s  id=%s  dur=%.1fs  bars=%s" % (style, m["id"], m["duration"], m["bars"]))
    print("   tracks:", {k: len(v) for k, v in sc["tracks"].items() if isinstance(v, list)})
    # 找旋律 G4 A4 C5 A4 G4 E4 G4
    target = ["G4", "A4", "C5", "A4", "G4", "E4", "G4"]
    for k, v in sc["tracks"].items():
        if not isinstance(v, list):
            continue
        seq = [pn(n["p"]) for n in v if isinstance(n, dict) and "p" in n]
        for i in range(len(seq) - len(target) + 1):
            if seq[i:i + len(target)] == target:
                print("   旋律在轨 %-8s 位置 %d/%d" % (k, i, len(seq)))
                break
    res.append(m)
print(json.dumps([{"id": m["id"], "dur": m["duration"], "url": m["url"]} for m in res],
                 ensure_ascii=False, indent=1))
