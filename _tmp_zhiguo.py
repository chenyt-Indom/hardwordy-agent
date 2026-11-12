import json
import sys

sys.path.insert(0, ".")
from backend import music

# 《我的祖国》主歌 + 副歌旋律骨架（C 调，4/4）
melody = (
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "      # 一条大河波浪宽
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 D4:1 E4:2 "  # 风吹稻花香两岸
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "      # 我家就在岸上住
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 A4:2 "      # 听惯了艄公的号子
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 E4:2 "      # 看惯了船上的白帆
    "G4:1 E4:1 C5:2 A4:1 A4:1 A4:2 "           # 这是美丽的祖国
    "E4:1 E4:1 G4:1 A4:1 A4:1 A4:3"            # 是我生长的地方
)

parsed = music.parse_notes(melody)
print("音符数:", len(parsed), "总拍:", round(sum(n["d"] for n in parsed), 2))

r = music.from_notes(melody, style="piano", bpm=68, vocals=False,
                     title="我的祖国（器乐版）")
print("ok:", r.get("ok"), "err:", r.get("error"))
print(json.dumps({k: v for k, v in r.items() if k not in ("notes", "score")},
                 ensure_ascii=False, indent=2)[:1200])
