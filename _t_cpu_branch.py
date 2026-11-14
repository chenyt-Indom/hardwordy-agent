"""模拟低配机（无显卡）：验证 close_mouth(auto) 不会硬跑 SDXL，而是走确定性。"""
import os, sys, json, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import avatar2, t2i

# 伪造：没有显卡
_orig = t2i.device_info
t2i.device_info = lambda: {"device": "cpu", "kind": "cpu", "gpu": None,
                           "cuda_available": False, "note": "模拟低配机",
                           "reason": "模拟：没有可用显卡", "forced": "auto", "can_gpu": False}

plan = avatar2.close_plan()
print("模拟 CPU -> tier=%s method=%s gen_ok=%s" % (plan["tier"], plan["method"], plan["gen_ok"]))
print("why:", plan["why"][:120])

# 关键：auto 模式下不能去碰 SDXL
t0 = time.time()
r = avatar2.close_mouth(preview=True, method="auto")
dt = time.time() - t0
print("\nclose_mouth(auto) on CPU -> %.1fs" % dt)
print("method:", r.get("method"))
print("reason:", (r.get("reason") or "")[:200])
print("plan.tier:", (r.get("plan") or {}).get("tier"))

# 断言：必须是确定性方法，且耗时远小于扩散
ok = r.get("method") in ("lip_shift_fallback", "lip_shift", "lip_mirror")
print("\n[CHECK] 走确定性方法:", ok)
print("[CHECK] 没有硬跑扩散（<10s）:", dt < 10)

t2i.device_info = _orig
