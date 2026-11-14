# -*- coding: utf-8 -*-
"""公网线路单条诊断：逐条试，打印每条的真实结果。"""
import json
import socket
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
print("token 长度 %d，gid=diag-%d" % (len(token), int(time.time())))
print()

for i, p in enumerate(cands):
    lk = R.RelayLink("")
    lk.gid = "diag-%d" % int(time.time())
    lk.token = token
    lk.state = "connecting"
    t0 = time.time()
    try:
        ok = lk._connect_once(p)
        err = lk.error
    except Exception:
        ok = False
        err = traceback.format_exc().strip().splitlines()[-1]
    ms = round((time.time() - t0) * 1000, 1)
    print("%d. %-38s %-4s %8s ms  state=%-10s %s"
          % (i + 1, p.get("url"), "OK" if ok else "NO", ms, lk.state, err))
    if ok and lk.sock is not None:
        try:
            lk.sock.close()
        except Exception:
            pass
    time.sleep(0.2)
