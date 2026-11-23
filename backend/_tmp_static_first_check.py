# -*- coding: utf-8 -*-
"""手工验证：评分前静态预检能「先修掉新增扣分点，再评分」。"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mm_static_first_")
os.environ["MM_DATA_DIR"] = _TMP
sys.path.insert(0, r"D:\local-multimodal-src")

from backend import iter_runner as IR  # noqa: E402

REL = "backend/_tmp_static_probe.py"
src0 = open(os.path.join(r"D:\local-multimodal-src", REL.replace("/", os.sep)),
            encoding="utf-8").read()

# ① 有基线（基线里没有这条问题）→ 必须判成新增并修掉
IR.record_static_baseline(points=[])
info = IR.precheck_static("t1", {"files": [REL], "title": "probe"})
print("[1] added=%s fixed=%s left=%s files=%s base=%s"
      % (info["added_n"], info["fixed_n"], info["left_n"],
         info["fix_files"], info["baseline"]))
for p in info["added"]:
    print("    added:", p["file"], p["line"], p["kind"])

src1 = open(os.path.join(r"D:\local-multimodal-src", REL.replace("/", os.sep)),
            encoding="utf-8").read()
print("[1] 文件改了吗:", src0 != src1)
print("[1] 还有 pass 吗:", "pass" in src1)
assert info["added_n"] >= 1, "没识别出新增扣分点"
assert info["fixed_n"] >= 1, "没修掉新增扣分点"
_left_kinds = {p["kind"] for p in info["left"]}
assert "silent-except" not in _left_kinds, info["left"]

# ② 修完再跑一遍：同样的调用不该再报新增（基线已更新）
info2 = IR.precheck_static("t1", {"files": [REL], "title": "probe"})
print("[2] 第二轮 added=%s fixed=%s" % (info2["added_n"], info2["fixed_n"]))
assert info2["added_n"] == 0, info2["added"]

# ③ 没有基线时退化成「按本轮改过的文件判定」
os.remove(os.path.join(_TMP, "iterloop", "scoregate.json")) \
    if os.path.isfile(os.path.join(_TMP, "iterloop", "scoregate.json")) else None
IR.record_static_baseline = lambda *a, **k: {}          # 别再写快照
IR.load_static_baseline = lambda: {"keys": None, "source": "", "at": ""}
IR._rewrite_except_lines(REL, IR._AUTOFIX_KINDS)        # 先把文件恢复成坏的样子
with open(os.path.join(r"D:\local-multimodal-src", REL.replace("/", os.sep)),
          "w", encoding="utf-8") as fh:
    fh.write(src0)
info3 = IR.precheck_static("t1", {"files": [REL], "title": "probe"})
print("[3] 无基线 added=%s fixed=%s base=%s"
      % (info3["added_n"], info3["fixed_n"], info3["baseline"]))
assert info3["added_n"] >= 1 and info3["fixed_n"] >= 1, info3

print("OK")
