# -*- coding: utf-8 -*-
"""C 盘满了 —— 只做**统计**，不删任何东西。看清占用再决定。"""
import os
import time
import shutil

td = r"C:\Users\19853\AppData\Local\Temp"
now = time.time()

try:
    u = shutil.disk_usage("C:\\")
    print(f"C 盘：剩余 {u.free/1048576:.1f} MB / 共 {u.total/1073741824:.2f} GB")
except Exception as e:
    print("disk_usage 失败:", e)

total = cnt = 0
old_total = old_cnt = 0
big = []
errs = 0
try:
    names = os.listdir(td)
except Exception as e:
    print("列目录失败:", e)
    names = []

for name in names:
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
        total += sz
        cnt += 1
        if now - mtime > 7 * 86400:
            old_total += sz
            old_cnt += 1
        if sz > 50 * 1048576:
            big.append((sz, name, int((now - mtime) / 86400)))
    except Exception:
        errs += 1

print(f"\nTemp 总大小 {total/1073741824:.2f} GB，{cnt} 项（{errs} 项读不到）")
print(f"7 天以上未改动：{old_total/1073741824:.2f} GB，{old_cnt} 项")
print("\n超过 50MB 的（前 15）：")
for sz, n, d in sorted(big, reverse=True)[:15]:
    print(f"  {sz/1048576:8.1f} MB  {d:4d}天前  {n[:70]}")
