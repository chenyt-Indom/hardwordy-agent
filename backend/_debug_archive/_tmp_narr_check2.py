# -*- coding: utf-8 -*-
"""临时自测：音频时长 vs 渲染出的视频时长（跑完即删）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import avatar2  # noqa: E402

LOG = os.path.join(tempfile.gettempdir(), "narr_check2.txt")
lines = []
from PIL import Image  # noqa: E402
img = os.path.join(tempfile.gettempdir(), "narr_avatar_test.png")
Image.new("RGB", (512, 640), (60, 90, 140)).save(img)

T = ("这份方案讲的是一个把文档变成数字人讲解视频的功能，"
     "用户不用再自己读长文档，点一下就能听到讲解，"
     "并且按段落分批合成，避免一次性处理超长音频。")
a = avatar2.synthesize_segment(T)
lines.append("chars=%s dur=%s engine=%s path=%s" % (
    a.get("chars"), a.get("duration"), a.get("engine"), a.get("name")))
if a.get("ok"):
    p = a["path"]
    lines.append("header_probe=" + str(avatar2._probe_duration(p)))
    lines.append("decode=" + str(avatar2._audio_duration(p)))
    v = avatar2.warp_lipsync(img, p, out_name="_tmp_w2.mp4")
    lines.append("video=" + str(v.get("duration")) + " ok=" + str(v.get("ok")))
    if v.get("ok"):
        lines.append("video_decode=" + str(avatar2._audio_duration(v["path"])))
        os.remove(v["path"])
with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("ok")
