# -*- coding: utf-8 -*-
"""Two-member end-to-end test for Hive group chat.

Starts two isolated instances (separate MM_DATA_DIR), makes them join the
same group over loopback, then A uploads an image + a document and sends a
message carrying them.  B must auto-receive both the message and the file
bytes.  Everything is asserted; nothing is assumed.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PORT_A, PORT_B = 8011, 8012

OK = 0
FAIL = 0


def chk(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  PASS  %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL  %s %s" % (name, extra))


def start(port, data_dir):
    env = dict(os.environ)
    env["MM_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(data_dir, "server.log"), "wb")
    p = subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(port, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get("http://127.0.0.1:%d/api/hive/status" % port, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def api(port, path, body=None, method=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if body is None:
        r = requests.request(method or "GET", url, timeout=timeout)
    else:
        r = requests.post(url, json=body, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:300]}


tmpA = tempfile.mkdtemp(prefix="hiveA_")
tmpB = tempfile.mkdtemp(prefix="hiveB_")
pa = pb = la = lb = None
try:
    print("=" * 62)
    print("1) start two isolated instances")
    pa, la = start(PORT_A, tmpA)
    pb, lb = start(PORT_B, tmpB)
    chk("A ready", wait_ready(PORT_A))
    chk("B ready", wait_ready(PORT_B))

    print("=" * 62)
    print("2) A creates group")
    sc, d = api(PORT_A, "/api/hive/create", {"name": "E2E-Test"})
    chk("create 200", sc == 200, str(sc))
    gid = (d.get("group") or {}).get("gid") or ""
    pw = d.get("password") or ""
    chk("got gid", bool(gid), gid)
    chk("got password(12)", len(pw.replace("-", "")) == 12, pw)
    if not gid:
        raise SystemExit(1)

    sc, mesh = api(PORT_A, "/api/hive/mesh")
    mport = int((mesh.get("mesh") or {}).get("port") or 0)
    chk("A mesh port", mport > 0, str(mport))

    print("=" * 62)
    print("3) B joins with password over loopback")
    sc, j = api(PORT_B, "/api/hive/join",
                {"password": pw, "name": "Bob",
                 "host": "127.0.0.1", "port": mport}, timeout=40)
    chk("join ok", bool(j.get("ok")), json.dumps(j, ensure_ascii=False)[:200])
    chk("same gid", (j.get("group") or {}).get("gid") == gid)

    # wait for peer handshake
    peers = 0
    for _ in range(30):
        time.sleep(0.5)
        sc, st = api(PORT_A, "/api/hive/mesh")
        peers = int((st.get("mesh") or {}).get("peers") or 0)
        if peers > 0:
            break
    chk("A sees peer", peers > 0, "peers=%d" % peers)

    print("=" * 62)
    print("4) A uploads image + document")
    # a real tiny PNG (8x8 gradient-ish, valid zlib stream)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAJElEQVQoU2NkYGD4z0AEYBxV"
        "SFKgAgMDw38GBgZGBgYGRgYAUQ0EBQ7T1s8AAAAASUVORK5CYII=")
    md = ("# 长文档测试\n\n" + "这是一段用于验证群附件分发的正文。\n" * 200).encode("utf-8")
    png_url = "data:image/png;base64," + base64.b64encode(png).decode()
    md_url = "data:text/markdown;base64," + base64.b64encode(md).decode()

    sc, r1 = api(PORT_A, "/api/hive/%s/upload" % gid,
                 {"name": "pic.png", "mime": "image/png", "data": png_url})
    chk("upload image", bool(r1.get("ok")), str(r1.get("error") or "")[:120])
    sc, r2 = api(PORT_A, "/api/hive/%s/upload" % gid,
                 {"name": "doc.md", "mime": "text/markdown", "data": md_url})
    chk("upload doc", bool(r2.get("ok")), str(r2.get("error") or "")[:120])
    f1 = r1.get("file") or {}
    f2 = r2.get("file") or {}
    chk("image meta kind", f1.get("kind") == "image", str(f1.get("kind")))
    chk("doc meta kind", f2.get("kind") in ("doc", "file"), str(f2.get("kind")))

    print("=" * 62)
    print("5) A sends message carrying both files")
    sc, sm = api(PORT_A, "/api/hive/%s/send" % gid,
                 {"text": "看图 + 文档", "extra": {"files": [f1, f2]}})
    chk("send ok", bool(sm.get("ok")), str(sm.get("error") or "")[:120])

    print("=" * 62)
    print("6) B auto-receives message + files")
    got = None
    for _ in range(40):
        time.sleep(0.5)
        sc, ms = api(PORT_B, "/api/hive/%s/messages" % gid)
        for m in (ms.get("messages") or []):
            if "看图" in (m.get("text") or ""):
                got = m
                break
        if got:
            break
    chk("B got message", got is not None)
    if got:
        chk("message has 2 files", len(got.get("files") or []) == 2,
            str(len(got.get("files") or [])))

    # B must have the actual bytes on disk (auto-received, not just a card)
    bdir = os.path.join(tmpB, "hive", "groups", gid, "files")
    have = []
    for _ in range(40):
        if os.path.isdir(bdir):
            have = [n for n in os.listdir(bdir) if not n.endswith(".json")]
        if len(have) >= 2:
            break
        time.sleep(0.5)
    chk("B disk has 2 files", len(have) >= 2, "%d files" % len(have))

    # byte-level identity
    for meta, raw, tag in ((f1, png, "image"), (f2, md, "doc")):
        fid = meta.get("fid") or ""
        sc, rr = api(PORT_B, "/api/hive/%s/files/%s" % (gid, fid), timeout=30)
        # endpoint returns raw bytes; re-request with requests directly
        url = "http://127.0.0.1:%d/api/hive/%s/files/%s" % (PORT_B, gid, fid)
        resp = requests.get(url, timeout=30)
        same = hashlib.md5(resp.content).hexdigest() == hashlib.md5(raw).hexdigest()
        chk("B bytes identical (%s)" % tag, same,
            "%d vs %d bytes" % (len(resp.content), len(raw)))

    print("=" * 62)
    print("7) offline delivery (B stops, A sends, B returns)")
    # stop B, send another message, restart B -> outbox must replay
    pb.terminate()
    try:
        pb.wait(timeout=10)
    except Exception:
        pb.kill()
    sc, sm2 = api(PORT_A, "/api/hive/%s/send" % gid,
                  {"text": "离线时发的消息-OFFLINE"})
    chk("A send while B offline", bool(sm2.get("ok")))
    pb, lb = start(PORT_B, tmpB)
    chk("B back ready", wait_ready(PORT_B))
    # B needs to reconnect to A
    api(PORT_B, "/api/hive/join", {"password": pw, "name": "Bob",
                                   "host": "127.0.0.1", "port": mport}, timeout=40)
    got2 = None
    for _ in range(40):
        time.sleep(0.5)
        sc, ms = api(PORT_B, "/api/hive/%s/messages" % gid)
        for m in (ms.get("messages") or []):
            if "OFFLINE" in (m.get("text") or ""):
                got2 = m
                break
        if got2:
            break
    chk("B got offline message after reconnect", got2 is not None)

finally:
    for p in (pa, pb):
        if p:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    for lg in (la, lb):
        try:
            lg.close()
        except Exception:
            pass
    for d in (tmpA, tmpB):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

print("=" * 62)
print("RESULT: PASS %d / FAIL %d" % (OK, FAIL))
sys.exit(0 if FAIL == 0 else 1)
