import os, glob, time

ROOT = r"D:\local-multimodal-src"
removed = []

# 1) 临时脚本与对比图
for pat in ["_t_*.py", "_mouth_*.png", "_q_*.png"]:
    for p in glob.glob(os.path.join(ROOT, pat)):
        try:
            os.remove(p); removed.append(p)
        except Exception as e:
            print("skip", p, e)

# 2) 分辨率测试视频
vd = os.path.join(ROOT, "data", "avatar2", "videos")
for p in glob.glob(os.path.join(vd, "_res_*.mp4")):
    try:
        os.remove(p); removed.append(p)
    except Exception as e:
        print("skip", p, e)

# 3) 本轮接口测试生成的 3 段（17:20:3x ~ 17:20:4x），演示片保留
for name in ["lipsync_20261114_172030_752a.mp4",
             "lipsync_20261114_172035_1117.mp4",
             "lipsync_20261114_172040_f763.mp4"]:
    p = os.path.join(vd, name)
    if os.path.isfile(p):
        try:
            os.remove(p); removed.append(p)
        except Exception as e:
            print("skip", p, e)

# 4) 本轮 TTS 测试音频（17:13 之后生成的）
td = os.path.join(ROOT, "data", "tts")
if os.path.isdir(td):
    for p in glob.glob(os.path.join(td, "tts_20261114_17*.mp3")):
        try:
            if time.time() - os.path.getmtime(p) < 3600:
                os.remove(p); removed.append(p)
        except Exception as e:
            print("skip", p, e)

# 5) 缓存
for p in glob.glob(os.path.join(ROOT, "__pycache__", "_t_*")):
    try:
        os.remove(p); removed.append(p)
    except Exception:
        pass

print("removed %d:" % len(removed))
for p in removed:
    print("  ", os.path.relpath(p, ROOT))
print("videos left:", len(os.listdir(vd)))
