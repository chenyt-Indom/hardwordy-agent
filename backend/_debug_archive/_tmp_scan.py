# -*- coding: utf-8 -*-
"""临时扫描工具：列出某个 .py 里所有 except 块的「块体长度 / 有没有留日志痕迹」。

用法：
    py backend/_tmp_scan.py iterloop.py

为什么留在仓库里：iterloop 的 silent-except 是上一轮唯一的扣分点，
    修完之后要能**随手复核**「还有没有 `except ...: pass`」。
    code_audit 只看 body 恰好是 `pass` 的那种；本脚本把 `return`/`continue`
    这种「没留任何痕迹的吞掉」也一并列出来，方便对照。
（不参与运行，只排查用。）
"""
import ast
import sys

_LOG_HINTS = ("log", "warn", "act", "print", "note", "record")


def scan(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        ln = node.lineno
        tail = lines[ln - 1] if 0 < ln <= len(lines) else ""
        names = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Attribute):
                    names.append(f.attr)
                elif isinstance(f, ast.Name):
                    names.append(f.id)
        logs = [x for x in names if any(k in x.lower() for k in _LOG_HINTS)]
        rows.append((ln, tail.strip(), len(node.body), logs,
                     type(node.body[0]).__name__))
    return rows


def main(argv):
    path = argv[1] if len(argv) > 1 else "iterloop.py"
    rows = scan(path)
    for ln, tail, n, logs, first in rows:
        print("%5d | %-52s | body=%d logs=%s | %s" % (ln, tail[:52], n, logs, first))
    bad = [r for r in rows if r[4] == "Pass"]
    print("\n共 %d 处 except，其中 `except ...: pass` %d 处" % (len(rows), len(bad)))
    for ln, tail, _n, _l, _f in bad:
        print("  pass-only: %d  %s" % (ln, tail))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
