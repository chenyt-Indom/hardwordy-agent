
# -*- coding: utf-8 -*-
import sys, os, json, time
sys.path.insert(0, r"D:\local-multimodal-src")
os.chdir(r"D:\local-multimodal-src")
from backend import avatar2
import importlib
importlib.reload(avatar2)

# 约 520 字，模拟一段长文档
text = ("今天我们来聊聊数字人技术的基本原理。所谓数字人，就是把一张静态的人像照片，"
        "配合语音和口型驱动算法，生成一段会说话的视频。" * 8)
print("总字数", len(text), flush=True)

def split(t, maxn=260):
    import re
    sents = re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*", t)
    out, cur = [], ""
    for s in sents:
        if len(s) > maxn:
            if cur: out.append(cur); cur = ""
            for i in range(0, len(s), maxn): out.append(s[i:i+maxn])
        elif len(cur + s) <= maxn:
            cur += s
        else:
            if cur: out.append(cur)
            cur = s
    if cur: out.append(cur)
    return [x for x in out if x.strip()]

parts = split(text)
print("分段数", len(parts), [len(p) for p in parts], flush=True)
names = []
t0 = time.time()
for i, p in enumerate(parts):
    ts = time.time()
    r = avatar2.speak(p, lipsync="wav2lip", boost=1.0, expr=False)
    print("第%d段 ok=%s dur=%s 耗时=%.1fs %s" % (i+1, r.get("ok"), r.get("duration"),
          time.time()-ts, r.get("error","")), flush=True)
    if not r.get("ok"): 
        print("FAIL"); break
    names.append(r["name"])
if len(names) == len(parts):
    c = avatar2.concat_videos(names)
    print("合并 ok=%s parts=%s dur=%s bytes=%s" % (c.get("ok"), c.get("parts"),
          c.get("duration"), c.get("bytes")), flush=True)
    print("合并名", c.get("name"), flush=True)
    # 校验时长
    tot = sum(avatar2._probe_duration(os.path.join(avatar2._vdir(), n)) for n in names)
    got = avatar2._probe_duration(os.path.join(avatar2._vdir(), c["name"]))
    print("时长 段和=%.2f 合并=%.2f" % (tot, got), flush=True)
    json.dump({"names": names, "merged": c["name"]}, open(r"D:\local-multimodal-src\_e2e.json","w"))
print("总耗时 %.1fs" % (time.time()-t0), flush=True)
