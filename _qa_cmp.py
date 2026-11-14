# -*- coding: utf-8 -*-
"""对照：旧参数成片 vs 新参数成片的锐度/码率。"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2

VD = r"D:\local-multimodal-src\data\avatar2\videos"
old = os.path.join(VD, "lipsync_20261114_185444_aa0c.mp4")   # 旧 veryfast/crf20
new = os.path.join(VD, "lipsync_20261114_190718_1327.mp4")   # 新 medium/crf16

def stat(p, tag):
    if not os.path.isfile(p):
        print(tag, "缺失", p); return
    cap = cv2.VideoCapture(p)
    vals, i = [], 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % 10 == 0:
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            vals.append(cv2.Laplacian(g, cv2.CV_64F).var())
        i += 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    cap.release()
    sz = os.path.getsize(p)
    dur = i / fps
    print("%-12s 帧=%3d  时长=%4.2fs  体积=%6.0fKB  码率=%5.2fMbps  平均sharp=%5.2f"
          % (tag, i, dur, sz / 1024, sz * 8 / dur / 1e6, sum(vals) / max(1, len(vals))))

stat(old, "旧 crf20")
stat(new, "新 crf16")

# 再列一下本轮所有新出的片子
print("\n最近 5 个成片：")
for p in sorted(glob.glob(os.path.join(VD, "*.mp4")), key=os.path.getmtime)[-5:]:
    print("  %s  %.0fKB  %s" % (os.path.basename(p), os.path.getsize(p) / 1024,
                                __import__("time").strftime("%m-%d %H:%M", __import__("time").localtime(os.path.getmtime(p)))))
