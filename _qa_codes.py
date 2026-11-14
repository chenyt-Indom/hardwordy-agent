# -*- coding: utf-8 -*-
"""验证：AI 消息里的 codes（代码改动）能不能存下来、读回来、广播出去。

为什么要单独验：前端渲染依赖 m.codes 这个字段。如果 hive 存储层
只保留已知字段（或 extra 没被合并），那么"后进群的人回看历史"就看不到
代码卡片 —— 这正是用户抱怨的现象之一。这里直接走真实的 send_message /
read_messages，不 mock。
"""
import sys, os, json, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())

from backend import hive

g = hive.create_group("_qa_codes_test", owner_name="测试")
gid = g.get("gid") or g.get("id")
print("临时群:", gid, "name=", g.get("name"))

codes = [
    {"type": "selfdev", "act": "patch", "rel": "backend/hive_client.py",
     "added": 68, "removed": 0,
     "diff": "@@ -1,3 +1,5 @@\n-a\n+b\n+c", "who": "模型", "is_new": False},
    {"type": "workspace", "act": "write", "rel": "index.html",
     "project": "群-测试-abc123", "added": 42, "removed": 0,
     "diff": "+<html>\n+</html>", "who": "模型", "is_new": True},
]

msg = hive.send_message(gid, "这是正文", mtype="ai",
                        extra={"by": "测试", "model": "deepseek-flash",
                               "thinking": "想了一下", "dur_ms": 1234,
                               "codes": codes})
print("发出去的 msg 有 codes 吗:", "codes" in msg, "条数=", len(msg.get("codes") or []))

back = hive.read_messages(gid, 0, 20)
hit = [m for m in back if m.get("type") == "ai"]
print("读回来的 AI 消息条数:", len(hit))
if hit:
    m0 = hit[-1]
    got = m0.get("codes") or []
    print("读回来的 codes 条数:", len(got))
    for c in got:
        print("  -", c.get("type"), c.get("act"), c.get("rel") or c.get("project"),
              "+%s" % c.get("added"), "-%s" % c.get("removed"),
              "diff_len=%d" % len(str(c.get("diff") or "")))
    # 逐字段核对
    ok = (len(got) == 2
          and got[0].get("rel") == "backend/hive_client.py"
          and got[1].get("project") == "群-测试-abc123"
          and str(got[0].get("diff") or "").startswith("@@"))
    print("字段完整:", ok)
else:
    print("字段完整: False（没读到消息）")

# 清理
try:
    hive.delete_group(gid)
    print("已删除临时群")
except Exception as e:
    print("删群失败(可忽略):", e)
