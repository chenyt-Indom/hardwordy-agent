"""群聊链路实测：连中继 → 发消息 → 读回 → 检查序号/分发。"""
import os, sys, json, time
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient
import backend.main as M

GID = "g_9fff9ee702b9"
c = TestClient(M.app)
res = []


def chk(name, ok, detail=""):
    res.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  " + str(detail) if detail else ""))


r = c.post("/api/hive/connect", json={"gid": GID})
chk("连接中继", r.status_code == 200 and r.json().get("client", {}).get("connected"),
    r.json().get("client", {}).get("latency_ms"))

time.sleep(0.5)
st = c.get("/api/hive/status").json()["client"]
chk("状态 online", st.get("state") == "online", st.get("state"))
chk("模式 relay", st.get("mode") == "relay", st.get("mode"))

mark = "链路自检 %d" % int(time.time())
t0 = time.time()
r = c.post("/api/hive/%s/send" % GID, json={"text": mark})
dt = (time.time() - t0) * 1000
j = r.json()
chk("发送消息", r.status_code == 200 and j.get("ok"), "%.0f ms" % dt)

time.sleep(0.6)
ms = c.get("/api/hive/%s/messages" % GID).json()
arr = ms.get("messages") or []
hit = [m for m in arr if mark in str(m.get("text", ""))]
chk("消息已落盘", len(hit) >= 1, "共 %d 条" % len(arr))

st2 = c.get("/api/hive/status").json()["client"]
rl = st2.get("relay") or {}
chk("中继在线", rl.get("online"), "rtt %.1f ms" % (rl.get("rtt_ms") or 0))
chk("帧收发正常", (rl.get("frames_out") or 0) > 0 and (rl.get("dropped") or 0) == 0,
    "out %s / in %s / dropped %s" % (rl.get("frames_out"), rl.get("frames_in"), rl.get("dropped")))
chk("seq 单调", (rl.get("server_seq") or 0) >= (rl.get("last_seq") or 0),
    "last %s / server %s" % (rl.get("last_seq"), rl.get("server_seq")))

peers = c.get("/api/hive/%s/peers" % GID).json()
print("peers:", json.dumps(peers, ensure_ascii=False)[:400])

ok_n = sum(1 for _, o, _ in res if o)
print("\n===== %d/%d 通过 =====" % (ok_n, len(res)))
