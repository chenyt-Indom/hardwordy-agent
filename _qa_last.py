# -*- coding: utf-8 -*-
"""最后一步：接口自检 + 清理。"""
import sys, os, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from backend.main import app
c = TestClient(app)
r = c.get("/api/avatar_lipsync/info")
print("info", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])
print("路由总数", len(app.routes))

# 清理：本轮测试图，保留 enc_cmp.png 作对比
keep = {"enc_cmp.png"}
d = r"D:\local-multimodal-src\_qa_out"
n = 0
if os.path.isdir(d):
    for p in glob.glob(os.path.join(d, "*")):
        if os.path.basename(p) in keep:
            continue
        try:
            if os.path.isfile(p):
                os.remove(p); n += 1
        except Exception:
            pass
print("清理 _qa_out", n, "个；保留", sorted(keep))

for p in ["_qa_chk.py"]:
    try:
        os.remove(p); print("删除", p)
    except Exception as e:
        print("删除失败", p, e)
