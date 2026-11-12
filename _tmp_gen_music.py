# -*- coding: utf-8 -*-
"""临时脚本：直接调用后端音乐引擎生成两首，验证可用性。"""
import json
import backend.music as music

out = []
for style, bars, title in (("pop", 32, "轻快流行"), ("gufeng", 32, "古风五声")):
    try:
        r = music.generate(style=style, bars=bars, vocals=True, title=title)
        out.append({"style": style, "ok": True, "result": r})
    except Exception as e:  # noqa: BLE001
        out.append({"style": style, "ok": False, "err": f"{type(e).__name__}: {e}"})

print(json.dumps(out, ensure_ascii=False, default=str, indent=2)[:3000])
