# -*- coding: utf-8 -*-
"""群聊 HTTP 接口验证 —— 真发请求，跑完清理。"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
OK, BAD = [], []


def call(path, body=None, method=None):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": repr(e)}


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


print("=" * 70)
print("群聊 HTTP 接口验证  (%s)" % BASE)
print("=" * 70)

st, d = call("/api/hive/status")
chk("GET /api/hive/status 200", st == 200 and d.get("ok", True) is not False, st)
chk("status 返回 user_id", bool(d.get("user_id")), d.get("user_id"))

st, d = call("/api/hive/groups")
chk("GET /api/hive/groups 200", st == 200, st)
chk("groups 是列表", isinstance(d.get("groups"), list), type(d.get("groups")).__name__)

st, d = call("/api/hive/create", {"name": "HTTP验证群"})
chk("POST /api/hive/create 200", st == 200 and d.get("ok"), (st, d.get("error")))
gid = (d.get("group") or {}).get("gid") or d.get("gid")
chk("建群返回 gid", bool(gid), gid)

if gid:
    st, d = call("/api/hive/%s/send" % gid, {"text": "HTTP 第一条"})
    chk("POST send 200", st == 200 and d.get("ok"), (st, d.get("error")))

    st, d = call("/api/hive/%s/messages" % gid)
    chk("GET messages 200", st == 200, st)
    msgs = d.get("messages") or []
    chk("消息数=2（系统+1条）", len(msgs) == 2, len(msgs))

    st, d = call("/api/hive/%s/messages?since=1" % gid)
    inc = d.get("messages") or []
    chk("增量 since=1 返回 1 条", len(inc) == 1, len(inc))

    st, d = call("/api/hive/%s/invite" % gid)
    chk("GET invite 200", st == 200 and d.get("ok"), (st, d.get("error")))
    chk("invite 带二维码", str(d.get("qr_png", "")).startswith("data:image/png"),
        str(d.get("qr_png", ""))[:26])

    st, d = call("/api/hive/%s/kick" % gid, {"user_id": "u_不存在"})
    chk("踢不存在的人返回错误而非 500", st in (200, 400, 404), st)

    # 空消息应被拒绝
    st, d = call("/api/hive/%s/send" % gid, {"text": ""})
    chk("空消息被拒绝", not d.get("ok", True), (st, d.get("error")))

    # 清理
    import shutil, os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from backend import hive
    shutil.rmtree(hive._gdir(gid), ignore_errors=True)
    left = [x for x in hive.list_groups() if x["gid"] == gid]
    chk("测试群已清理", len(left) == 0, len(left))

st, d = call("/api/hive/relay")
chk("GET /api/hive/relay 200", st == 200, st)

st, d = call("/api/hive/relay/probe", {"url": "http://127.0.0.1:1"})
chk("probe 对坏地址返回失败而非 500", st == 200 and not d.get("ok", True), (st, d.get("ok")))

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
if BAD:
    for b in BAD:
        print("  - " + b)
print("=" * 70)
