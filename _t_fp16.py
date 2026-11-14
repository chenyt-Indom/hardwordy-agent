"""实测：CPU + fp16 加载 SDXL inpaint 的内存门槛（只加载 + 单步，不跑整张）。

目的：确认低配机（16GB）有没有可能在 CPU 上跑 SDXL。
结论写 _t_fp16.json。
"""
import os, sys, json, time, traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "_t_fp16.json")
res = {"stages": []}


def mem():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"used_gb": round(vm.used / 1e9, 2), "avail_gb": round(vm.available / 1e9, 2)}
    except Exception:
        return {}


def log(stage, t0, **kw):
    d = {"stage": stage, "sec": round(time.time() - t0, 1)}
    d.update(kw); d["mem"] = mem()
    res["stages"].append(d)
    print(json.dumps(d, ensure_ascii=False), flush=True)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


try:
    import torch
    from diffusers import AutoPipelineForInpainting
    from backend import t2i
    res["base_mem"] = mem()

    d = t2i.inpaint_model_dir()
    for dt_name, dt in [("float16", torch.float16), ("bfloat16", torch.bfloat16)]:
        try:
            t0 = time.time()
            pipe = AutoPipelineForInpainting.from_pretrained(
                d, torch_dtype=dt, variant="fp16", local_files_only=True)
            log("load_" + dt_name, t0, ok=True)
            # 单步 256x256
            from PIL import Image
            import numpy as np
            side = 256
            img = Image.fromarray(np.full((side, side, 3), 200, np.uint8))
            pipe.enable_attention_slicing()
            t0 = time.time()
            pipe(prompt="closed mouth", image=img, mask_image=img,
                 num_inference_steps=2, strength=0.9, guidance_scale=7.5)
            log("infer_" + dt_name, t0, per_step=round((time.time() - t0) / 2, 2))
            del pipe
            import gc; gc.collect()
            log("free_" + dt_name, time.time())
        except Exception as e:
            log("fail_" + dt_name, time.time(), err="%s: %s" % (type(e).__name__, e))
    res["ok"] = True
except Exception as e:
    res["ok"] = False
    res["error"] = "%s: %s" % (type(e).__name__, e)
    res["tb"] = traceback.format_exc()[-1200:]
finally:
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
