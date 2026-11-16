
import sys, os, json, time
sys.path.insert(0, r"D:\local-multimodal-src")
os.chdir(r"D:\local-multimodal-src")
from backend import self_iterate as si, iter_runner as ir

out = {}
probe = r"D:\local-multimodal-src\backend\_probe_featradar.py"
plan = si.create_plan(
    summary="链路验证：featradar 来源的计划能否被执行器真执行",
    actions=[{
        "title": "新建探针文件",
        "detail": "在 backend/ 目录下新建文件 _probe_featradar.py，内容只有一行：FEAT_PROBE = 1\n不要改任何其他文件。",
        "risk": "low", "files": [],
        "status": "approved", "state": "approved",
        "note": "只新建这一个文件", "origin": "feature",
    }],
    trigger="featradar",
)
pid = plan["plan"]["id"]
out["pid"] = pid
d, pl = si._find_plan(pid)
out["approved_visible"] = len([a for a in (pl or {}).get("actions", []) if a.get("status") == "approved"])
sub = ir.submit(pid, source="featradar", title="链路验证")
out["submit"] = sub

t0 = time.time()
last = None
while time.time() - t0 < 300:
    time.sleep(3)
    d2, pl2 = si._find_plan(pid)
    acts = (pl2 or {}).get("actions") or []
    if not acts: break
    st = [(a.get("status"), str(a.get("error") or a.get("result") or "")[:200]) for a in acts]
    last = st
    if all(s[0] not in ("pending", "approved") for s in st):
        break
out["elapsed"] = round(time.time() - t0, 1)
out["final_status"] = last
out["probe_exists"] = os.path.exists(probe)
out["probe_content"] = open(probe, encoding="utf-8").read()[:200] if os.path.exists(probe) else None
json.dump(out, open(r"D:\local-multimodal-src\data\_verify_b_result.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
