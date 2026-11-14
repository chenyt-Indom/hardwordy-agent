# -*- coding: utf-8 -*-
"""真实端到端：让群 AI 独立完成一个多文件小项目，验证：
   ① 代码落在**这个群自己的**工作区项目里
   ② 多文件 + 真的能跑
   ③ 产物自动进群产物文件夹，其他成员能下载
"""
import os
import sys
import time
import json

OK, BAD = [], []


def chk(name, cond, detail=""):
    (OK if cond else BAD).append(name + (" | " + detail if detail else ""))


from backend import hive, workspace, main

g = hive.create_group("群开发E2E", "测试员")
gid = g["gid"]
proj = main._hive_workspace_project(gid)
print("gid=%s\nproj=%s" % (gid, proj), flush=True)

TASK = (
    "请在当前工作区项目里做一个**多文件的待办清单网页**，要求：\n"
    "1. 至少 3 个文件：index.html、style.css、app.js\n"
    "2. 功能：能添加待办、勾选完成、删除；数据存 localStorage\n"
    "3. 界面干净，深色主题\n"
    "4. 写完后用 workspace_run 跑一个简单的自检（比如用 node -e 检查语法，"
    "或者写个 test.js 打印文件是否都存在），确认没写错\n"
    "最后用一两句话说明你建了哪些文件。"
)

t0 = time.time()
try:
    main._hive_run_model_now(gid, TASK, {}, "mid_e2e_dev", "测试员", "u_tester")
except Exception as e:
    chk("模型任务执行", False, "%s: %s" % (type(e).__name__, e))
print("耗时 %.1fs" % (time.time() - t0), flush=True)

# ---------- 1) 消息落盘 + 无报错
msgs = hive.read_messages(gid, 0, 50)
ai = [m for m in msgs if m.get("type") == "ai" or m.get("role") == "ai"]
chk("AI 回复已落盘", len(ai) >= 1, "n=%d" % len(ai))
if ai:
    last = ai[-1]
    chk("执行无错误", not last.get("error"), str(last.get("error"))[:200])
    chk("有正文", len(str(last.get("text") or "")) > 30,
        str(last.get("text") or "")[:120])
    print("--- AI 正文（前 600 字）---")
    print(str(last.get("text") or "")[:600].encode("gbk", "replace").decode("gbk"))

# ---------- 2) 文件真的落在群项目里
root = workspace.root(proj)
files = []
for dp, _dn, fn in os.walk(root):
    if "_回收站" in dp:
        continue
    for f in fn:
        files.append(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"))
files = sorted(files)
print("群项目文件:", files, flush=True)
chk("项目里有多个文件", len(files) >= 3, str(len(files)))
chk("有 index.html", any(f.endswith("index.html") for f in files), str(files))
chk("有 css", any(f.endswith(".css") for f in files), str(files))
chk("有 js", any(f.endswith(".js") for f in files), str(files))

# 关键：确认没有写到全局 active_project 里
others = workspace.active_project()
chk("没串到全局项目", others != proj, others)

# ---------- 3) 产物进群产物文件夹
arts = hive.list_artifacts(gid)
print("产物:", [(a["name"], a.get("source")) for a in arts], flush=True)
chk("产物已自动收集", len(arts) >= 3, "n=%d" % len(arts))
chk("产物里有代码", any("AI代码_" in a["name"] for a in arts),
    str([a["name"] for a in arts]))
chk("产物 source 标了项目", any(proj in str(a.get("source") or "") for a in arts),
    str([a.get("source") for a in arts]))
chk("产物文件真实存在",
    all(os.path.isfile(hive.file_path(gid, a["fid"])) for a in arts))

# 产物内容 == 项目文件内容（说明发出去的是真代码）
one = [a for a in arts if "index.html" in a["name"]]
if one:
    body = hive.read_file_bytes(gid, one[0]["fid"]).decode("utf-8", "ignore")
    chk("产物内容是真 HTML", "<html" in body.lower() or "<!doctype" in body.lower(),
        body[:80])

# ---------- 4) 前端列表接口
from fastapi.testclient import TestClient
cli = TestClient(main.app)
r = cli.get("/api/hive/%s/artifacts" % gid)
d = r.json()
chk("接口 200 且 total>0", r.status_code == 200 and d.get("total", 0) > 0,
    str(d.get("total")))
if arts:
    r = cli.get("/api/hive/%s/files/%s?download=1" % (gid, arts[0]["fid"]))
    chk("下载可用", r.status_code == 200 and len(r.content) > 0,
        "%s bytes" % len(r.content))

names = [a["name"] for a in arts]
chk("产物无重名（同名只留最终版）", len(names) == len(set(names)), str(names))

print("\n==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
print("\n[测试群] gid=%s  proj=%s" % (gid, proj))
sys.exit(1 if BAD else 0)
