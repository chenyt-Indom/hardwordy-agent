# -*- coding: utf-8 -*-
"""看群聊工具表里到底有哪些"能写代码/能跑"的工具。"""
from backend import main, config

cfg = config.load_config() or {}
sch = main._hive_tool_schemas(cfg)
names = []
for s in sch:
    fn = (s.get("function") or {}) if isinstance(s, dict) else {}
    n = fn.get("name") or ""
    if n:
        names.append(n)
print("工具总数:", len(names))
ws = [n for n in names if "workspace" in n or "selfdev" in n]
print("workspace/selfdev 工具 (%d):" % len(ws))
for n in sorted(ws):
    print("   ", n)
code = [n for n in names if n in ("write_code", "run_python", "read_file",
                                  "write_file", "list_directory", "search_files")]
print("文件/执行类:", sorted(code))
print("ask_user 是否被剔除:", "ask_user" not in names)
