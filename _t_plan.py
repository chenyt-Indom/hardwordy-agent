"""验证 close_plan 分级 + close_mouth 的 auto 语义是否按设备走。"""
import os, sys, json, py_compile, traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

for f in ["backend/avatar2.py"]:
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
        print("compile OK", f)
    except Exception as e:
        print("compile FAIL", f, e); raise

from backend import avatar2

plan = avatar2.close_plan()
print("\nclose_plan():")
print(json.dumps(plan, ensure_ascii=False, indent=1))

info = avatar2.info()
print("\ninfo().close_plan.tier =", (info.get("close_plan") or {}).get("tier"))
print("info() has close_plan:", "close_plan" in info)

# 真实跑一次 preview（本机是 GPU，应走 sdxl_hires）
import time
t0 = time.time()
r = avatar2.close_mouth(preview=True, method="auto")
print("\nclose_mouth(auto) -> %.1fs" % (time.time() - t0))
print(json.dumps({k: v for k, v in r.items() if k != "plan"}, ensure_ascii=False, indent=1))
print("plan in result:", bool(r.get("plan")), (r.get("plan") or {}).get("tier"))
