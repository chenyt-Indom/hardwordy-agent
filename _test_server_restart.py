# -*- coding: utf-8 -*-
"""真实验证：服务器端中继重启 → 客户端必须自动重连回来。"""
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
lk.gid = "restart-test-%d" % int(time.time())
lk.token = TOKEN
lk.cands = R.candidates_for(URL)
lk._stop = False
threading.Thread(target=lk._loop, daemon=True).start()
threading.Thread(target=lk._heartbeat_loop, daemon=True).start()


def wait_state(want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if lk.state in want:
            return True, round(time.time() - t0, 2)
        time.sleep(0.05)
    return False, round(time.time() - t0, 2)


ok, sec = wait_state(("online",), 20)
print("1) 初始连接：%s（%.2fs，走 %s）"
      % ("通" if ok else "断", sec, lk.active_url), flush=True)
if not ok:
    sys.exit(1)

ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
       "root@139.199.69.88"]
print("2) 重启服务器端中继服务…", flush=True)
t_restart = time.time()
p = subprocess.run(ssh + ["systemctl restart hive-relay; sleep 1; systemctl is-active hive-relay"],
                   capture_output=True, text=True, timeout=60)
print("   服务状态：%s" % (p.stdout or "").strip(), flush=True)

# 3) 等它自己发现断线
ok, sec = wait_state(("connecting", "error"), 30)
print("3) 察觉断线：%s（%.2fs）state=%s" % ("是" if ok else "否", sec, lk.state), flush=True)

# 4) 等它自己连回来
ok, sec = wait_state(("online",), 60)
total = round(time.time() - t_restart, 2)
print("4) 自动恢复：%s（重启后 %.2fs 回到 online，走 %s）"
      % ("是" if ok else "否", total, lk.active_url), flush=True)
print("   累计尝试 %d 次，失败连击 %d" % (lk.cand_tries, lk.fail_streak), flush=True)

lk._stop = True
ok2 = ok and total < 45
print()
print("结果：%s" % ("通过 —— 服务器重启后自动恢复" if ok2 else "未通过"))
sys.exit(0 if ok2 else 1)
