# -*- coding: utf-8 -*-
"""验证：后入群的人也能看到产物列表（本机没索引、没本体）。"""
import os
import sys
import json

OK, BAD = [], []


def chk(n, c, d=""):
    (OK if c else BAD).append(n + (" | " + d if d else ""))


from backend import hive, main

g = hive.create_group("后入群测试", "测试员")
gid = g["gid"]

# 造 2 个产物，并像真的一样挂到一条 AI 消息上
m1 = hive.save_file(gid, "AI代码_群-x__index.html", b"<html>1</html>",
                    "text/html", tag="ai", source="项目 群-x")
m2 = hive.save_file(gid, "AI图片_1.png", b"\x89PNGxx", "image/png",
                    tag="ai", source="画图")
hive.append_message(gid, {"from": "ai", "name": "hd", "type": "ai",
                          "text": "做好了", "files": [m1, m2]}) \
    if hasattr(hive, "append_message") else None

# 确认落盘消息里带 files（用项目自己的发消息接口）
try:
    main._hive_post(gid, "做好了", "ai", {"files": [m1, m2], "by": "hd"})
except Exception as e:
    print("post warn:", e)

arts = hive.list_artifacts(gid)
chk("本机有本体时 local=True", len(arts) == 2 and all(a["local"] for a in arts),
    str([(a["name"], a["local"]) for a in arts]))

# ---- 模拟「后入群」：清空索引 + 删掉文件本体，只留消息里的 files
idx_path = hive._files_index_path(gid)
hive._write_json(idx_path, {})
for f in os.listdir(hive.artifacts_dir(gid)):
    os.remove(os.path.join(hive.artifacts_dir(gid), f))

arts2 = hive.list_artifacts(gid)
chk("清空索引后仍能看到产物（来自历史消息）", len(arts2) == 2,
    str([a["name"] for a in arts2]))
chk("标记为本机没有", all(a["local"] is False for a in arts2),
    str([a["local"] for a in arts2]))
chk("名字保留", any("index.html" in a["name"] for a in arts2),
    str([a["name"] for a in arts2]))
chk("source 保留", any(a.get("source") == "项目 群-x" for a in arts2),
    str([a.get("source") for a in arts2]))

# 接口层也要能看到
from fastapi.testclient import TestClient
api = TestClient(main.app)
r = api.get("/api/hive/%s/artifacts" % gid)
d = r.json()
chk("接口 total=2", d.get("total") == 2, str(d.get("total")))
chk("接口里 local 为 False",
    all(x["local"] is False for x in d.get("artifacts") or []),
    str([x.get("local") for x in d.get("artifacts") or []]))

import shutil
shutil.rmtree(hive._gdir(gid), ignore_errors=True)

print("==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
    sys.exit(1)
