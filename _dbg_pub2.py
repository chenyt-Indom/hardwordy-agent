# -*- coding: utf-8 -*-
"""公网线路逐条诊断（每条硬超时 10 秒，绝不卡死）。"""
import json
import sys
import threading
import time
import traceback

sys.path.insert(0, ".")
import backend.hive_relay_link as R


class _DummyNode:
    def __init__(self):
        self.started = True
        self.peers = []

    def start(self, *a, **k):
        pass

    def add_group(self, *a, **k):
        pass

    def _dispatch(self, *a, **k):
        pass


class _DummyMesh:
    PROTO_VER = 1

    def __init__(self):
        self._LOCK = threading.Lock()
        self._n = _DummyNode()

    def node(self):
        return self._n

    @staticmethod
    def _rk(gid):
        return b"r" * 16

    @staticmethod
    def _open_frame(body, key):
        return None


R.MESH = _DummyMesh()

with open("data/hive/relay.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)
token = cfg.get("token") or ""

cands = R.candidates_for("ws://139.199.69.88/hive-relay")
print("token 长度 %d" % len(token), flush=True)

for i, p in enumerate(cands):
    box = {}

    def run(p=p, box=box):
        lk = R.RelayLink("")
        lk.gid = "diag-%d" % int(time.time() * 1000)
        lk.token = token
        lk.state = "connecting"
        try:
            box["ok"] = lk._connect_once(p)
            box["err"] = lk.error
            box["state"] = lk.state
            box["sock"] = lk.sock
        except Exception:
            box["ok"] = False
            box["err"] = traceback.format_exc().strip().splitlines()[-1]
            box["state"] = "exc"

    t0 = time.time()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(10.0)
    ms = round((time.time() - t0) * 1000, 1)
    if th.is_alive():
        print("%d. %-38s 卡住 >10s  ← 这条会让握手无限等待"
              % (i + 1, p.get("url")), flush=True)
        continue
    print("%d. %-38s %-4s %8s ms  state=%-10s %s"
          % (i + 1, p.get("url"), "OK" if box.get("ok") else "NO", ms,
             box.get("state"), box.get("err") or ""), flush=True)
    s = box.get("sock")
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    time.sleep(0.2)

print("done", flush=True)
