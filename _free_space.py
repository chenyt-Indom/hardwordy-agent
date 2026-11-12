# -*- coding: utf-8 -*-
"""磁盘满了，先做安全清理：只删可重建的缓存和本轮测试残留。

**不动**：data/、sessions/、image_library/、用户的 dist_v*（构建产物由用户决定）。
只清 __pycache__、临时测试脚本、以及我这几轮产生的 _bak_* 备份（保留最新一个）。
"""
import os
import shutil
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))
freed = 0


def dsize(p):
    n = 0
    for dp, _, fs in os.walk(p):
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass
    return n


def rm(p):
    global freed
    try:
        if os.path.isdir(p):
            sz = dsize(p)
            shutil.rmtree(p, ignore_errors=True)
            freed += sz
            print(f"[dir]  {os.path.relpath(p, ROOT)}  {sz/1048576:.1f}MB")
        elif os.path.exists(p):
            sz = os.path.getsize(p)
            os.remove(p)
            freed += sz
            print(f"[file] {os.path.relpath(p, ROOT)}  {sz/1024:.0f}KB")
    except Exception as e:
        print("[fail]", p, e)


# 1. 所有 __pycache__（可重建）
for p in glob.glob(os.path.join(ROOT, "**", "__pycache__"), recursive=True):
    rm(p)

# 2. 本轮验证的临时脚本
for n in ["_hive_verify.py", "_hive_http_verify.py", "_r2.py", "_r3.py", "_r4.py",
          "_r5.py", "_r6.py", "_r7.py", "_r8.py", "_r9.py", "_rf.py",
          "_render_tmp.py", "_cleanup_now.py", "_diag_tail.py", "_pack_backup.py",
          "_pack_chunk.py"]:
    rm(os.path.join(ROOT, n))

# 3. 我自己的备份目录（_bak_*），只留最新一个
baks = sorted(glob.glob(os.path.join(ROOT, "_bak_*")), key=os.path.getmtime)
for b in baks[:-1]:
    rm(b)

print(f"\n释放约 {freed/1048576:.1f} MB")
u = shutil.disk_usage(ROOT)
print(f"剩余 {u.free/1073741824:.2f} GB / 共 {u.total/1073741824:.2f} GB")
