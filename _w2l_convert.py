"""把 wav2lip_gan.pth 转成 ONNX（用 Python 3.12 + torch CPU 离线执行）

产出：
  wav2lip_gan.onnx      输入 mel(1,1,80,16) + frames(1,6,96,96) -> (1,3,96,96)
"""
import os
import sys
import importlib.util

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
PTH = os.path.join(BASE, "wav2lip_gan.pth")
ONNX = os.path.join(BASE, "wav2lip_gan.onnx")
MODELDEF = os.path.join(BASE, "_w2l_modeldef.py")

import torch
import numpy as np

print("torch", torch.__version__)

spec = importlib.util.spec_from_file_location("w2l_modeldef", MODELDEF)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Wav2Lip = mod.Wav2Lip

model = Wav2Lip()
ckpt = torch.load(PTH, map_location="cpu")
sd = ckpt.get("state_dict", ckpt)
new_sd = {k.replace("module.", ""): v for k, v in sd.items()}
missing, unexpected = model.load_state_dict(new_sd, strict=False)
print("missing:", len(missing), "unexpected:", len(unexpected))
if missing:
    print("  missing sample:", missing[:5])
if unexpected:
    print("  unexpected sample:", unexpected[:5])
model.eval()

mel = torch.randn(1, 1, 80, 16)
frames = torch.randn(1, 6, 96, 96)

with torch.no_grad():
    ref = model(mel, frames).numpy()
print("pytorch out", ref.shape, "range %.3f..%.3f" % (ref.min(), ref.max()))

torch.onnx.export(
    model, (mel, frames), ONNX,
    export_params=True,
    opset_version=11,
    input_names=["mel_spectrogram", "video_frames"],
    output_names=["predicted_frames"],
    dynamic_axes={"mel_spectrogram": {0: "batch"}, "video_frames": {0: "batch"},
                  "predicted_frames": {0: "batch"}},
    verbose=False,
)
print("exported", ONNX, "%.1f MB" % (os.path.getsize(ONNX) / 1e6))

# 用 onnxruntime 验证一致性
import onnxruntime as ort
sess = ort.InferenceSession(ONNX, providers=["CPUExecutionProvider"])
out = sess.run(None, {"mel_spectrogram": mel.numpy(), "video_frames": frames.numpy()})[0]
d = np.abs(out - ref)
print("ONNX vs torch: max diff %.6f  mean %.8f" % (d.max(), d.mean()))
print("VERIFY", "PASS" if d.max() < 1e-4 else "FAIL")
