# -*- coding: utf-8 -*-
"""清理：删除临时脚本自身。"""
import os

for f in ("_tmp_gen_music.py",):
    try:
        os.remove(f)
        print("removed", f)
    except Exception as e:  # noqa: BLE001
        print("skip", f, e)
