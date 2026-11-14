# -*- coding: utf-8 -*-
"""多线路切换 / 心跳 真实验证。

三组测试：
  A. 本机起一个隔离中继，主线路指向死端口 → 必须自动切到活端口
  B. 公网真机：主线路指向死端口 → 必须自动切到 443/8080/80 中某条
  C. 心跳：人为把 last_rx 拨到 100 秒前 → 必须主动断开并重连

测试里把 MESH 换成哑对象，避免往 data/hive 写测试群。
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, ".")

import backend.hive_relay_link as R
import backend.hive_relay_server as SRV


# ---------------------------------------------------------------- 哑 MESH
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_state(lk, want, timeout=12.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if lk.state in want:
            return True, round(time.time() - t0, 2)
        time.sleep(0.05)
    return False, round(time.time() - t0, 2)


results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail))


# ================================================================ A 本机切换
print("=" * 66)
print("A. 本机隔离中继：主线路死 → 自动切备线")
print("=" * 66)

good_port = free_port()
tmp_data = os.path.join(os.environ.get("TEMP", "."), "_hive_test_data_%d" % good_port)
os.makedirs(tmp_data, exist_ok=True)
srv = SRV.RelayServer("127.0.0.1", good_port, tmp_data, "testtoken", 4)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
time.sleep(0.8)

dead_port = free_port()
lk = R.RelayLink("")
lk.gid = "testgid"
lk.token = "testtoken"
lk.cands = R.candidates_for("hive4://127.0.0.1:%d" % dead_port) + \
    R.candidates_for("hive4://127.0.0.1:%d" % good_port)
lk.cands = [c for c in lk.cands if not str(c.get("url", "")).startswith("ws")]
# 只留两条：死的、活的（避免 ws 派生干扰本机测试）
lk.cands = [
    {"host": "127.0.0.1", "port": dead_port, "ws": False, "tls": False,
     "path": "/hive-relay", "url": "hive4://127.0.0.1:%d" % dead_port},
    {"host": "127.0.0.1", "port": good_port, "ws": False, "tls": False,
     "path": "/hive-relay", "url": "hive4://127.0.0.1:%d" % good_port},
]
lk._stop = False
lt = threading.Thread(target=lk._loop, daemon=True)
lt.start()
ok, sec = wait_state(lk, ("online",))
record("A1 切到备用端口并上线", ok,
       "用时 %.2fs，active=%s" % (sec, lk.active_url))
record("A2 确实走的是备用线路", lk.active_url.endswith(str(good_port)),
       lk.active_url)
record("A3 累计尝试次数 >= 2", lk.cand_tries >= 2, "tries=%d" % lk.cand_tries)

# 关掉服务 → 应该掉线并开始重试
srv.shutdown()
time.sleep(0.5)
ok2, sec2 = wait_state(lk, ("error", "connecting"), timeout=10.0)
record("A4 服务端关掉后能察觉", ok2, "用时 %.2fs，state=%s" % (sec2, lk.state))
lk._stop = True
time.sleep(0.3)


# ================================================================ B 公网切换
print()
print("=" * 66)
print("B. 公网真机：主线路死 → 自动切 443/8080/80")
print("=" * 66)

with open(os.path.join("data", "hive", "relay.json"), "r", encoding="utf-8") as f:
    cfg = json.load(f)
real_token = cfg.get("token") or ""
print("  真实地址：%s" % cfg.get("url"))

lk2 = R.RelayLink("")
lk2.gid = "failover-probe-%d" % int(time.time())   # 临时群名，MESH 是哑的，不落盘
lk2.token = real_token
# 主线路故意写成死端口，看它能不能自己找到活路
lk2.cands = R.candidates_for("ws://139.199.69.88:9999/hive-relay")
print("  候选 %d 条：%s" % (len(lk2.cands), [c.get("url") for c in lk2.cands]))
lk2._stop = False
lt2 = threading.Thread(target=lk2._loop, daemon=True)
lt2.start()
ok, sec = wait_state(lk2, ("online",), timeout=25.0)
record("B1 主线路坏掉后仍能上线", ok,
       "用时 %.2fs，state=%s" % (sec, lk2.state))
record("B2 自动选中了真实可用线路", bool(lk2.active_url), lk2.active_url)
record("B3 走的不是那条死线路", ":9999" not in (lk2.active_url or ""),
       lk2.active_url)

# 断线重连：把当前 socket 打断，看它能不能自己回来
if lk2.sock is not None:
    try:
        lk2.sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
time.sleep(0.2)
ok, sec = wait_state(lk2, ("online",), timeout=20.0)
record("B4 连接被打断后自动恢复", ok, "用时 %.2fs" % sec)

# ================================================================ C 心跳
print()
print("=" * 66)
print("C. 心跳：假死链必须被主动断开")
print("=" * 66)

if lk2.sock is not None:
    hb_before = lk2.heartbeat_fail
    lk2.last_rx = time.time() - 100     # 假装 100 秒没收到任何包
    ok, sec = wait_state(lk2, ("connecting", "error"), timeout=12.0)
    record("C1 假死链被心跳识破并断开", ok,
           "用时 %.2fs，heartbeat_fail %d→%d" % (sec, hb_before,
                                                 lk2.heartbeat_fail))
    ok, sec = wait_state(lk2, ("online",), timeout=20.0)
    record("C2 断开后自动重连成功", ok, "用时 %.2fs" % sec)

lk2._stop = True
time.sleep(0.5)

# ================================================================ 汇总
print()
print("=" * 66)
npass = sum(1 for _, ok, _ in results if ok)
print("汇总：%d/%d 通过" % (npass, len(results)))
for name, ok, detail in results:
    if not ok:
        print("  未通过：%s（%s）" % (name, detail))
print("=" * 66)
sys.exit(0 if npass == len(results) else 1)
