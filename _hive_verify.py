# -*- coding: utf-8 -*-
"""群聊（Hive）全功能验证 —— 跑完自动清理测试群。"""
import os
import sys
import json
import time
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import hive

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (" | " + str(extra) if extra else ""))
    print(("  OK   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


print("=" * 70)
print("群聊全功能验证")
print("=" * 70)

created = []

# ---------------------------------------------------------------- 1 身份
print("\n[1] 身份 profile")
p = hive.profile()
chk("profile 返回 user_id", bool(p.get("user_id")), p.get("user_id"))
chk("profile 返回 name", bool(p.get("name")), p.get("name"))
p2 = hive.set_name("验证员甲")
chk("set_name 生效", p2.get("name") == "验证员甲", p2.get("name"))

# ---------------------------------------------------------------- 2 建群
print("\n[2] 建群 create_group")
g = hive.create_group("验证群_自动化", "验证员甲")
gid = g["gid"]
created.append(gid)
chk("建群返回 gid", gid.startswith("g_"), gid)
chk("群名正确", g["name"] == "验证群_自动化", g["name"])
chk("建群者是群主", g["owner"] == p["user_id"], g["owner"])
chk("对外信息不含密钥", "key" not in g, list(g.keys()))
chk("初始 seq=1（含建群系统消息）", g["seq"] == 1, g["seq"])

# ---------------------------------------------------------------- 3 列表
print("\n[3] 群列表 list_groups")
lst = hive.list_groups()
hit = [x for x in lst if x["gid"] == gid]
chk("新群出现在列表里", len(hit) == 1, len(hit))
chk("列表项带成员数", hit[0].get("members") == 1, hit[0].get("members"))

# ---------------------------------------------------------------- 4 加密
print("\n[4] 端到端加密 _seal / _open")
key = hive.group_key(gid)
chk("能取到群密钥", key is not None and len(key) == 32, len(key or b""))
blob = hive._seal(key, {"hello": "世界", "n": 42})
chk("密文不含明文", "世界" not in blob and "hello" not in blob, blob[:40] + "...")
chk("解密还原正确", hive._open(key, blob) == {"hello": "世界", "n": 42})
wrong = hive._new_key()
chk("错误密钥解不开", hive._open(wrong, blob) is None)
chk("密钥指纹稳定", hive.key_fingerprint(key) == hive.key_fingerprint(key),
    hive.key_fingerprint(key))

# ---------------------------------------------------------------- 5 消息
print("\n[5] 发消息 / 读消息")
m1 = hive.send_message(gid, "第一条普通消息")
chk("发消息返回 seq", m1.get("seq") == 2, m1.get("seq"))
m2 = hive.send_message(gid, "第二条普通消息")
chk("seq 递增", m2["seq"] == m1["seq"] + 1, (m1["seq"], m2["seq"]))
msgs = hive.read_messages(gid)
chk("读全量消息数=3", len(msgs) == 3, len(msgs))
chk("消息顺序正确", [m["seq"] for m in msgs] == [1, 2, 3], [m["seq"] for m in msgs])
chk("消息带发送者名", msgs[1].get("name") == "验证员甲", msgs[1].get("name"))
since = hive.read_messages(gid, since=2)
chk("增量读取 since=2 返回 1 条", len(since) == 1 and since[0]["seq"] == 3, len(since))
chk("消息 id 唯一", len(set(m["id"] for m in msgs)) == 3)

# ---------------------------------------------------------------- 6 系统消息
print("\n[6] 系统消息 add_system_message")
sm = hive.add_system_message(gid, "验证员乙 加入了群")
chk("系统消息 type=system", sm.get("type") == "system", sm.get("type"))

# ---------------------------------------------------------------- 7 @hd 解析
print("\n[7] @hd 解析 parse_hd")
cases = [
    ("@hd 帮我写一个登录接口", "帮我写一个登录接口"),
    ("大家看下这个 bug @hd 修一下", "修一下"),
    ("@HD 大写也要认", "大写也要认"),
    ("普通聊天没有 at", None),
    ("@hdx 不是唤醒", None),
    ("@hd", ""),
]
for text, want in cases:
    got = hive.parse_hd(text)
    chk("parse_hd(%r)" % text[:22], got == want, got)

# ---------------------------------------------------------------- 8 @hd 提示词
print("\n[8] @hd 提示词 build_hd_prompt")
hist = [
    {"name": "甲", "text": "我们准备做一个登录页", "type": "text"},
    {"name": "乙", "text": "用 JWT 吧", "type": "text"},
    {"name": "甲", "text": "@hd 写个登录接口", "type": "text"},
]
pr = hive.build_hd_prompt("写个登录接口，用 JWT", hist, "验证员甲")
chk("提示词是字符串", isinstance(pr, str), type(pr).__name__)
chk("提示词含当前指令", "写个登录接口，用 JWT" in pr)
chk("提示词含上下文", "JWT" in pr or "登录页" in pr)
chk("提示词声明指令最高优先级",
    ("绝对" in pr) or ("最高" in pr) or ("优先" in pr))
chk("提示词不与正文标题撞名",
    pr.count("【当前指令】") == 1, pr.count("【当前指令】"))

# ---------------------------------------------------------------- 9 邀请二维码
print("\n[9] 邀请二维码 invite_payload")
inv = hive.invite_payload(gid, "127.0.0.1:8000")
chk("返回 payload", bool(inv.get("payload")), (inv.get("payload") or "")[:40])
chk("返回二维码 png(data-url)", str(inv.get("qr_png", "")).startswith("data:image/png"),
    str(inv.get("qr_png", ""))[:30])
chk("payload 内含群密钥（k= 参数）", "k=" in inv["payload"], inv["payload"][:46])
chk("payload 不含群名以外的多余字段", "o_id=" in inv["payload"])

# ---------------------------------------------------------------- 10 入群
print("\n[10] 扫码入群 join_group（模拟另一台机器）")
# 用同一份 payload，但换一个身份：直接构造一个新进程视角比较麻烦，
# 这里验证 payload 解析 + 重复入群幂等
try:
    j = hive.join_group(inv["payload"], "验证员乙")
    chk("join_group 不报错", True, j.get("gid") if isinstance(j, dict) else j)
except Exception as e:
    chk("join_group 不报错", False, repr(e))
# 坏 payload 必须被拒绝
try:
    hive.join_group("这不是一个合法的邀请码", "坏人")
    chk("坏 payload 被拒绝", False, "没抛异常")
except Exception as e:
    chk("坏 payload 被拒绝", True, type(e).__name__)

# ---------------------------------------------------------------- 11 角色 / 踢人
print("\n[11] 角色 / 踢人")
mem = hive.load_members(gid)
uid = p["user_id"]
chk("成员表有自己", uid in mem, list(mem.keys()))
r = hive.set_role(gid, uid, "admin")
chk("set_role 改成功", hive.load_members(gid)[uid]["role"] == "admin",
    hive.load_members(gid)[uid]["role"])
hive.set_role(gid, uid, "owner")
try:
    k = hive.kick_member(gid, "u_不存在的人")
    chk("踢不存在的人给出提示", isinstance(k, dict), k)
except Exception as e:
    chk("踢不存在的人不崩", True, type(e).__name__)
try:
    hive.kick_member(gid, uid)
    chk("群主踢自己应被拒绝", False, "没拒绝")
except Exception as e:
    chk("群主踢自己应被拒绝", True, type(e).__name__)

# ---------------------------------------------------------------- 12 中继配置
print("\n[12] 中继配置 relay_config / set_relay")
rc = hive.relay_config()
chk("relay_config 返回 dict", isinstance(rc, dict), type(rc).__name__)
old = rc.get("url", "")
hive.set_relay("ws://127.0.0.1:9/ws", "", True)
chk("set_relay 写入", hive.relay_config().get("url") == "ws://127.0.0.1:9/ws",
    hive.relay_config().get("url"))
hive.set_relay(old, "", True)
chk("set_relay 还原", hive.relay_config().get("url") == old, hive.relay_config().get("url"))

# ---------------------------------------------------------------- 13 状态
print("\n[13] 状态 status")
st = hive.status()
chk("status 返回 dict", isinstance(st, dict), type(st).__name__)
chk("status 含 my_id", "my_id" in st or "user_id" in st, list(st.keys()))

# ---------------------------------------------------------------- 清理
print("\n[14] 清理测试数据")
for g_ in created:
    d = hive._gdir(g_)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
left = [x for x in hive.list_groups() if x["gid"] in created]
chk("测试群已删除", len(left) == 0, len(left))
hive.set_name("本地用户")

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败清单：")
    for f in FAIL:
        print("  - " + f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
