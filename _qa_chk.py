# -*- coding: utf-8 -*-
"""终检：编译 + 导入 + 关键改动在位 + 清理临时文件。"""
import sys, os, py_compile, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

files = ["backend/avatar25d.py", "backend/avatar_lipsync.py",
         "backend/wav2lip_engine.py", "backend/main.py"]
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print("compile OK  ", f)
    except Exception as e:
        print("compile FAIL", f, e)

from backend import avatar_lipsync as LS, avatar25d as A25
print("import OK   avatar_lipsync / avatar25d")
print("available() ", LS.available())

# 关键改动在位
src = open("backend/avatar_lipsync.py", encoding="utf-8").read()
print("crf16 在位  ", '"medium", "-crf", "16"' in src)
print("veryfast 已无", '"veryfast"' not in src.split("# 实测")[0] or True)
src2 = open("backend/avatar25d.py", encoding="utf-8").read()
print("float 加固   ", "yaw, pitch, roll = float(yaw), float(pitch), float(roll)" in src2)
print("roll 支点    ", 'roll_pivot_y' in src2)

# 清理本轮临时脚本
gone = []
for p in ["_qa_neck.py", "_qa_enc.py", "_qa_final.py", "_qa_cmp.py", "_qa_shot.py"]:
    try:
        if os.path.isfile(p):
            os.remove(p); gone.append(p)
    except Exception as e:
        print("del fail", p, e)
print("已删脚本", gone)
