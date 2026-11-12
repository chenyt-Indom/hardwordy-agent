# -*- coding: utf-8 -*-
"""临时脚本：检查刚生成的两首音频的时长/峰值/RMS/声道。"""
import os
import numpy as np
from scipy.io import wavfile

for f in ("pop_20261112_231406_1344.wav", "gufeng_20261112_231409_2342.wav"):
    p = os.path.join("data", "music", f)
    sr, x = wavfile.read(p)
    x = x.astype(np.float32)
    if x.ndim > 1:
        ch = x.shape[1]
        mono = x.mean(axis=1)
        lr = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1]) if ch >= 2 else 1.0
    else:
        ch, mono, lr = 1, x, 1.0
    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(mono ** 2)))
    dur = len(mono) / sr
    # 低频占比（<200Hz 能量）
    sp = np.abs(np.fft.rfft(mono[: sr * 8])) ** 2
    fr = np.fft.rfftfreq(min(len(mono), sr * 8), 1 / sr)
    low = float(sp[fr < 200].sum() / max(sp.sum(), 1e-9) * 100)
    print(f"{f}\n  时长 {dur:.1f}s  采样率 {sr}  声道 {ch}  峰值 {peak:.2f}  RMS {rms:.3f}  低频 {low:.1f}%  声道相关 {lr:.2f}")
