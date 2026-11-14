# -*- coding: utf-8 -*-
"""临时验证脚本：出 3 秒 3D 数字人视频并统计帧间差。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

import avatar3d as A3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    t0 = time.time()
    r = A3.render(
        os.path.join(ROOT, "data", "avatar2", "avatar.png"),
        os.path.join(ROOT, "data", "tts", "_test3d_voice.mp3.mp3"),
        os.path.join(ROOT, "data", "avatar2", "videos", "_test3d.mp4"),
        limit_sec=3.0, amp=1.4)
    print(r, "%.1fs" % (time.time() - t0))

    cap = cv2.VideoCapture(r["path"])
    prev = None
    diffs = []
    frames = []
    for _ in range(20000):
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    cap.release()
    print("frames %d  diff mean=%.2f max=%.2f" % (len(frames), np.mean(diffs), np.max(diffs)))
    sel = [frames[i] for i in [2, 18, 37, 55, 72] if i < len(frames)]
    out = os.path.join(ROOT, "data", "models", "face3d", "_final4.png")
    cv2.imencode(".png", np.hstack(sel))[1].tofile(out)
    print("saved", out)
