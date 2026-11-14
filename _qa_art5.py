# -*- coding: utf-8 -*-
"""验证：打包产物进群文件夹 + 之前的产物收集不回归。"""
import os
import sys
import json

OK, BAD = [], []


def chk(n, c, d=""):
    (OK if c else BAD).append(n + (" | " + d if d else ""))


from backend import hive, workspace, tools, main

g = hive.create_group("打包产物测试", "测试员")
gid = g["gid"]
proj = main._hive_workspace_project(gid)

workspace.set_thread_project(proj)
try:
    workspace.write_text("a.py", "print(1)\n", by="ai")
    workspace.write_text("b.html", "<b>x</b>", by="ai")
    evs = []
    out = tools.dispatch("workspace_pack", {}, evs, {"session": "hive:" + gid})
    print("pack out:", str(out)[:200], flush=True)
    chk("pack 执行成功", "打包好" in str(out), str(out)[:100])
    chk("pack 发了事件", any(e.get("act") == "pack" for e in evs),
        str([e.get("act") for e in evs]))
    chk("pack 事件带 path",
        any(str(e.get("path") or "").endswith(".zip") for e in evs),
        str([e.get("path") for e in evs]))

    # 把 pack 事件 + 两个 write 事件一起交给收集器
    all_evs = [{"type": "workspace", "act": "write", "rel": "a.py", "project": proj},
               {"type": "workspace", "act": "write", "rel": "b.html", "project": proj}]
    all_evs += evs
    metas = main._hive_save_artifacts(gid, all_evs)
    names = sorted(m["name"] for m in metas)
    print("产物:", names, flush=True)
    chk("产物数 3（2 代码 + 1 zip）", len(metas) == 3, str(names))
    chk("有 zip 产物", any(n.endswith(".zip") for n in names), str(names))
    zipm = [m for m in metas if m["name"].endswith(".zip")]
    if zipm:
        raw = hive.read_file_bytes(gid, zipm[0]["fid"])
        chk("zip 内容是真 zip", raw[:2] == b"PK", str(raw[:4]))
        chk("zip 有大小", len(raw) > 100, "%d bytes" % len(raw))
        chk("zip source 标了打包", "打包" in str(zipm[0].get("source")),
            str(zipm[0].get("source")))

    # 重复事件去重
    dup = main._hive_save_artifacts(gid, all_evs)
    chk("重复事件去重（同名同大小不再收）", len(dup) == 0, str(len(dup)))
finally:
    workspace.set_thread_project("")

# 回归：图片 / 文库仍能收
evs2 = [{"type": "image", "mime": "image/png",
         "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}]
m2 = main._hive_save_artifacts(gid, evs2)
chk("回归：图片仍能收", len(m2) == 1 and m2[0]["kind"] == "image", str(m2))

arts = hive.list_artifacts(gid)
chk("产物列表总数 4", len(arts) == 4, str(len(arts)))

import shutil
shutil.rmtree(hive._gdir(gid), ignore_errors=True)
shutil.rmtree(workspace.root(proj), ignore_errors=True)

print("==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
    sys.exit(1)
