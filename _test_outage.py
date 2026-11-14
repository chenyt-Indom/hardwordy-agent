# -*- coding: utf-8 -*-
"""服务器停机 12 秒再启动 → 客户端必须在退避上限内接回。"""
import json
import subprocess
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

lk = R.RelayLink("")
lk.gid = "outage-%d" % int(time.time())
lk.token = TOKEN
lk.cands = R.candidates_for(URL)
lk._stop = False
threading.Thread(target=lk._loop, daemon=True).start()
threading.Thread(target=lk._heartbeat_loop, daemon=True).start()

t0 = time.time()
while time.time() - t0 < 20 and lk.state != "online":
    time.sleep(0.05)
print("1) 初始连接：%s（走 %s）" % (lk.state, lk.active_url), flush=True)
if lk.state != "online":
    sys.exit(1)

ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
       "root@139.199.69.88"]

print("2) 停掉中继服务，模拟服务器故障…", flush=True)
subprocess.run(ssh + ["systemctl stop hive-relay"], capture_output=True, timeout=60)
ca0 = lk.connected_at
t_down = time.time()

# 记录掉线被察觉的时刻
t_notice = None
while time.time() - t_down < 30:
    if t_notice is None and lk.state != "online":
        t_notice = round(time.time() - t_down, 2)
    time.sleep(0.02)

print("3) 察觉断线：%s" % ("%.2fs" % t_notice if t_notice else "没抓到"), flush=True)
print("   当前状态：%s，尝试 %d 次，失败连击 %d"
      % (lk.state, lk.cand_tries, lk.fail_streak), flush=True)

print("4) 重新启动服务…", flush=True)
t_up = time.time()
subprocess.run(ssh + ["systemctl start hive-relay"], capture_output=True, timeout=60)

ok = False
while time.time() - t_up < 40:
    if lk.connected_at and lk.connected_at != ca0 and lk.state == "online":
        ok = True
        break
    time.sleep(0.02)
rec = round(time.time() - t_up, 2)
print("5) 恢复：%s（服务起来后 %.2fs 接回，走 %s）"
      % ("是" if ok else "否", rec, lk.active_url), flush=True)
print("   全程停机 %.1fs，累计尝试 %d 次" % (t_up - t_down, lk.cand_tries), flush=True)

lk._stop = True
print()
print("结果：%s" % ("通过" if ok and rec < 15 else "未通过"))
sys.exit(0 if ok and rec < 15 else 1)
