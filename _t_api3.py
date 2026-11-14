"""复验：motion=0 回显是否正确 + 静止档真的不动"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar2 as A2
from fastapi.testclient import TestClient
from backend.main import app

c = TestClient(app)
r = c.post("/api/avatar_lipsync/speak", json={"text": "测试一下静止档。", "motion": 0, "max_side": 720})
print("HTTP", r.status_code)
d = r.json()
print("motion_level:", d.get("motion_level"), " motion:", d.get("motion"),
      " frames:", d.get("frames"))

# 验证静止档：整帧位移应≈0
p = d.get("path")
cap = cv2.VideoCapture(p)
gs = []
while True:
    ok, f = cap.read()
    if not ok: break
    gs.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
cap.release()
ref = gs[0].astype(np.float32)
h, w = ref.shape
win = cv2.createHanningWindow((w, h), cv2.CV_32F)
sh = []
for i in range(0, len(gs), 3):
    (dx, dy), _ = cv2.phaseCorrelate(ref, gs[i].astype(np.float32), win)
    sh.append((dx, dy))
a = np.array(sh)
print("静止档 头部位移 dx %+.2f~%+.2f  dy %+.2f~%+.2f" %
      (a[:, 0].min(), a[:, 0].max(), a[:, 1].min(), a[:, 1].max()))
os.remove(p)
print("清理测试视频")
