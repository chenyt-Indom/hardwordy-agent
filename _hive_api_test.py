# -*- coding: utf-8 -*-
"""群聊接口端到端测试（走真实 FastAPI 路由，不是直接调函数）。"""
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient

import backend.hive as H
from backend.main import app

FAIL = []


def chk(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + f"  {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


# 用独立的测试数据目录，跑完删掉，不污染真实群数据
TEST_DATA = os.path.join(ROOT, "_hive_api_data")
shutil.rmtree(TEST_DATA, ignore_errors=True)
H._DATA = TEST_DATA
H._GROUPS = os.path.join(TEST_DATA, "groups")
H._PROFILE = os.path.join(TEST_DATA, "profile.json")
H._CFG = os.path.join(TEST_DATA, "relay.json")

c = TestClient(app)

print("== 1. 状态 ==")
r = c.get("/api/hive/status")
d = r.json()
chk("status 200", r.status_code == 200)
chk("返回本机身份", bool(d["hive"]["user_id"]), d["hive"]["user_id"])
chk("二维码能力正常", d["hive"]["qr_ok"] is True)
chk("初始没有群", d["hive"]["groups"] == 0)

print("== 2. 改昵称 ==")
r = c.post("/api/hive/profile", json={"name": "老王"})
chk("改名成功", r.json()["profile"]["name"] == "老王", r.json()["profile"]["name"])

print("== 3. 建群 ==")
r = c.post("/api/hive/create", json={"name": "官网改版"})
d = r.json()
chk("建群成功", d.get("ok") is True and d["group"]["gid"].startswith("g_"))
gid = d["group"]["gid"]
chk("群名正确", d["group"]["name"] == "官网改版")
chk("我是群主", d["group"]["owner"] == H.profile()["user_id"])
chk("返回里不含密钥", "key" not in d["group"])

print("== 4. 群列表 ==")
d = c.get("/api/hive/groups").json()
chk("列表有 1 个群", len(d["groups"]) == 1)
chk("列表带成员数", d["groups"][0]["members"] == 1, str(d["groups"][0]["members"]))

print("== 5. 发消息（普通聊天，不唤醒模型）==")
r = c.post(f"/api/hive/{gid}/send", json={"text": "大家好，前端我来做"})
d = r.json()
chk("发送成功", d.get("ok") is True)
chk("没有唤醒模型", d.get("woke_model") is False)

print("== 6. @hd 会唤醒模型 ==")
r = c.post(f"/api/hive/{gid}/send", json={"text": "@hd 帮我列一下首页要改哪几处"})
d = r.json()
chk("识别到 @hd", d.get("woke_model") is True)
chk("指令被抽出来", "首页要改哪几处" in str(d.get("sent")), "")

print("== 7. 读消息 ==")
d = c.get(f"/api/hive/{gid}/messages").json()
msgs = d["messages"]
chk("有消息", len(msgs) >= 3, f"{len(msgs)} 条")
chk("按 seq 升序", [m["seq"] for m in msgs] == sorted(m["seq"] for m in msgs))
chk("带成员表", len(d["members"]) == 1)
last = msgs[-1]["seq"]
chk("增量读（since=最后一条）得 0 条",
    len(c.get(f"/api/hive/{gid}/messages?since={last}").json()["messages"]) == 0)

print("== 8. 二维码 ==")
d = c.get(f"/api/hive/{gid}/invite").json()
chk("返回载荷", d["payload"].startswith("hive://join?"))
chk("返回 PNG", d["qr_png"].startswith("data:image/png;base64,"), f"{len(d['qr_png'])} 字节")
chk("载荷里有群号", gid in d["payload"])

print("== 9. 别人扫码入群 ==")
payload = d["payload"]
backup = H._read_json(H._PROFILE)
H._write_json(H._PROFILE, {"user_id": "u_other001", "name": "小李", "created": 0})
try:
    r = c.post("/api/hive/join", json={"payload": payload})
    d2 = r.json()
    chk("入群成功", d2.get("ok") is True, str(d2.get("error") or ""))
    chk("成员变 2 人", len(H.load_members(gid)) == 2)
    # 非群主踢人应被拒
    r = c.post(f"/api/hive/{gid}/kick", json={"user_id": H.profile()["user_id"]})
    chk("非群主踢人被拒", r.json().get("ok") is False, r.json().get("error", ""))
finally:
    H._write_json(H._PROFILE, backup)

print("== 10. 群主踢人 ==")
r = c.post(f"/api/hive/{gid}/kick", json={"user_id": "u_other001"})
d = r.json()
chk("踢人成功", d.get("ok") is True, str(d.get("error") or ""))
chk("成员回到 1 人", len(H.load_members(gid)) == 1)
chk("有踢人系统消息", "被移出了群" in H.read_messages(gid)[-1]["text"])

print("== 11. 中继配置 ==")
r = c.post("/api/hive/relay", json={"url": "ws://127.0.0.1:8765", "enabled": True})
chk("保存中继地址", r.json()["relay"]["url"] == "ws://127.0.0.1:8765")
d = c.get("/api/hive/relay").json()
chk("能读回中继地址", d["relay"]["url"] == "ws://127.0.0.1:8765")

print("== 12. 边界 ==")
chk("空群名建群仍可（有兜底）",
    c.post("/api/hive/create", json={"name": ""}).json().get("ok") is True)
chk("空口令入群被拒",
    c.post("/api/hive/join", json={"payload": ""}).json().get("ok") is False)
chk("不存在的群读消息返回空",
    c.get("/api/hive/g_not_exist/messages").json()["messages"] == [])
chk("不存在的群邀请报错",
    c.get("/api/hive/g_not_exist/invite").json().get("ok") is False)

shutil.rmtree(TEST_DATA, ignore_errors=True)
print()
print("=" * 52)
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")
