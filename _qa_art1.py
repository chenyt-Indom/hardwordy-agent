# -*- coding: utf-8 -*-
"""验证：群产物目录、tag 落盘、list_artifacts、workspace 线程项目隔离。"""
import os
import sys
import py_compile

OK = []
BAD = []


def chk(name, cond, detail=""):
    (OK if cond else BAD).append(name + (" | " + detail if detail else ""))


# 1) 语法
for f in ("backend/main.py", "backend/hive.py", "backend/workspace.py"):
    try:
        py_compile.compile(f, doraise=True)
        chk("compile " + f, True)
    except Exception as e:
        chk("compile " + f, False, str(e)[:200])

# 2) 导入
try:
    from backend import hive, workspace, main  # noqa: F401
    chk("import backend.main", True)
except Exception as e:
    chk("import backend.main", False, "%s: %s" % (type(e).__name__, e))
    print("\n".join(OK))
    print("---- FAIL ----")
    print("\n".join(BAD))
    sys.exit(1)

# 3) 造一个临时群，验证 tag 落盘
g = hive.create_group("产物测试群", "测试员")
gid = g["gid"]
print("gid =", gid)

m1 = hive.save_file(gid, "用户上传.txt", b"hello-user", "text/plain")
m2 = hive.save_file(gid, "AI产出.png", b"\x89PNG-fake", "image/png",
                    tag="ai", source="画图")
chk("user tag", m1.get("tag") == "user", str(m1.get("tag")))
chk("ai tag", m2.get("tag") == "ai", str(m2.get("tag")))
chk("ai source", m2.get("source") == "画图", str(m2.get("source")))

p1 = hive.file_path(gid, m1["fid"])
p2 = hive.file_path(gid, m2["fid"])
chk("user 落在 files/", os.sep + "files" + os.sep in p1, p1)
chk("ai 落在 artifacts/", os.sep + "artifacts" + os.sep in p2, p2)
chk("ai 文件真实存在", os.path.isfile(p2), p2)

arts = hive.list_artifacts(gid)
chk("list_artifacts 只返回 ai", len(arts) == 1 and arts[0]["fid"] == m2["fid"],
    "n=%d" % len(arts))

# 4) 读回内容一致
chk("读回内容一致", hive.read_file_bytes(gid, m2["fid"]) == b"\x89PNG-fake")

# 5) workspace 线程项目隔离
workspace.set_active_project("主项目测试")
before = workspace.active_project()
workspace.set_thread_project("群-A-123456")
inside = workspace.active_project()
d = workspace.root("群-A-123456")
chk("线程内覆盖生效", inside == "群-A-123456", inside)
chk("覆盖项目目录已建", os.path.isdir(d), d)
workspace.set_thread_project("")
after = workspace.active_project()
chk("清除后回到全局", after == before, "%s -> %s" % (before, after))

# 6) 群项目名派生
proj = main._hive_workspace_project(gid)
chk("群项目名可读", proj.startswith("群-产物测试群-"), proj)
chk("群项目名合法", workspace.safe_project(proj) == proj)

# 清理测试群
try:
    import shutil
    shutil.rmtree(hive._gdir(gid), ignore_errors=True)
    shutil.rmtree(workspace.root("群-A-123456"), ignore_errors=True)
    chk("清理测试群", True)
except Exception as e:
    chk("清理测试群", False, str(e))

print("\n==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("\n==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
    sys.exit(1)
