# -*- coding: utf-8 -*-
"""临时探针：验证 _fix_landed_rows 的「复发倒扣」是否真的生效。用完即删。

为什么要有它：RSI 施工单 T1-2 说「fix_landed 目前只发不退」，
但 iterloop.py 第 515-519 行的注释说 2026-11-23 已改成「累计 + 复发自动倒扣」。
两者矛盾。如果不实测就照着 PDF 再加一个倒扣函数，会出现**双扣**（-3 → -6）。
"""
import sys, os, json, shutil
sys.path.insert(0, r".")
from backend import iterloop as IL

p = IL._ability_snap_path()
print("snapshot 路径:", p)
bak = p + ".probe_bak"
had = os.path.exists(p)
if had:
    shutil.copy2(p, bak)

try:
    if os.path.exists(p):
        os.remove(p)

    r1 = IL._fix_landed_rows({"all_names": ["t1", "t2"], "failed_names": ["t1"]})
    print("轮1 t1 红  -> score=%s detail=%s" % (r1 and r1["score"], r1 and r1["detail"]))

    r2 = IL._fix_landed_rows({"all_names": ["t1", "t2"], "failed_names": []})
    print("轮2 t1 翻绿-> score=%s detail=%s" % (r2 and r2["score"], r2 and r2["detail"]))

    r3 = IL._fix_landed_rows({"all_names": ["t1", "t2"], "failed_names": ["t1"]})
    print("轮3 t1 复发-> score=%s detail=%s" % (r3 and r3["score"], r3 and r3["detail"]))

    print("\n结论：复发后分数 3.0 -> %s（若为 0.0 则倒扣已生效）" % (r3 and r3["score"]))
finally:
    if os.path.exists(p):
        os.remove(p)
    if had:
        shutil.move(bak, p)
    print("已恢复原 snapshot")
