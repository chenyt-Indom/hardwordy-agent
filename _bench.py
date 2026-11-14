# -*- coding: utf-8 -*-
"""单帧耗时基准：定位出片慢在哪一步"""
import sys, os, glob, subprocess, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar_lipsync as AL, avatar2 as A2, wav2lip_engine as W2L

OUT = "_qa_out"
os.makedirs(OUT, exist_ok=True)
mp3 = os.path.join(OUT, "s.mp3")
TEXT = "这是修复后的效果。"
if os.path.isfile(mp3):
    os.remove(mp3)
async def gen():
    import edge_tts
    c = edge_tts.Communicate(TEXT, "zh-CN-XiaoxiaoNeural")
    await c.save(mp3)
asyncio.run(gen())
wav = os.path.join(OUT, "s.wav")
subprocess.run([A2.ffmpeg(), "-y", "-loglevel", "error", "-i", mp3, "-ar", "16000", "-ac", "1", wav], check=False)
sr, y = A2._read_wav(wav)
print("音频 %.2f 秒" % (len(y)/sr))

img = cv2.imread("data/avatar2/avatar.png")
eng = AL._engine()

t = time.time(); mel = W2L.wav_to_mel(y.astype(np.float32)); print("mel %.3fs" % (time.time()-t))

# 单帧推理
t = time.time()
frames, meta = eng.render(img, y.astype(np.float32))
print("render 全片 %d 帧 %.2fs  (%.1f ms/帧)" % (len(frames), time.time()-t, 1000*(time.time()-t)/max(1, len(frames))))

# 拆开测 _composite
box = meta["box"]
print("box", box)

# 单帧 match_light
x1, y1, x2, y2 = box
w, h = x2-x1, y2-y1
fake96 = cv2.resize(img[y1:y2, x1:x2], (96, 96))
gen_big = cv2.resize(fake96, (w, h), interpolation=cv2.INTER_CUBIC)
orig_big = img[y1:y2, x1:x2]
roi = np.ones((h, w), bool)
t = time.time()
for _ in range(10):
    _ = eng._match_light(gen_big, orig_big, mask_keep=roi.astype(np.float32))
print("_match_light %.1f ms/次" % (100*(time.time()-t)))

t = time.time()
for _ in range(10):
    _ = eng._detail_inject(gen_big, orig_big, 0.5)
print("_detail_inject %.1f ms/次" % (100*(time.time()-t)))

t = time.time()
for _ in range(10):
    _ = eng._composite(img, fake96, box, fake96, openness=0.8)
print("_composite 合计 %.1f ms/次" % (100*(time.time()-t)))

# 动作层
from backend import avatar25d as A25
prep = A25._prep3d("data/avatar2/avatar.png", max_side=1024)
t = time.time()
for _ in range(10):
    _ = A25._warp(frames[0], prep, yaw=5.0, pitch=3.0, roll=1.0, iters=1)
print("_warp %.1f ms/次" % (100*(time.time()-t)))
