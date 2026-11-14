"""端到端：接口层测 motion 参数（不重启服务，用 TestClient）"""
import os, sys, glob, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
from backend import avatar2 as A2

from fastapi.testclient import TestClient
from backend.main import app

c = TestClient(app)

# info
r = c.get("/api/avatar_lipsync/info")
print("info:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])

# 用短文本测三档动作（每段 TTS 约 2 秒，渲染快）
txt = "这是一段测试语音。"
out = {}
for mv in (0, 1, 1.6):
    t0 = time.time()
    r = c.post("/api/avatar_lipsync/speak",
               json={"text": txt, "motion": mv, "max_side": 720})
    dt = time.time() - t0
    if r.status_code != 200:
        print("motion=%s  HTTP %d  %s" % (mv, r.status_code, str(r.text)[:300]))
        continue
    d = r.json()
    out[mv] = d
    print("motion=%s  HTTP200  %.1fs  frames=%d  motion=%s  motion_sec=%s  size=%s  level=%s"
          % (mv, dt, d.get("frames"), d.get("motion"), d.get("motion_sec"),
             d.get("size"), d.get("motion_level")))

# 越界值夹紧
r = c.post("/api/avatar_lipsync/speak", json={"text": "测试", "motion": 99, "max_side": 720})
if r.status_code == 200:
    print("motion=99 -> level=%s (应被夹到 2.0)" % r.json().get("motion_level"))
else:
    print("motion=99 HTTP", r.status_code, str(r.text)[:200])

# 清理本次生成的测试视频
import glob as g
root = A2._root()
vdir = A2._vdir()
made = [d["name"] for d in out.values()] + ([r.json()["name"]] if r.status_code == 200 else [])
for n in made:
    p = os.path.join(vdir, n)
    if os.path.isfile(p):
        os.remove(p)
        print("清理:", n)
print("done")
