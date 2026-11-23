# -*- coding: utf-8 -*-
"""临时脚本：恢复后台自动化（跑分期间暂停过）。用完可删。"""
import sys
sys.path.insert(0, r".")
from backend import self_iterate as SI

print("恢复前 is_paused =", SI.is_paused())
print("toggle ->", SI.toggle_pause(False).get("paused"))
print("恢复后 is_paused =", SI.is_paused())
