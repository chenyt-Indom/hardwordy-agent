# -*- coding: utf-8 -*-
"""临时自测 2：路由注册 + schema 暴露 + dispatch 真跑。跑完即删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import app  # noqa: E402
from backend import tools  # noqa: E402

print("=== 1. 路由 ===")
for r in app.routes:
    p = getattr(r, "path", "")
    if "/api/tts" in p:
        print("  ", p, sorted(getattr(r, "methods", []) or []))

print("=== 2. schema ===")
names = [s["function"]["name"] for s in tools.make_schemas()]
print("tts_speak 在默认 schema 里:", "tts_speak" in names, "| 总数:", len(names))

print("=== 3. dispatch 真跑（在线 · 云希） ===")
ev = []
out = tools.dispatch("tts_speak", {"text": "这是分发链路测试，念两句就停。",
                                   "voice": "zh-CN-YunxiNeural", "rate": 10}, ev, {})
print(out)
print("ui_events:", ev)

print("=== 4. 空文本 ===")
print(tools.dispatch("tts_speak", {"text": "   "}, [], {}))
