import json
import sys

sys.path.insert(0, ".")
from backend import music

mid = "piano_20261112_231613_2487"
with open(music._score_path(mid), encoding="utf-8") as f:
    sc = json.load(f)

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pn(p):
    return "%s%d" % (NAMES[int(p) % 12], int(p) // 12 - 1)


target = ["G4", "A4", "C5", "A4", "G4", "E4", "G4"]
tr = sc["tracks"]["piano"]
seq = [pn(n["p"]) for n in tr]
print("piano 轨总音符:", len(seq))
print("前 30:", " ".join(seq[:30]))
print("后 40:", " ".join(seq[-40:]))

# 找目标旋律片段
hit = -1
for i in range(len(seq) - len(target) + 1):
    if seq[i:i + len(target)] == target:
        hit = i
        break
print("目标旋律 G4 A4 C5 A4 G4 E4 G4 命中位置:", hit)
if hit >= 0:
    print("该处音符:", " ".join(seq[hit:hit + 14]))
    print("该处时间:", [round(tr[hit + k]["t"], 2) for k in range(min(7, len(tr) - hit))])
else:
    # 找最长的匹配子串
    best = 0
    for i in range(len(seq)):
        for j in range(i + 1, min(i + 10, len(seq)) + 1):
            if seq[i:j] == target[:j - i]:
                best = max(best, j - i)
    print("最长匹配前缀长度:", best)
    print("旋律段可能位置（G4 开头）:", [i for i, s in enumerate(seq) if s == "G4"][:10])
