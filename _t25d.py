"""伪3D 端到端验证：TTS → 渲染 → 抽帧检查。"""
import os, sys, time
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
from backend import avatar25d as A25
from backend import avatar2 as A2

print("available:", A25.available())
print("info:", A25.info())

img = A2._img_path()
print("形象图:", img, os.path.isfile(img))

# 准备测试语音
from backend import tts as tts_mod
TXT = "大家好，这是伪三D数字人的效果测试。我现在会轻微点头，也会眨眼睛。"
a = tts_mod.synth(TXT, voice="zh-CN-XiaoxiaoNeural", rate=0, engine="auto")
print("TTS:", a.get("ok"), a.get("engine"), a.get("path"))
wav = A2._to_wav16k(a.get("path"))
sr, y = A2._read_wav(wav)
print("wav %.2f 秒 sr=%d" % (len(y) / float(sr), sr))

words = A2._asr_words(wav)
print("ASR:", len(words), "字")

out = os.path.join(ROOT, "_p25d_test.mp4")
t0 = time.time()
r = A25.render(img, wav, out, words=words, amp=1.2, motion=1.0)
print("render:", {k: v for k, v in r.items() if k != "path"})
print("耗时 %.1f 秒" % (time.time() - t0))
