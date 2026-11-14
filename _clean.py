# -*- coding: utf-8 -*-
"""清理本轮临时文件，并做最终核对。"""
import glob
import os
import shutil
import sys

root = r"D:\local-multimodal-src"
targets = []
for pat in ("_probe*.py", "_check*.py", "_dbg*.py", "_test_*.py",
            "_verify_*.py", "_measure*.py", "_clean*.py"):
    targets += glob.glob(os.path.join(root, pat))

removed = []
for f in sorted(set(targets)):
    if os.path.basename(f) == "_clean.py":
        continue
    try:
        os.remove(f)
        removed.append(os.path.basename(f))
    except Exception as e:
        print("删不掉 %s：%s" % (f, e))

# 临时测试数据
tmp = os.environ.get("TEMP", ".")
for d in glob.glob(os.path.join(tmp, "_hive_test_data_*")) + \
        glob.glob(os.path.join(tmp, "_hive_dbg_*")):
    try:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(os.path.basename(d) + "/")
    except Exception:
        pass

# 根目录残留 __pycache__
pc = os.path.join(root, "__pycache__")
if os.path.isdir(pc):
    shutil.rmtree(pc, ignore_errors=True)
    removed.append("__pycache__/")

print("清理 %d 项：%s" % (len(removed), ", ".join(removed)))

# 最终核对
sys.path.insert(0, root)
import py_compile
for f in ("backend/hive_relay_link.py", "backend/hive_client.py",
          "backend/main.py"):
    py_compile.compile(os.path.join(root, f), doraise=True)
print("三个文件编译通过")

import backend.main as M
import backend.hive_client as HC
print("路由 %d 条" % len(M.app.routes))
cfg = HC.relay_config()
print("中继配置：url=%s token_set=%s urls=%s"
      % (cfg.get("url"), bool(cfg.get("token")), cfg.get("urls")))

# 确认候选派生正常
import backend.hive_relay_link as R
cands = R.candidates_for(cfg.get("url"), cfg.get("urls"))
print("候选线路 %d 条" % len(cands))
print("常量：HB_EVERY=%s HB_IDLE=%s BACKOFF_MAX=%s CAND_COOLDOWN=%s/%s"
      % (R.HB_EVERY, R.HB_IDLE, R.BACKOFF_MAX, R.CAND_COOLDOWN,
         R.CAND_COOLDOWN_HARD))
