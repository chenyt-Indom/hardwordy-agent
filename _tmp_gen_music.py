# -*- coding: utf-8 -*-
"""临时脚本：生成并只打印关键信息。"""
import json
import os
import backend.music as music

res = []
for style, title in (("pop", "轻快流行"), ("gufeng", "古风五声")):
    r = music.generate(style=style, bars=32, vocals=True, title=title)
    keys = {}
    if isinstance(r, dict):
        for k in ("ok", "id", "name", "file", "path", "title", "style",
                  "duration", "seconds", "bpm", "url", "audio"):
            if k in r:
                keys[k] = r[k]
    res.append({"style": style, "keys": keys})

print(json.dumps(res, ensure_ascii=False, indent=2))

d = os.path.join("data", "music")
if os.path.isdir(d):
    files = [(f, os.path.getsize(os.path.join(d, f))) for f in os.listdir(d)
             if f.lower().endswith(".wav")]
    files.sort(key=lambda x: -x[1])
    print("\n--- data/music 最近文件 ---")
    for f, s in files[:8]:
        print(f"{f}  {s/1024:.0f} KB")
