import os
import sys
import importlib.util

BASE = r"D:\local-multimodal-src\data\models\wav2lip"
import torch
import numpy as np
import cv2

spec = importlib.util.spec_from_file_location("w2l_modeldef", os.path.join(BASE, "_w2l_modeldef.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ckpt = torch.load(os.path.join(BASE, "wav2lip_gan.pth"), map_location="cpu")
print("ckpt keys:", list(ckpt.keys())[:10])
sd = ckpt.get("state_dict", ckpt)
sd = {k.replace("module.", ""): v for k, v in sd.items()}
print("n params:", len(sd))
ks = list(sd.keys())
print("first 8:", ks[:8])
print("last 8:", ks[-8:])
# 权重统计
import numpy as np
vals = [v.flatten() for v in sd.values() if v.dtype == torch.float32]
allv = torch.cat(vals)
print("weight mean %.6f std %.6f min %.4f max %.4f" % (allv.mean(), allv.std(), allv.min(), allv.max()))
# BN running_mean 检查
bn = [k for k in ks if "running_mean" in k]
print("bn running_mean count:", len(bn))
for k in bn[:5]:
    print("  ", k, sd[k][:4].tolist())

model = mod.Wav2Lip()
model.load_state_dict(sd)
model.eval()

# 用真实输入
img = cv2.imread(r"D:\local-multimodal-src\data\avatar2\avatar.png")
box = (322, 244, 694, 738)
x1, y1, x2, y2 = box
face = cv2.resize(img[y1:y2, x1:x2], (96, 96))
masked = face.copy()
masked[:, 48:] = 0
inp = np.concatenate([masked, face], axis=2) / 255.0
fin = np.transpose(inp, (2, 0, 1))[None].astype(np.float32)

mel = np.load(r"D:\local-multimodal-src\_dbg_mel1.npy")
with torch.no_grad():
    out = model(torch.from_numpy(mel), torch.from_numpy(fin)).numpy()
pred = np.transpose(out[0], (1, 2, 0))

# 与输入比对
inp_face = (fin[0, 3:6] * 255).transpose(1, 2, 0)
print("\n输出 vs 输入(原始脸):")
print("  pred mean %.3f  input mean %.3f" % (pred.mean() * 255, inp_face.mean()))
d = np.abs(pred * 255 - inp_face)
print("  mean abs diff %.2f  max %.2f" % (d.mean(), d.max()))
# 左半 vs 右半
print("  pred 左半 mean %.1f  右半 mean %.1f" % (pred[:, :48].mean() * 255, pred[:, 48:].mean() * 255))
print("  input 左半 mean %.1f  右半 mean %.1f" % (inp_face[:, :48].mean(), inp_face[:, 48:].mean()))
