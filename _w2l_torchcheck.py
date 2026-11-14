import os
import sys
import importlib.util

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
sys.path.insert(0, r"D:\local-multimodal-src\backend")

import torch
import numpy as np
import cv2

spec = importlib.util.spec_from_file_location("w2l_modeldef", os.path.join(BASE, "_w2l_modeldef.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

model = mod.Wav2Lip()
ckpt = torch.load(os.path.join(BASE, "wav2lip_gan.pth"), map_location="cpu")
sd = {k.replace("module.", ""): v for k, v in ckpt.get("state_dict", ckpt).items()}
model.load_state_dict(sd)
model.eval()

# 准备真实输入
img = cv2.imread(r"D:\local-multimodal-src\data\avatar2\avatar.png")
# 用固定的人脸框
box = (322, 244, 694, 738)
x1, y1, x2, y2 = box
face = cv2.resize(img[y1:y2, x1:x2], (96, 96))
masked = face.copy()
masked[:, 48:] = 0
inp = np.concatenate([masked, face], axis=2) / 255.0
fin = np.transpose(inp, (2, 0, 1))[None].astype(np.float32)

mel = np.load(r"D:\local-multimodal-src\_dbg_mel.npy") if os.path.exists(r"D:\local-multimodal-src\_dbg_mel.npy") else None
if mel is None:
    mel = np.random.randn(1, 1, 80, 16).astype(np.float32) * 0.5

with torch.no_grad():
    out = model(torch.from_numpy(mel), torch.from_numpy(fin)).numpy()
pred = np.transpose(out[0], (1, 2, 0))
print("torch pred range %.3f..%.3f mean %.3f" % (pred.min(), pred.max(), pred.mean()))
cv2.imwrite(r"D:\local-multimodal-src\_dbg_torch_pred.jpg", np.clip(pred * 255, 0, 255).astype(np.uint8))

# 同时存输入，供 onnx 对比
np.save(r"D:\local-multimodal-src\_dbg_fin.npy", fin)
np.save(r"D:\local-multimodal-src\_dbg_mel1.npy", mel)
cv2.imwrite(r"D:\local-multimodal-src\_dbg_input96.jpg", face)
print("saved")
