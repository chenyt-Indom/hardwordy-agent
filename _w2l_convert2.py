"""转换 wav2lip.pth 与 4x_BigFace.pth 为 ONNX"""
import os
import sys
import importlib.util

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
import torch
import numpy as np

# ---------- 1. wav2lip.pth ----------
spec = importlib.util.spec_from_file_location("w2l_modeldef", os.path.join(BASE, "_w2l_modeldef.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

pth = os.path.join(BASE, "wav2lip.pth")
onnx_out = os.path.join(BASE, "wav2lip.onnx")
model = mod.Wav2Lip()
ckpt = torch.load(pth, map_location="cpu")
sd = {k.replace("module.", ""): v for k, v in ckpt.get("state_dict", ckpt).items()}
m, u = model.load_state_dict(sd, strict=False)
print("[wav2lip.pth] missing", len(m), "unexpected", len(u))
model.eval()
mel = torch.randn(1, 1, 80, 16)
frames = torch.randn(1, 6, 96, 96)
with torch.no_grad():
    ref = model(mel, frames).numpy()
print("[wav2lip.pth] out", ref.shape, "%.3f..%.3f" % (ref.min(), ref.max()))
torch.onnx.export(model, (mel, frames), onnx_out, export_params=True, opset_version=18,
                  input_names=["mel_spectrogram", "video_frames"],
                  output_names=["predicted_frames"],
                  dynamic_axes={"mel_spectrogram": {0: "batch"}, "video_frames": {0: "batch"},
                                "predicted_frames": {0: "batch"}}, verbose=False)
import onnxruntime as ort
s = ort.InferenceSession(onnx_out, providers=["CPUExecutionProvider"])
o = s.run(None, {"mel_spectrogram": mel.numpy(), "video_frames": frames.numpy()})[0]
print("[wav2lip.pth] onnx diff max %.6f" % np.abs(o - ref).max())

# ---------- 2. 4x_BigFace.pth (RRDBNet) ----------
dst = r"D:\local-multimodal-src\_w2lhd"
arch_src = open(os.path.join(dst, "basicsr_archs_rrdbnet_arch.py"), encoding="utf-8").read()
util_src = open(os.path.join(dst, "basicsr_archs_arch_util.py"), encoding="utf-8").read()
# 去掉 registry 依赖
util_src = util_src.replace("from basicsr.utils.registry import ARCH_REGISTRY", "")
util_src = util_src.replace("from basicsr.ops.dcn import ModulatedDeformConvPack, modulated_deform_conv", "")
util_src = util_src.replace("from basicsr.utils import get_root_logger", "def get_root_logger(*a, **k):\n    import logging\n    return logging.getLogger('sr')")
# 截断掉 DCNv2Pack（依赖 basicsr.ops.dcn，RRDBNet 用不到）
_idx = util_src.find("class DCNv2Pack")
if _idx > 0:
    util_src = util_src[:_idx]
util_src = util_src.replace("from torch.nn.modules.utils import _pair", "from torch.nn.modules.utils import _pair")
arch_src = arch_src.replace("from basicsr.utils.registry import ARCH_REGISTRY", "")
arch_src = arch_src.replace("@ARCH_REGISTRY.register()", "")
arch_src = arch_src.replace("from .arch_util import default_init_weights, make_layer", "")
combined = """import torch
from torch import nn as nn
from torch.nn import functional as F
import math
from torch.nn.modules.utils import _pair
from torch.nn import init as init
from torch.nn.modules.batchnorm import _BatchNorm

""" + util_src + "\n\n" + arch_src
mp = os.path.join(BASE, "_rrdb_def.py")
open(mp, "w", encoding="utf-8").write(combined)

spec2 = importlib.util.spec_from_file_location("rrdb_def", mp)
mod2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(mod2)

sr = mod2.RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32)
ck = torch.load(os.path.join(BASE, "4x_BigFace.pth"), map_location="cpu")
key = "params" if "params" in ck else "state_dict"
sr.load_state_dict(ck[key], strict=True)
sr.eval()
print("[BigFace] loaded, key =", key)
x = torch.rand(1, 3, 96, 96)
with torch.no_grad():
    y = sr(x).numpy()
print("[BigFace] out", y.shape, "expect (1,3,384,384)")
onnx_sr = os.path.join(BASE, "bigface_x4.onnx")
torch.onnx.export(sr, x, onnx_sr, export_params=True, opset_version=18,
                  input_names=["input"], output_names=["output"],
                  dynamic_axes={"input": {0: "batch", 2: "h", 3: "w"},
                                "output": {0: "batch", 2: "H", 3: "W"}}, verbose=False)
s2 = ort.InferenceSession(onnx_sr, providers=["CPUExecutionProvider"])
o2 = s2.run(None, {"input": x.numpy()})[0]
print("[BigFace] onnx diff max %.6f" % np.abs(o2 - y).max())
print("DONE")
