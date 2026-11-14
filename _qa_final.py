# -*- coding: utf-8 -*-
"""终检：真实出片 + 编码后清晰度对比（新参数 medium/crf16）。"""
import sys, os, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar_lipsync as LS

t0 = time.time()
r = LS.speak("大家好，这是编码参数优化之后的画面质量测试。",
             blink=False, head=True, max_side=1024, motion=1.0)
print("speak ok=%s  %.1fs" % (r.get("ok"), time.time() - t0))
if not r.get("ok"):
    print("ERR", r.get("error")); sys.exit(1)
print("  frames=%s  dur=%ss  size=%s  bytes=%d"
      % (r.get("frames"), r.get("duration"), r.get("size"), r.get("bytes")))
print("  path=%s" % r.get("path"))

# 解码首帧 / 中间帧，测锐度
cap = cv2.VideoCapture(r["path"])
vals = []
i = 0
while True:
    ok, f = cap.read()
    if not ok:
        break
    if i in (0, 20, 40, 60, 80, 100):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        vals.append((i, cv2.Laplacian(g, cv2.CV_64F).var()))
    i += 1
cap.release()
print("  解码帧数", i)
for idx, v in vals:
    print("   帧%-4d sharp=%.2f" % (idx, v))
if vals:
    print("  平均 sharp=%.2f" % (sum(v for _, v in vals) / len(vals)))
