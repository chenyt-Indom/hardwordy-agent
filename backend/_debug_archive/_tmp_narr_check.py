# -*- coding: utf-8 -*-
"""临时自测脚本（跑完即删）：验证 narrate_doc / synthesize_segment / warp_lipsync。"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import avatar2  # noqa: E402

LOG = os.path.join(tempfile.gettempdir(), "narr_check.txt")
lines = []


def say(*a):
    lines.append(" ".join(str(x) for x in a))


def flush():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# 1) 造一张临时形象图（不写 data/，用 _IMG 指向临时绝对路径）
from PIL import Image  # noqa: E402
img = os.path.join(tempfile.gettempdir(), "narr_avatar_test.png")
Image.new("RGB", (512, 640), (60, 90, 140)).save(img)
avatar2._IMG = img
say("img:", avatar2._img_path(), os.path.isfile(avatar2._img_path()))

# 2) 切句 + 分段
text = "第一句话讲背景。第二句话讲方案！第三句话讲成本？第四句话讲风险；第五句话收尾。"
sents = avatar2.split_sentences(text)
segs = avatar2.plan_segments(sents, max_chars=20, max_sec=6)
say("sentences:", len(sents), "segments:", len(segs),
    "chars:", [s["chars"] for s in segs])

# 3) synthesize_segment：时长与字数近似成比例
t0 = time.time()
a1 = avatar2.synthesize_segment("这是一段很短的话。")
a2 = avatar2.synthesize_segment("这是一段比较长的话，用来验证音频时长是否和字数近似成比例，应该明显更长一些才对。")
say("tts1:", a1.get("ok"), a1.get("duration"), a1.get("chars"), a1.get("engine"))
say("tts2:", a2.get("ok"), a2.get("duration"), a2.get("chars"), a2.get("engine"))
if a1.get("ok") and a2.get("ok"):
    say("ratio sec/char:", round(a1["duration"] / a1["chars"], 3),
        round(a2["duration"] / a2["chars"], 3))
say("tts seconds:", round(time.time() - t0, 1))

# 4) warp_lipsync：出 mp4 且时长 ≈ 音频时长
if a2.get("ok"):
    v = avatar2.warp_lipsync(avatar2._img_path(), a2["path"],
                             out_name="_tmp_warp_test.mp4", engine="local")
    say("warp:", v.get("ok"), v.get("duration"), v.get("error"), v.get("bytes"))
    if v.get("ok"):
        say("warp exists:", os.path.isfile(v["path"]))
        os.remove(v["path"])

# 5) 空文档 → 明确错误
d = avatar2.narrate_doc("不存在的文档.md")
say("missing doc:", d.get("ok"), d.get("error"))

# 6) 真实文档 → narrate_doc（用短的 docx）
cands = [f["rel"] for f in __import__("backend.doclib", fromlist=["x"]).list_files()
         if f["rel"].endswith((".docx", ".pptx")) and not f["rel"].startswith("改前备份")]
t0 = time.time()
r = avatar2.narrate_doc(cands[0], max_chars=300, max_segments=2) if cands else {
    "ok": False, "error": "no doc"}
say("narrate:", r.get("ok"), r.get("error"), r.get("duration"),
    "sentences:", r.get("sentences"), "parts:", r.get("parts"),
    "path exists:", os.path.isfile(r.get("path") or ""))
say("segments:", json.dumps(
    [{k: s.get(k) for k in ("chars", "sentences", "audio_duration", "video_duration")}
     for s in (r.get("segments") or [])], ensure_ascii=False))
say("narrate seconds:", round(time.time() - t0, 1))
if r.get("ok"):
    lst = avatar2.list_narrations()
    say("list_narrations:", len(lst), lst[0]["name"] if lst else "")
    say("delete:", avatar2.delete_narration(r["name"]))
    say("paths valid:", avatar2.narration_path("../../etc/passwd"))

flush()
print("done")
