# -*- coding: utf-8 -*-
"""临时探针（下划线开头，不算护栏，跑完就删）：看 test_anchor_diagnose 里的 except 处理。"""
import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

p = os.path.join(ROOT, "backend", "_feature_tests", "test_anchor_diagnose.py")
src = open(p, encoding="utf-8", errors="replace").read()
lines = src.splitlines()
print("总行数:", len(lines))
tree = ast.parse(src, filename=p)
for node in ast.walk(tree):
    if not isinstance(node, ast.ExceptHandler):
        continue
    ln = node.lineno or 0
    body = node.body
    silent = len(body) == 1 and isinstance(body[0], ast.Pass)
    tail = lines[ln - 1] if 0 < ln <= len(lines) else ""
    print("ln=%d silent=%s noqa=%s | %s" % (ln, silent, "noqa" in tail.lower(),
                                            tail.strip()[:60]))
