"""验证三条分支：GPU+无key / CPU+无key / CPU+有key。"""
import os, sys, json, py_compile, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
py_compile.compile(os.path.join(ROOT, "backend/avatar2.py"), doraise=True)
print("compile OK")

from backend import avatar2, t2i

print("\n=== 1) 本机真实（GPU）close_plan ===")
p = avatar2.close_plan()
print(json.dumps({k: p[k] for k in ("tier", "best", "gen_ok", "cloud_ok", "method", "eta")},
                 ensure_ascii=False))

print("\n=== 2) 模拟低配机（CPU），无 key ===")
_orig = t2i.device_info
t2i.device_info = lambda: {"device": "cpu", "kind": "cpu", "gpu": None,
                           "cuda_available": False, "note": "", "reason": "",
                           "forced": "auto", "can_gpu": False}
p2 = avatar2.close_plan("")
print(json.dumps({k: p2[k] for k in ("tier", "best", "gen_ok", "cloud_ok", "method", "eta")},
                 ensure_ascii=False))
print("why:", p2["why"])

print("\n=== 3) 模拟低配机（CPU），有 key ===")
p3 = avatar2.close_plan("sk-test-123")
print(json.dumps({k: p3[k] for k in ("tier", "best", "gen_ok", "cloud_ok", "method", "eta")},
                 ensure_ascii=False))
print("why:", p3["why"])

print("\n=== 4) 低配机 + 无 key 真调 close_mouth（应给明确出路，不卡死）===")
t0 = time.time()
r = avatar2.close_mouth(preview=True, method="cloud", image_key="")
print("%.1fs -> ok=%s method=%s" % (time.time() - t0, r.get("ok"), r.get("method")))
print("error:", (r.get("error") or "")[:400])
print("plan.best:", (r.get("plan") or {}).get("best"))

t2i.device_info = _orig
print("\n=== 5) 本机 GPU + 无 key（应自动改走本地 SDXL，不报错）===")
t0 = time.time()
r5 = avatar2.close_mouth(preview=True, method="cloud", image_key="")
print("%.1fs -> ok=%s method=%s" % (time.time() - t0, r5.get("ok"), r5.get("method")))
print("reason:", (r5.get("reason") or "")[:200])
print("cloud_skipped:", (r5.get("detail") or {}).get("cloud_skipped", "")[:120])
