# -*- coding: utf-8 -*-
"""临时脚本：跑 RSI 基准期间暂停后台自动迭代/自检，跑完可再切回。用完即删。"""
import sys
sys.path.insert(0, r".")
from backend import self_iterate as SI

print("暂停前 is_paused =", SI.is_paused())
print("toggle ->", SI.toggle_pause(True))
print("暂停后 is_paused =", SI.is_paused())
