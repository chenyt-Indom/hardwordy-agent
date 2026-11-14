# -*- coding: utf-8 -*-
"""诊断椭圆补丁：清晰度断层 / 色差 / 边缘梯度"""
import sys, os, glob, wave
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import wav2lip_engine as W2L

img_path = "data/avatar2/avatar.png"
img = cv2.imread(img_path)
H, W = img.shape[:2]

eng = W2L.Wav2LipEngine(
    onnx_path=W2L._paths()["onnx"] if hasattr(W2L, "_paths") else None) if False else None
# 直接用引擎构造方式
import inspect
print(inspect.signature(W2L.Wav2LipEngine.__init__))
