# -*- coding: utf-8 -*-
"""临时探针（跑完就删）：直接执行「盯梢小助手」的验收断言文件。"""
import glob
import importlib.util
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

cands = glob.glob(os.path.join(_HERE, "_feature_tests", "_pending_*XX*"))
assert cands, "没找到验收断言文件"
p = cands[0]
print("loading:", os.path.basename(p))
spec = importlib.util.spec_from_file_location("_probe_accept", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    mod.run()
    print("ACCEPTANCE OK")
except Exception:
    traceback.print_exc()
    print("ACCEPTANCE FAILED")
    sys.exit(1)
