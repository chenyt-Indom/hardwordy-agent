# -*- coding: utf-8 -*-
"""端到端验证：群产物接口 + 群独立开发项目 + workspace 产物自动进文件夹。"""
import os
import sys
import json

OK, BAD = [], []


def chk(name, cond, detail=""):
    (OK if cond else BAD).append(name + (" | " + detail if detail else ""))


from fastapi.testclient import TestClient
from backend import hive, workspace, main

cli = TestClient(main.app)

# ---------- 1) 建群
r = cli.post("/api/hive/create", json={"name": "产物E2E测试群"})
d = r.json()
gid = d.get("gid") or (d.get("group") or {}).get("gid") or ""
chk("建群", bool(gid), json.dumps(d, ensure_ascii=False)[:200])
if not gid:
    print("\n".join(OK)); print("---- FAIL ----"); print("\n".join(BAD)); sys.exit(1)

# ---------- 2) 空产物列表
r = cli.get("/api/hive/%s/artifacts" % gid)
d = r.json()
chk("空列表 200", r.status_code == 200 and d.get("ok") is True)
chk("空列表 total=0", d.get("total") == 0, str(d.get("total")))
chk("返回真实目录", os.path.basename(d.get("dir") or "") == "artifacts", str(d.get("dir")))
chk("返回群项目名", str(d.get("project") or "").startswith("群-产物E2E测试群-"),
    str(d.get("project")))

# ---------- 3) 模拟一次 AI 工具产出（三类事件）
proj = main._hive_workspace_project(gid)
workspace.set_thread_project(proj)
try:
    workspace.write_text("index.html", "<h1>hi</h1>", by="ai")
    workspace.write_text("app.py", "print('ok')\n", by="ai")
    workspace.mkdir("static", by="ai") if False else None
finally:
    workspace.set_thread_project("")

evs = [
    {"type": "image", "mime": "image/png", "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="},
    {"type": "library", "act": "write", "rel": "测试报告.md"},
    {"type": "workspace", "act": "write", "rel": "index.html", "project": proj},
    {"type": "workspace", "act": "write", "rel": "app.py", "project": proj},
    {"type": "workspace", "act": "mkdir", "rel": "static", "project": proj},
]
# 先造一个文库文件
try:
    from backend import doclib
    doclib.write("测试报告.md", "# 测试\n内容\n") if hasattr(doclib, "write") else None
except Exception:
    pass

metas = main._hive_save_artifacts(gid, evs)
names = sorted(m["name"] for m in metas)
chk("产物数量 3（图片+代码x2，mkdir 不收）", len(metas) == 3, str(names))
chk("有图片", any(n.startswith("AI图片_") for n in names), str(names))
chk("有代码且带项目名", any("AI代码_" + proj in n for n in names), str(names))
chk("全部 tag=ai", all(m.get("tag") == "ai" for m in metas))

# ---------- 4) 列表接口能看到
r = cli.get("/api/hive/%s/artifacts" % gid)
d = r.json()
chk("列表 total=3", d.get("total") == 3, str(d.get("total")))
chk("列表按时间倒序", [m["ts"] for m in d["artifacts"]] ==
    sorted([m["ts"] for m in d["artifacts"]], reverse=True))
chk("字节数 > 0", int(d.get("bytes") or 0) > 0, str(d.get("bytes")))

# ---------- 5) 下载接口能拿到本体
fid = metas[0]["fid"]
r = cli.get("/api/hive/%s/files/%s?download=1" % (gid, fid))
chk("下载 200", r.status_code == 200, str(r.status_code))
chk("下载带 attachment", "attachment" in (r.headers.get("content-disposition") or ""),
    r.headers.get("content-disposition") or "")

# ---------- 6) 磁盘上真的是独立目录
ad = hive.artifacts_dir(gid)
fd = hive.files_dir(gid)
chk("artifacts 目录存在", os.path.isdir(ad), ad)
chk("用户上传不会进 artifacts", not os.path.isdir(fd) or
    len([x for x in os.listdir(fd)]) == 0, str(os.listdir(fd) if os.path.isdir(fd) else []))
chk("artifacts 里有文件", len(os.listdir(ad)) == 3, str(len(os.listdir(ad))))

# ---------- 7) 群项目隔离：两个群不串
g2 = hive.create_group("另一个群", "测试员")["gid"]
p1 = main._hive_workspace_project(gid)
p2 = main._hive_workspace_project(g2)
chk("两个群项目名不同", p1 != p2, "%s vs %s" % (p1, p2))
workspace.set_thread_project(p1)
a = workspace.active_project()
workspace.set_thread_project(p2)
b = workspace.active_project()
workspace.set_thread_project("")
chk("线程内各看各的项目", a == p1 and b == p2, "%s / %s" % (a, b))
chk("不覆盖时回到全局", workspace.active_project() not in (p1, p2),
    workspace.active_project())

# ---------- 8) 老数据兼容（无 tag 的 meta 仍能读）
m_old = hive.save_file(gid, "老附件.txt", b"old", "text/plain")
idx_path = hive._files_index_path(gid)
idx = hive._read_json(idx_path, {})
idx[m_old["fid"]].pop("tag", None)
hive._write_json(idx_path, idx)
chk("老数据（无 tag）仍可读",
    hive.read_file_bytes(gid, m_old["fid"]) == b"old")
chk("老数据不算产物", all(m["fid"] != m_old["fid"]
                          for m in hive.list_artifacts(gid)))

# ---------- 清理
import shutil
for g in (gid, g2):
    shutil.rmtree(hive._gdir(g), ignore_errors=True)
shutil.rmtree(workspace.root(p1), ignore_errors=True)
shutil.rmtree(workspace.root(p2), ignore_errors=True)

print("==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
    sys.exit(1)
