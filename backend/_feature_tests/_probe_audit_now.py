# -*- coding: utf-8 -*-
"""临时探针（下划线开头，不算护栏，跑完就删）：看当前自检报了哪些扣分点。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from backend import code_audit as CA  # noqa: E402

r = CA.audit()
print("files=%s total=%s counts=%s scored=%s"
      % (r.get("files"), r.get("total"), r.get("counts"), r.get("counts_scored")))
for f in r.get("findings") or []:
    print("[%s] %s:%s %s | %s" % (f.get("level"), f.get("file"), f.get("line"),
                                  f.get("kind"), str(f.get("msg"))[:70]))
