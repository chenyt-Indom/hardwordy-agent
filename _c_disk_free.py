# -*- coding: utf-8 -*-
"""C 盘满了，导致 Python 工具无法写临时文件。

只清 **7 天以上未改动** 的 Temp 内容 —— 这是 Windows 临时目录，
里面的东西本身就是"随时可删"的语义，且长期未改动说明没有程序在用。
**不碰**：data/、用户文档、项目目录、正在运行的进程文件。
删失败（被占用）就跳过，不强行。
"""
import os
import time
import shutil

td = r"C:\Users\19853\AppData\Local\Temp"
now = time.time()
CUT = 7 * 86400

freed = 0
deleted = 0
skipped = 0


def rm_path(p, sz):
    global freed, deleted, skipped
    try:
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        freed += sz
        deleted += 1
    except Exception:
        skipped += 1


for name in os.listdir(td):
    p = os.path.join(td, name)
    try:
        if os.path.isdir(p):
            sz = 0
            for dp, _, fs in os.walk(p):
                for f in fs:
                    try:
                        sz += os.path.getsize(os.path.join(dp, f))
                    except Exception:
                        pass
            mtime = os.path.getmtime(p)
        else:
            sz = os.path.getsize(p)
            mtime = os.path.getmtime(p)
        if now - mtime > CUT:
            rm_path(p, sz)
    except Exception:
        skipped += 1

u = shutil.disk_usage("C:\\")
print(f"删除 {deleted} 项，跳过 {skipped} 项（被占用）")
print(f"释放 {freed/1048576:.1f} MB")
print(f"C 盘剩余 {u.free/1048576:.1f} MB / 共 {u.total/1073741824:.2f} GB")
