# -*- coding: utf-8 -*-
"""探针：复现 test_ollama_error.py 第 4/5/6 节「stub 收到 0 次」的现象。

自己起一个 stub Ollama + 一个真实服务，逐个发请求，把每一节的结果打出来。
跑完即删。
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_probe_oe_")
PORT_APP, PORT_STUB = 18431, 18432

RAW_ERR = "invalid character '\\n' in string literal"
OK_REPLY = "好的，这是重试之后正常给出的回答。"
STUB_ROUNDS = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        txt = json.dumps(req.get("messages") or [], ensure_ascii=False)
        key = next((k for k in ("瞬时坏一次", "一直坏", "模型不存在", "普通提问")
                    if k in txt), "")
        STUB_ROUNDS.append(key)
        n_th = STUB_ROUNDS.count(key)
        print("  [stub] key=%r 第%d次" % (key, n_th))

        def ndjson(items):
            data = b"".join((json.dumps(x, ensure_ascii=False) + "\n").encode("utf-8")
                            for x in items)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        if not req.get("stream", True):
            body = json.dumps({"message": {"role": "assistant", "content": "好的。"},
                               "done": True, "done_reason": "stop"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if key == "瞬时坏一次" and n_th == 1:
            return ndjson([{"message": {"role": "assistant", "content": "我来看一下。"},
                            "done": False},
                           {"error": RAW_ERR, "done": True}])
        if key == "一直坏":
            return ndjson([{"error": RAW_ERR, "done": True}])
        if key == "模型不存在":
            return ndjson([{"error": 'model "foo:1b" not found, try pulling it first',
                            "done": True}])
        return ndjson([{"message": {"role": "assistant", "content": OK_REPLY}, "done": False},
                       {"message": {"role": "assistant", "content": ""},
                        "done": True, "done_reason": "stop"}])


def chat(prompt, timeout=150):
    """每次都用**全新连接**、一次性读完 —— 避免 keep-alive 读到上一个响应的残留。"""
    import http.client
    body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                       "stream": True, "session_id": "probe-" + prompt[:6]},
                      ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", PORT_APP, timeout=timeout)
    conn.request("POST", "/api/chat", body,
                 {"Content-Type": "application/json", "Connection": "close"})
    resp = conn.getresponse()
    raw_all = resp.read()
    conn.close()
    evs, text, errs, notes = [], [], [], []
    for raw in raw_all.split(b"\n"):
        s_ = raw.decode("utf-8", "ignore").strip()
        if not s_:
            continue
        try:
            ev = json.loads(s_)
        except Exception:
            continue
        evs.append(ev)
        if "error" in ev:
            errs.append(ev)
        if ev.get("note"):
            notes.append(ev["note"])
        m = ev.get("message")
        if isinstance(m, dict):
            if isinstance(m.get("content"), str):
                text.append(m["content"])
    return evs, "".join(text), errs, notes


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", PORT_STUB), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg["ollama_url"] = "http://127.0.0.1:%d" % PORT_STUB
    json.dump(cfg, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    env = dict(os.environ, MM_DATA_DIR=TMP, PYTHONIOENCODING="utf-8")
    logp = os.path.join(TMP, "srv.log")
    log = open(logp, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import uvicorn; from backend.main import app; "
         "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='warning')" % PORT_APP],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        base = "http://127.0.0.1:%d" % PORT_APP
        for _ in range(40):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=3).read()
                break
            except Exception:
                time.sleep(1)
        print("服务已就绪\n")

        for sec, prompt in (("3", "【瞬时坏一次】帮我看看这个"),
                            ("4", "【一直坏】帮我看看这个"),
                            ("5", "【模型不存在】帮我看看这个"),
                            ("6", "【普通提问】你好")):
            STUB_ROUNDS.clear()
            time.sleep(2.0)   # 让上一个请求在服务端彻底结束
            print("=== 第 %s 节：%s" % (sec, prompt))
            t0 = time.time()
            try:
                evs, text, errs, notes = chat(prompt)
                print("  用时 %.1fs｜事件 %d｜正文 %r" % (time.time() - t0, len(evs), text[:70]))
                print("  错误事件: %s" % [str(e)[:90] for e in errs])
                for _e in evs:
                    _k = list(_e.keys())[0] if _e else "?"
                    _m = _e.get("message") or {}
                    if _k == "message":
                        if _m.get("content_reset"):
                            print("    [ev] message.content_reset")
                        elif _m.get("content"):
                            print("    [ev] message.content=%r" % _m["content"][:50])
                        else:
                            print("    [ev] message=%s" % str(_m)[:60])
                    elif _k == "note":
                        print("    [ev] note=%r" % str(_e["note"])[:50])
                    elif _k == "done":
                        print("    [ev] done.text=%r" % str(_e.get("text"))[:60])
                    else:
                        print("    [ev] %s" % _k)
                print("  备注: %s" % [n[:50] for n in notes])
            except Exception as e:
                print("  请求异常: %s: %s" % (type(e).__name__, e))
            print("  stub 收到: %s\n" % STUB_ROUNDS)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        print("===== 服务日志（最后 40 行）=====")
        try:
            for ln in open(logp, encoding="utf-8", errors="replace").read().splitlines()[-40:]:
                print("  " + ln[:160])
        except Exception as e:
            print("  读日志失败:", e)


if __name__ == "__main__":
    main()
