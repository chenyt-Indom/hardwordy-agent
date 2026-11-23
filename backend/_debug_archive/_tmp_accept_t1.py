# -*- coding: utf-8 -*-
"""临时验收脚本：T1-1 / T1-2 验收 + ability_part 端到端。用完即删。"""
import sys, os, shutil
sys.path.insert(0, r".")
import backend.iterloop as m

print("T1-1 ABILITY_PER_FILE =", m.ABILITY_PER_FILE, "(expect 0.0)")
print("T1-2 hasattr(_fix_regressed_rows) =", hasattr(m, "_fix_regressed_rows"), "(expect True)")

p = m._ability_snap_path()
bak = p + ".acc_bak"
had = os.path.exists(p)
if had:
    shutil.copy2(p, bak)
try:
    out = m.ability_part(
        {"passed": 10, "failed": 2, "total": 12,
         "all_names": ["acc_a", "acc_b"], "failed_names": ["acc_a"]},
        {"smoke": [{"ok": True}], "symbols_ok": True, "py_files": 40})
    print("ability_part score =", out["score"])
    print("rows keys =", [r["key"] for r in out["rows"]])
    print("file_compile row score =",
          [r["score"] for r in out["rows"] if r["key"] == "file_compile"])
finally:
    if os.path.exists(p):
        os.remove(p)
    if had:
        shutil.move(bak, p)
print("snapshot restored:", os.path.exists(p) == had)
