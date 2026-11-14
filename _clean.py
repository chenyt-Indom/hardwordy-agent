"""清理本轮临时产物。"""
import os, shutil, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
removed = []

files = ["_diag_motion.py", "_diag_fix.py", "_diag_blink2.py", "_v_e2e.py",
         "_check.py", "_blink_cmp.png", "_blink_eyes.png"]
for f in files:
    p = os.path.join(ROOT, f)
    if os.path.isfile(p):
        os.remove(p); removed.append(f)

d = os.path.join(ROOT, "_v")
if os.path.isdir(d):
    shutil.rmtree(d, ignore_errors=True); removed.append("_v/")

for d in glob.glob(os.path.join(ROOT, "__pycache__")):
    shutil.rmtree(d, ignore_errors=True); removed.append("__pycache__/")

# 测试视频：只留最新那条给用户看，其余同前缀的旧测试片清掉
vdir = os.path.join(ROOT, "data", "avatar2", "videos")
keep = "lipsync_20261114_175413_6a78.mp4"
if os.path.isdir(vdir):
    allv = sorted(glob.glob(os.path.join(vdir, "*.mp4")),
                  key=os.path.getmtime, reverse=True)
    print("视频目录共 %d 个，最新 3 个：" % len(allv))
    for v in allv[:3]:
        print("   ", os.path.basename(v),
              "%.1fMB" % (os.path.getsize(v) / 1048576.0))

print("\n清理 %d 项：" % len(removed))
for r in removed:
    print("   -", r)

# 确认配置没被污染
import json
mp = os.path.join(ROOT, "data", "avatar2", "mouth.json")
if os.path.isfile(mp):
    print("\nmouth.json:", json.load(open(mp, encoding="utf-8")))
