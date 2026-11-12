# -*- coding: utf-8 -*-
"""自检：情感预设 / 音高 / 音频库增删改。跑完会清掉测试文件。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend import tts  # noqa: E402

print("styles:", [s["id"] for s in tts.style_list()])

r = tts.synth("这是一段情感测试，用来确认音高和情感预设真的生效。",
              voice="zh-CN-XiaoxiaoNeural", rate=0, pitch=0, style="cheerful",
              filename="_chk_cheerful")
print("cheerful ok=%s name=%s bytes=%s rate=%s pitch=%s style=%s" % (
    r.get("ok"), r.get("name"), r.get("bytes"), r.get("rate"),
    r.get("pitch"), r.get("style_name")))
print("warnings:", r.get("warnings"))

r2 = tts.synth("这是一段低沉的测试，语速慢一点，音调低一点。",
               voice="zh-CN-YunjianNeural", style="sad", filename="_chk_sad")
print("sad ok=%s rate=%s pitch=%s bytes=%s" % (
    r2.get("ok"), r2.get("rate"), r2.get("pitch"), r2.get("bytes")))

lst = tts.list_audio()
print("list count:", len(lst), "first:", [(x["name"], x["bytes"]) for x in lst[:3]])

print("rename:", tts.rename_audio("_chk_sad.mp3", "_chk_sad_renamed"))
print("delete:", tts.delete_audio("_chk_sad_renamed.mp3"))
print("delete2:", tts.delete_audio("_chk_cheerful.mp3"))
print("after:", [x["name"] for x in tts.list_audio()][:3])
