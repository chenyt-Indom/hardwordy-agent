"""验证：低配机+无key 给出路；本机GPU+无key 自动改走本地。"""
import os, sys, time, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import avatar2, t2i

print("=== 4) 低配机 + 无 key ===")
_orig = t2i.device_info
t2i.device_info = lambda: {"device": "cpu", "kind": "cpu", "gpu": None,
                           "cuda_available": False, "note": "", "reason": "",
                           "forced": "auto", "can_gpu": False}
t0 = time.time()
r = avatar2.close_mouth(preview=True, method="cloud", image_key="")
print("%.1fs -> ok=%s method=%s" % (time.time() - t0, r.get("ok"), r.get("method")))
print("plan.best:", (r.get("plan") or {}).get("best"))
print("error:")
print((r.get("error") or "")[:600])
t2i.device_info = _orig

print("\n=== 5) 本机 GPU + 无 key（应自动改走本地 SDXL）===")
t0 = time.time()
r5 = avatar2.close_mouth(preview=True, method="cloud", image_key="")
print("%.1fs -> ok=%s method=%s" % (time.time() - t0, r5.get("ok"), r5.get("method")))
print("reason:", (r5.get("reason") or "")[:220])
print("cloud_skipped:", str((r5.get("detail") or {}).get("cloud_skipped", ""))[:150])
