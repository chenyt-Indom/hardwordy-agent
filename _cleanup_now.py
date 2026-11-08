# -*- coding: utf-8 -*-
"""清理本轮排查用的临时脚本。"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
NAMES = [
    "_qa_tail_drop.py",
    "_fix_tail_drop.py",
    "_qa_tail_fix.py",
    "_qa_tail_fix.js",
    "_qa_tail_fix.out.txt",
    "_fix_ensure_ascii.py",
]
for n in NAMES:
    p = os.path.join(ROOT, n)
    if os.path.exists(p):
        try:
            os.remove(p)
            print("[del]", n)
        except Exception as e:
            print("[fail]", n, e)
    else:
        print("[skip]", n)
