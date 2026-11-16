
import sys, os, json, time
sys.path.insert(0, r"D:\local-multimodal-src")
os.chdir(r"D:\local-multimodal-src")
from backend import self_iterate as si, iter_runner as ir

out = {}
probe = r"D:\local-multimodal-src\backend\_probe_featradar.py"
plan = si.create_plan(
    summary="链路验证B2：新建文件类动作能否真执行",
    actions=[{
        "title": "新建探针文件",
        "detail": "在 backend/ 目录下新建文件 backend/_probe_featradar.py，内容只有一行：FEAT_PROBE = 1\n不要改任何其他文件。",
        "risk": "low", "files": [],
        "status": "approved", "state": "approved",
        "note": "只新建这一个文件", "origin": "feature",
    }],
    trigger="featradar",
)
pid = plan["plan"]["id"]
out["pid"] = pid

# 先看 _plan_patch_for_action 能不能认出目标
try:
    p, err = si._plan_patch_for_action(plan["plan"], plan["plan"]["actions"][0])
    out["plan_patch_error"] = err
    out["patch_generated"] = bool(p)
    out["patch_preview"] = json.dumps(p, ensure_ascii=False)[:400] if p else None
except Exception as e:
    out["plan_patch_error"] = "%s: %s" % (type(e).__name__, e)

sub = ir.submit(pid, source="featradar", title="链路验证B2")
out["submit"] = sub

t0 = time.time()
last = None
while time.time() - t0 < 300:
    time.sleep(3)
    d2, pl2 = si._find_plan(pid)
    acts = (pl2 or {}).get("actions") or []
    if not acts: break
    last = [(a.get("status"), str(a.get("error") or a.get("result") or "")[:220]) for a in acts]
    if all(s[0] not in ("pending", "approved") for s in last):
        break
out["elapsed"] = round(time.time() - t0, 1)
out["final_status"] = last
out["probe_exists"] = os.path.exists(probe)
out["probe_content"] = open(probe, encoding="utf-8").read()[:200] if os.path.exists(probe) else None
json.dump(out, open(r"D:\local-multimodal-src\data\_verify_b2_result.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
