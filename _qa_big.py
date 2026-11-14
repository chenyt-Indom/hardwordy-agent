# -*- coding: utf-8 -*-
"""复杂项目验证：5+ 文件、多关卡小游戏，看轮次放宽后能不能一次做完。"""
import os
import sys
import time

OK, BAD = [], []


def chk(n, c, d=""):
    (OK if c else BAD).append(n + (" | " + d if d else ""))


from backend import hive, workspace, main

g = hive.create_group("复杂项目E2E", "测试员")
gid = g["gid"]
proj = main._hive_workspace_project(gid)
print("gid=%s proj=%s" % (gid, proj), flush=True)

TASK = (
    "请在当前工作区项目里做一个**打砖块小游戏**，要求：\n"
    "1. 至少 5 个文件：index.html、style.css、game.js、levels.js、test.py\n"
    "2. 多关卡（levels.js 里定义至少 3 关，每关砖块布局不同）\n"
    "3. 功能：分数、生命、暂停/继续、重新开始、通关判定\n"
    "4. 用 Canvas 2D 绘制，深色主题\n"
    "5. 写完后用 workspace_run 跑 test.py 做自检（检查文件齐全、"
    "关键函数存在、括号配对、关卡数据合法），把结果给我看\n"
    "最后说明建了哪些文件、自检结果如何。"
)

t0 = time.time()
err = ""
try:
    main._hive_run_model_now(gid, TASK, {}, "mid_e2e_big", "测试员", "u_tester")
except Exception as e:
    err = "%s: %s" % (type(e).__name__, e)
cost = time.time() - t0
print("耗时 %.1fs  异常=%s" % (cost, err or "无"), flush=True)

root = workspace.root(proj)
files = []
for dp, _dn, fn in os.walk(root):
    if "_回收站" in dp:
        continue
    for f in fn:
        files.append(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"))
files = sorted(files)
print("文件:", files, flush=True)

chk("没抛异常", not err, err[:200])
chk("文件数 >= 5", len(files) >= 5, str(len(files)))
for need in ("index.html", "style.css", "game.js", "levels.js", "test.py"):
    chk("有 " + need, any(f.endswith(need) for f in files), str(files))

# 真跑自检
rc = None
try:
    import subprocess
    r = subprocess.run([sys.executable, "test.py"], cwd=root, capture_output=True,
                       text=True, timeout=120)
    rc = r.returncode
    print("test.py rc =", rc, flush=True)
    print((r.stdout or "")[-900:].encode("gbk", "replace").decode("gbk"), flush=True)
except Exception as e:
    print("跑 test.py 失败:", e)
chk("test.py 能跑通", rc == 0, str(rc))

# 内容规模
try:
    sz = {f: os.path.getsize(os.path.join(root, f)) for f in files}
    total = sum(sz.values())
    print("体积:", sz, flush=True)
    chk("总代码量 > 8KB", total > 8192, "%d bytes" % total)
    chk("game.js 有实质内容",
        os.path.getsize(os.path.join(root, "game.js")) > 2000
        if os.path.exists(os.path.join(root, "game.js")) else False)
    lv = os.path.join(root, "levels.js")
    if os.path.exists(lv):
        txt = open(lv, encoding="utf-8", errors="ignore").read()
        chk("levels.js 定义了多关", txt.count("level") >= 3 or txt.count("[") >= 3,
            "len=%d" % len(txt))
except Exception as e:
    chk("读体积", False, str(e))

# 产物
arts = hive.list_artifacts(gid)
print("产物:", [(a["name"], a.get("source")) for a in arts], flush=True)
chk("产物自动进文件夹", len(arts) >= 5, "n=%d" % len(arts))
chk("产物无重名", len({a["name"] for a in arts}) == len(arts),
    str([a["name"] for a in arts]))
chk("产物都是本机可读", all(a.get("local") for a in arts),
    str([a.get("local") for a in arts]))

# 轮次记录（从消息里看用了多少轮）
msgs = hive.read_messages(gid, 0, 20)
ai = [m for m in msgs if m.get("type") == "ai"]
if ai:
    chk("AI 有输出", len(str(ai[-1].get("text") or "")) > 50,
        str(ai[-1].get("text") or "")[:100])
    chk("执行无错误", not ai[-1].get("error"), str(ai[-1].get("error"))[:150])

print("\n==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
print("\n[测试群] gid=%s proj=%s" % (gid, proj))
sys.exit(1 if BAD else 0)
