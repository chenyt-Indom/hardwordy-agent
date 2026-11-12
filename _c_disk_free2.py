# -*- coding: utf-8 -*-
"""C 盘持续被写满。清 Temp 里的大块残留（>50MB）。

这些都是临时目录里的安装包 / 解压残留 / 性能分析文件，
语义上就是"随时可删"。删失败（被占用）就跳过。
**不动**项目数据、用户文档。
"""
import os
import time
import shutil

td = r"C:\Users\19853\AppData\Local\Temp"
now = time.time()

# 明确是临时产物的大块目录/文件
TARGETS = [
    "mprof4", "precprof", "tailprof",
    "edgechk6_bgqu3fdi", "edgechk5_62yvbm_u", "edgechk4_yxddfo4t",
    "edgechk3_99zrpt3w", "edgechk_hf4dl3xm",
    "360UnPackTmp", "RarSFX0", "Intel", "trae-cn-user-x64",
    "Legion Zone", "mm_light_shot",
    "LocalMultimodalAssistant-Windows-x64.zip",
]

freed = 0
for name in TARGETS:
    p = os.path.join(td, name)
    if not os.path.exists(p):
        print(f"[skip] {name} 不存在")
        continue
    try:
        if os.path.isdir(p):
            sz = 0
            for dp, _, fs in os.walk(p):
                for f in fs:
                    try:
                        sz += os.path.getsize(os.path.join(dp, f))
                    except Exception:
                        pass
            shutil.rmtree(p)
        else:
            sz = os.path.getsize(p)
            os.remove(p)
        freed += sz
        print(f"[del]  {name}  {sz/1048576:.1f}MB")
    except Exception as e:
        print(f"[fail] {name}: {type(e).__name__}")

u = shutil.disk_usage("C:\\")
print(f"\n释放 {freed/1048576:.1f} MB")
print(f"C 盘剩余 {u.free/1048576:.1f} MB / 共 {u.total/1073741824:.2f} GB")
