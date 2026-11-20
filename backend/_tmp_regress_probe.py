# -*- coding: utf-8 -*-
"""临时排查脚本：用 regress.run_disk 看子进程回归的真实失败原因。用完即删。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import regress  # noqa: E402

r = regress.run_disk(scope="quick", timeout=300)
print("ok:", r.get("ok"))
print("summary:", r.get("summary"))
print("failed:", json.dumps(r.get("failed_items"), ensure_ascii=False)[:1500])
