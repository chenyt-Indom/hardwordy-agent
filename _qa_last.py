import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from backend import main as M
from backend import hive
gs = hive.list_groups()
print("群数量：", len(gs))
for g in gs:
    print("  -", g.get("name"), g.get("gid"))
bad = [g for g in gs if "自动删除" in str(g.get("name") or "")]
print("测试残留：", bad or "（无）")
print("路由：", len([r for r in M.app.routes if hasattr(r, "path")]))
print("临时脚本残留：", [f for f in os.listdir(ROOT) if f.startswith("_qa_")] or "（无）")
