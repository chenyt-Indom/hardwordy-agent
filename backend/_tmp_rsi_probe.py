import ast
import sys

from backend import code_audit as CA

out = []
paths = list(CA._py_files())
out.append("n_paths=%d" % len(paths))
lp = [p for p in paths if p.name == "lessonloop.py"]
out.append("lessonloop_found=%s" % bool(lp))
if lp:
    p = lp[0]
    src = CA._read(p)
    try:
        tree = ast.parse(src, filename=str(p))
        names = CA._top_level_names(tree)
        out.append("rsi_speed_in_names=%s n_names=%d" % ("rsi_speed" in names, len(names)))
        out.append("has_speed_def=%s" % any(
            isinstance(n, ast.FunctionDef) and n.name == "rsi_speed" for n in tree.body))
    except SyntaxError as e:
        out.append("SYNTAX %s line %s" % (e.msg, e.lineno))

sym = {}
trees = {}
for p in paths:
    src = CA._read(p)
    if not src.strip():
        continue
    try:
        trees[CA._rel(p)] = (p, ast.parse(src, filename=str(p)))
    except SyntaxError:
        out.append("SKIP_SYNTAX %s" % p.name)
sym = CA._module_symbols(trees)
out.append("sym_lessonloop_has_speed=%s" % ("rsi_speed" in sym.get("lessonloop", set())))
out.append("sym_lessonloop_size=%d" % len(sym.get("lessonloop", set())))

fs = CA._check_cross_module(trees, sym)
out.append("cross_module_findings=%d" % len(fs))
for f in fs[:20]:
    out.append("%s %s:%s %s" % (f["level"], f["file"], f["line"], f["msg"]))

sys.stdout.write("\n".join(out) + "\n")
