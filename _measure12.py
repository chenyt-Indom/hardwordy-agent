# -*- coding: utf-8 -*-
"""连续 12 轮连接：只输出汇总数字。"""
import json
import sys
import threading
import time

sys.path.insert(0, ".")
import backend.hive_relay_link as R


class _N:
    started = True
    peers = []

    def start(self, *a, **k):
        pass

    def add_group(self, *a, **k):
        pass

    def _dispatch(self, *a, **k):
        pass


class _M:
    PROTO_VER = 1

    def __init__(self):
        self._LOCK = threading.Lock()

    def node(self):
        return _N()

    @staticmethod
    def _rk(g):
        return b"r" * 16

    @staticmethod
    def _open_frame(b, k):
        return None


R.MESH = _M()

with open("data/hive/relay.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)
TOKEN, URL = cfg.get("token") or "", cfg.get("url") or ""

times, paths, fails = [], [], 0
for i in range(12):
    lk = R.RelayLink("")
    lk.gid = "rep2-%d-%d" % (i, int(time.time()))
    lk.token = TOKEN
    lk.cands = R.candidates_for(URL)
    lk._stop = False
    threading.Thread(target=lk._loop, daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < 20:
        if lk.state in ("online", "error"):
            break
        time.sleep(0.02)
    times.append(round((time.time() - t0) * 1000, 1))
    paths.append(lk.active_url)
    if lk.state != "online":
        fails += 1
    lk._stop = True
    if lk.sock is not None:
        try:
            lk.sock.close()
        except Exception:
            pass
    time.sleep(0.1)

print("成功 %d/12" % (12 - fails))
print("耗时(ms)：%s" % times)
print("最快 %.0f ms / 最慢 %.0f ms / 平均 %.0f ms"
      % (min(times), max(times), sum(times) / len(times)))
from collections import Counter
print("走的线路分布：%s" % dict(Counter(paths)))
