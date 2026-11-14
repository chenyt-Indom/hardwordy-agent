# -*- coding: utf-8 -*-
"""Group @hd long-output end-to-end test.

A asks the AI (in-group) for a long article.  Asserts:
  · the AI message text is longer than HIVE_LONG_CHARS (3000)
  · it carries a .md attachment automatically
  · member B receives BOTH the message and the file bytes
  · the file content equals the message text
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
sys.path.insert(0, ROOT)
PY = sys.executable
PORT_A, PORT_B = 8013, 8014

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


from backend import keyvault  # noqa: E402

KEY = os.environ.get("DEEPSEEK_API_KEY") or keyvault.get_key()
if not KEY:
    print("SKIP: no cloud key")
    sys.exit(2)
print("key present: %d chars" % len(KEY))


def start(port, data_dir):
    env = dict(os.environ)
    env["MM_DATA_DIR"] = data_dir
    env["MM_MODEL"] = "deepseek-flash"
    env["DEEPSEEK_API_KEY"] = KEY
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
            if requests.get("http://127.0.0.1:%d/api/hive/status" % port,
                            timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def api(port, path, body=None, timeout=60):
    url = "http://127.0.0.1:%d%s" % (port, path)
    r = requests.post(url, json=body, timeout=timeout) if body is not None \
        else requests.get(url, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:300]}


tmpA = tempfile.mkdtemp(prefix="hiveLA_")
tmpB = tempfile.mkdtemp(prefix="hiveLB_")
pa = pb = la = lb = None
try:
    print("=" * 62)
    print("1) start A and B")
    pa, la = start(PORT_A, tmpA)
    pb, lb = start(PORT_B, tmpB)
    chk("A ready", wait_ready(PORT_A))
    chk("B ready", wait_ready(PORT_B))

    sc, d = api(PORT_A, "/api/hive/create", {"name": "LongText"})
    gid = (d.get("group") or {}).get("gid") or ""
    pw = d.get("password") or ""
    chk("group created", bool(gid), gid)
    sc, mesh = api(PORT_A, "/api/hive/mesh")
    mport = int((mesh.get("mesh") or {}).get("port") or 0)
    sc, j = api(PORT_B, "/api/hive/join",
                {"password": pw, "name": "Bob", "host": "127.0.0.1",
                 "port": mport}, timeout=40)
    chk("B joined", bool(j.get("ok")))
    for _ in range(30):
        time.sleep(0.5)
        sc, st = api(PORT_A, "/api/hive/mesh")
        if int((st.get("mesh") or {}).get("peers") or 0) > 0:
            break
    chk("peers connected",
        int((api(PORT_A, "/api/hive/mesh")[1].get("mesh") or {}).get("peers") or 0) > 0)

    print("=" * 62)
    print("2) A asks in-group for a long article")
    instr = ("@hd 请写一篇完整的文章，主题是「为什么软件项目需要分层架构」。"
             "要求：包含引言、至少 6 个分论点（每个都要有解释和例子）、结论；"
             "全文不少于 3500 个汉字，不要只给提纲，不要写「其余同理」。")
    sc, sm = api(PORT_A, "/api/hive/%s/send" % gid, {"text": instr})
    chk("send accepted", bool(sm.get("ok")))
    chk("woke model", bool(sm.get("woke_model")))

    print("=" * 62)
    print("3) wait for AI answer (up to 240s)")
    ai = None
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(2)
        sc, ms = api(PORT_A, "/api/hive/%s/messages" % gid)
        for m in (ms.get("messages") or []):
            if m.get("type") == "ai" and len(m.get("text") or "") > 200:
                ai = m
                break
        if ai:
            break
    chk("AI message arrived", ai is not None,
        "%.1fs" % (time.time() - t0))
    if not ai:
        raise SystemExit(1)

    body = ai.get("text") or ""
    n = len(body)
    chk("text longer than 3000 chars", n > 3000, "%d chars" % n)
    chk("not truncated by max_tokens",
        ("max_tokens" not in body) and ("被截断" not in body[-200:]),
        repr(body[-60:]))
    files = ai.get("files") or []
    chk("auto .md attachment", len(files) == 1 and
        str((files[0] or {}).get("name") or "").endswith(".md"),
        str([f.get("name") for f in files]))
    chk("long_chars recorded", int(ai.get("long_chars") or 0) == n,
        str(ai.get("long_chars")))

    print("=" * 62)
    print("4) B receives message + file bytes")
    got = None
    for _ in range(40):
        sc, ms = api(PORT_B, "/api/hive/%s/messages" % gid)
        for m in (ms.get("messages") or []):
            if m.get("type") == "ai" and len(m.get("text") or "") > 200:
                got = m
                break
        if got:
            break
        time.sleep(1)
    chk("B got AI message", got is not None)
    if got:
        chk("B sees same length", len(got.get("text") or "") == n,
            "%d vs %d" % (len(got.get("text") or ""), n))

    fid = (files[0] or {}).get("fid") if files else ""
    bdir = os.path.join(tmpB, "hive", "groups", gid, "files")
    have = []
    for _ in range(40):
        if os.path.isdir(bdir):
            have = [x for x in os.listdir(bdir) if not x.endswith(".json")]
        if have:
            break
        time.sleep(1)
    chk("B disk has the file", len(have) >= 1, str(have[:3]))
    if fid:
        resp = requests.get("http://127.0.0.1:%d/api/hive/%s/files/%s"
                            % (PORT_B, gid, fid), timeout=60)
        chk("B file bytes == message text",
            hashlib.md5(resp.content).hexdigest()
            == hashlib.md5(body.encode("utf-8")).hexdigest(),
            "%d vs %d bytes" % (len(resp.content), len(body.encode("utf-8"))))
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
        shutil.rmtree(d, ignore_errors=True)

print("=" * 62)
print("RESULT: PASS %d / FAIL %d" % (OK, FAIL))
sys.exit(0 if FAIL == 0 else 1)
