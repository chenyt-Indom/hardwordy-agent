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
sd = {k.replace("module.", ""): v for k, v in ckpt["state_dict"].items()}
model = mod.Wav2Lip()
model.load_state_dict(sd)
model.eval()

img = cv2.imread(r"D:\local-multimodal-src\data\avatar2\avatar.png")
box = (322, 244, 694, 738)
x1, y1, x2, y2 = box
face = cv2.resize(img[y1:y2, x1:x2], (96, 96))
mel = np.load(r"D:\local-multimodal-src\_dbg_mel1.npy")

variants = {
    "right_masked": lambda f: (np.concatenate([np.where(np.arange(96)[None, :, None] < 48, f, 0), f], 2) / 255.0),
    "lower_masked": lambda f: (np.concatenate([np.where(np.arange(96)[:, None, None] < 48, f, 0), f], 2) / 255.0),
    "no_mask": lambda f: (np.concatenate([f, f], 2) / 255.0),
}

for name, fn in variants.items():
    inp = fn(face).astype(np.float32)
    fin = np.transpose(inp, (2, 0, 1))[None]
    with torch.no_grad():
        out = model(torch.from_numpy(mel), torch.from_numpy(fin)).numpy()
    pred = np.transpose(out[0], (1, 2, 0))
    cv2.imwrite(r"D:\local-multimodal-src\_var_%s.jpg" % name,
                np.clip(pred * 255, 0, 255).astype(np.uint8))
    print("%-14s pred range %.3f..%.3f  L/R mean %.1f/%.1f  (input L/R %.1f/%.1f)"
          % (name, pred.min(), pred.max(),
             pred[:, :48].mean() * 255, pred[:, 48:].mean() * 255,
             face[:, :48].mean(), face[:, 48:].mean()))
cv2.imwrite(r"D:\local-multimodal-src\_var_input.jpg", face)
print("saved")
