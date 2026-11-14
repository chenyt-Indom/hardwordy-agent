"""验证工具产物（图片 / 文档）能否变成群附件。"""
import os, sys, base64, struct, zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from backend import main as M
from backend import hive

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1; print("  FAIL  %s %s" % (name, extra))


def _png_1x1() -> bytes:
    """造一张真 PNG（1x1 红点），避免依赖任何图片库。"""
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


print("1) 建临时群")
g = hive.create_group("产物验证群_自动删除", "测试机")
gid = g["gid"]
print("   gid =", gid)

print("2) 图片产物 → 群附件")
png = _png_1x1()
evs = [{"type": "image", "mime": "image/png",
        "b64": base64.b64encode(png).decode()}]
metas = M._hive_save_artifacts(gid, evs)
print("   返回：", [(m.get("name"), m.get("size"), m.get("kind")) for m in metas])
chk("产出 1 个附件", len(metas) == 1, str(len(metas)))
if metas:
    m = metas[0]
    chk("有 fid", bool(m.get("fid")), str(m.get("fid")))
    chk("kind=image", str(m.get("kind")) == "image", str(m.get("kind")))
    p = hive.file_path(gid, m["fid"])
    chk("文件真落盘", os.path.isfile(p), p)
    chk("字节一致", open(p, "rb").read() == png,
        "%d vs %d" % (os.path.getsize(p) if os.path.isfile(p) else -1, len(png)))

print("3) 文库产物 → 群附件")
from backend import doclib
w = doclib.write_file("_qa_probe.md", "# 测试\n\n这是一份验证文档。\n")
evs2 = [{"type": "library", "act": "write", "rel": w.get("rel") or "_qa_probe.md"}]
metas2 = M._hive_save_artifacts(gid, evs2)
print("   返回：", [(m.get("name"), m.get("size")) for m in metas2])
chk("产出 1 个文档附件", len(metas2) == 1, str(len(metas2)))
if metas2:
    chk("mime 是 markdown 或文本",
        "markdown" in str(metas2[0].get("mime")) or "text" in str(metas2[0].get("mime")),
        str(metas2[0].get("mime")))

print("4) 空/坏输入不炸")
chk("空列表", M._hive_save_artifacts(gid, []) == [])
chk("None", M._hive_save_artifacts(gid, None) == [])
chk("坏 b64 不抛", isinstance(M._hive_save_artifacts(
    gid, [{"type": "image", "b64": "!!!notbase64!!!"}]), list))

print("5) 清理")
try:
    doclib.delete_file("_qa_probe.md")
except Exception:
    pass
hive.delete_group(gid)
print("   已删群 + 删探针文档")

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))
