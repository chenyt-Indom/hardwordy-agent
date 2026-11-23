"""临时探针：列出所有 range(len(x) - 1) 形态（含被 _near_slide 抑制的），用完即删。"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_audit as CA  # noqa: E402

OUT = []
for p in CA._py_files():
    src = CA._read(p)
    if not src.strip():
        continue
    try:
        tree = ast.parse(src, filename=str(p))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "range"):
            continue
        if len(node.args) != 1:
            continue
        a = node.args[0]
        if (isinstance(a, ast.BinOp) and isinstance(a.op, ast.Sub)
                and isinstance(a.right, ast.Constant) and a.right.value == 1
                and isinstance(a.left, ast.Call)
                and getattr(a.left.func, "id", None) == "len"):
            lines = src.splitlines()
            ctx = "\n".join(lines[max(0, node.lineno - 3):node.lineno + 2])
            OUT.append("%s:%d  slide_suppressed=%s\n%s\n"
                       % (CA._rel(p), node.lineno,
                          CA._near_slide(p, node.lineno), ctx))

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_rlm.txt")
with io.open(p, "w", encoding="utf-8") as fh:
    fh.write("\n".join(OUT) if OUT else "(none)")
sys.stderr.write("WROTE %d\n" % len(OUT))
