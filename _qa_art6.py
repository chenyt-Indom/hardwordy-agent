# -*- coding: utf-8 -*-
"""验证附件补发链路：后入群的人也能打开产物。"""
import os
import sys
import time

OK, BAD = [], []


def chk(n, c, d=""):
    (OK if c else BAD).append(n + (" | " + d if d else ""))


from backend import hive, main, hive_client, hive_mesh

g = hive.create_group("补发测试群", "测试员")
gid = g["gid"]
m = hive.save_file(gid, "旧产物.txt", b"hello-relay" * 100, "text/plain",
                   tag="ai", source="测试")
fid = m["fid"]
print("fid =", fid)

# ---------- 1) T_FREQ 常量存在且被 _dispatch 处理
chk("T_FREQ 已定义", getattr(hive_mesh, "T_FREQ", None) == 14,
    str(getattr(hive_mesh, "T_FREQ", None)))

node = hive_mesh.MESH.node()


class _FakePeer:
    gid = gid
    uid = "u_fake"
    alive = True

    def send(self, *a, **k):
        return True


got = []
old_cb = node.on_freq
node.on_freq = lambda g_, o_: got.append((g_, o_))
try:
    node._dispatch(_FakePeer(), hive_mesh.T_FREQ, {"fid": fid, "ttl": 1})
finally:
    node.on_freq = old_cb
chk("mesh 收到 T_FREQ 会回调上层", len(got) == 1 and got[0][1].get("fid") == fid,
    str(got))

# ---------- 2) 本机有文件 → 触发补发
calls = []
cli = hive_client.client()
old_send = cli.send_file
cli.send_file = lambda g_, meta, data, **k: (calls.append((g_, meta.get("fid"),
                                                          len(data))), {"ok": True})[1]
main._freq_sent.clear()
try:
    main._hive_on_freq(gid, {"fid": fid})
    for _ in range(30):
        if calls:
            break
        time.sleep(0.05)
    chk("本机有文件时会补发", len(calls) == 1, str(calls))
    chk("补发的是同一份", calls and calls[0][1] == fid, str(calls))
    chk("补发内容完整", calls and calls[0][2] == len(b"hello-relay" * 100),
        str(calls))

    # ---------- 3) 节流：60 秒内第二次不再发
    main._hive_on_freq(gid, {"fid": fid})
    time.sleep(0.3)
    chk("60 秒内不重复补发（防泛洪）", len(calls) == 1, str(len(calls)))

    # ---------- 4) 本机没有的 fid → 不补发
    main._hive_on_freq(gid, {"fid": "f_不存在"})
    time.sleep(0.2)
    chk("本机没有就不补发", len(calls) == 1, str(len(calls)))
finally:
    cli.send_file = old_send

# ---------- 5) 缺文件时接口返回可区分的状态码（不是 404 让人以为没了）
from fastapi.testclient import TestClient
api = TestClient(main.app)
os.remove(hive.file_path(gid, fid))     # 模拟"本机没有本体"
r = api.get("/api/hive/%s/files/%s" % (gid, fid))
chk("缺文件不是静默 404", r.status_code in (404, 425), str(r.status_code))
chk("提示文案说清了原因",
    ("补发" in r.text) or ("没有" in r.text), r.text[:80])

# ---------- 6) request_file 在没网时安全失败
r2 = cli.request_file(gid, fid)
chk("request_file 安全返回", isinstance(r2, dict) and "ok" in r2, str(r2))

# ---------- 7) 有文件时正常返回
m2 = hive.save_file(gid, "有产物.txt", b"abc", "text/plain", tag="ai")
r = api.get("/api/hive/%s/files/%s" % (gid, m2["fid"]))
chk("有文件时 200", r.status_code == 200 and r.content == b"abc",
    "%s %s" % (r.status_code, r.content[:20]))

import shutil
shutil.rmtree(hive._gdir(gid), ignore_errors=True)

print("==== 通过 %d ====" % len(OK))
for x in OK:
    print("  OK  " + x)
if BAD:
    print("==== 失败 %d ====" % len(BAD))
    for x in BAD:
        print("  FAIL " + x)
    sys.exit(1)
