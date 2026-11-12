# -*- coding: utf-8 -*-
"""临时自测：后端导入 + 工具分发 + 路由注册。跑完即删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=== 1. 导入 backend.main（验证语法与依赖） ===")
from backend.main import app  # noqa: E402
print("import ok")

print("=== 2. 路由是否注册 ===")
paths = sorted({getattr(r, "path", "") for r in app.routes})
for p in paths:
    if "/api/tts" in p:
        print("  ", p)

print("=== 3. 工具是否在 schema 里 ===")
from backend import tools  # noqa: E402
names = [t["function"]["name"] for t in tools.TOOLS]
print("tts_speak in TOOLS:", "tts_speak" in names, "| 工具总数:", len(names))

print("=== 4. 通过 dispatch 真跑一次 ===")
ev = []
out = tools.dispatch("tts_speak", {"text": "这是一次分发链路测试，念两句就停。",
                                   "voice": "zh-CN-YunxiNeural", "rate": 10},
                     ev, {})
print(out)
print("ui_events:", ev)

print("=== 5. 空文本 ===")
print(tools.dispatch("tts_speak", {"text": "   "}, [], {}))
