# -*- coding: utf-8 -*-
"""单次建连诊断：把 _connect_once 的真实失败原因打出来。"""
import os
import socket
import sys
import threading
import time
import traceback

sys.path.insert(0, ".")
import backend.hive_relay_link as R
import backend.hive_relay_server as SRV


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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


port = free_port()
tmp = os.path.join(os.environ.get("TEMP", "."), "_hive_dbg_%d" % port)
os.makedirs(tmp, exist_ok=True)
srv = SRV.RelayServer("127.0.0.1", port, tmp, "testtoken", 4)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.8)

lk = R.RelayLink("")
lk.gid = "testgid"
lk.token = "testtoken"
lk.state = "connecting"
p = {"host": "127.0.0.1", "port": port, "ws": False, "tls": False,
     "path": "/hive-relay", "url": "hive4://127.0.0.1:%d" % port}

print("端口 %d，token=testtoken" % port)
try:
    ok = lk._connect_once(p)
    print("_connect_once -> %s" % ok)
except Exception:
    print("_connect_once 抛异常：")
    traceback.print_exc()
print("state=%s error=%r" % (lk.state, lk.error))
print("active_url=%r" % lk.active_url)
print("sock=%r" % (lk.sock,))
lk._stop = True
srv.shutdown()
