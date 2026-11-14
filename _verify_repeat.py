# -*- coding: utf-8 -*-
"""反复验证：连续多轮真连 + 多轮故障切换 + 全线路不通的回归。"""
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
TOKEN = cfg.get("token") or ""
URL = cfg.get("url") or ""


def one_round(cands, gid, timeout=25.0, stop_after_online=True):
    """起一个 link，等它上线，返回 (ok, 用时, active_url, 状态)。"""
    lk = R.RelayLink("")
    lk.gid = gid
    lk.token = TOKEN
    lk.cands = cands
    lk._stop = False
    threading.Thread(target=lk._loop, daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        if lk.state in ("online", "error"):
            break
        time.sleep(0.05)
    used = round(time.time() - t0, 2)
    st = lk.state
    au = lk.active_url
    if stop_after_online:
        lk._stop = True
        if lk.sock is not None:
            try:
                lk.sock.close()
            except Exception:
                pass
    return st == "online", used, au, st, lk


results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print("  [%s] %-40s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


# ================================================== 1 连续 12 轮真连
print("=" * 70)
print("1. 连续 12 轮真实连接（每轮全新对象，模拟反复断线重连）")
print("=" * 70)
times = []
succ = 0
for i in range(12):
    cands = R.candidates_for(URL)
    ok, used, au, st, lk = one_round(cands, "rep-%d-%d" % (i, int(time.time())))
    times.append(used)
    if ok:
        succ += 1
    print("  第%2d轮 %-4s %6.2fs  %s" % (i + 1, "通" if ok else "断", used, au),
          flush=True)
    time.sleep(0.15)
rec("1.1 12 轮全部连上", succ == 12, "%d/12，均 %.2fs，最慢 %.2fs"
    % (succ, sum(times) / len(times), max(times)))
rec("1.2 平均耗时 < 2 秒", (sum(times) / len(times)) < 2.0,
    "均 %.2fs" % (sum(times) / len(times)))

# ================================================== 2 故障切换 5 轮
print()
print("=" * 70)
print("2. 故障切换 5 轮（主线路写成死端口，必须自己找到活路）")
print("=" * 70)
fs = []
fok = 0
for i in range(5):
    dead = "ws://139.199.69.88:%d/hive-relay" % (9000 + i)
    cands = R.candidates_for(dead)
    ok, used, au, st, lk = one_round(cands, "fo-%d-%d" % (i, int(time.time())),
                                     timeout=30.0)
    fs.append(used)
    if ok:
        fok += 1
    print("  第%d轮 %-4s %6.2fs  %s" % (i + 1, "通" if ok else "断", used, au),
          flush=True)
    time.sleep(0.15)
rec("2.1 5 轮全部自动切换成功", fok == 5, "%d/5，均 %.2fs" % (fok, sum(fs) / len(fs)))
rec("2.2 切换平均 < 8 秒", (sum(fs) / len(fs)) < 8.0, "均 %.2fs" % (sum(fs) / len(fs)))

# ================================================== 3 全线路不通（回归：不许卡死）
print()
print("=" * 70)
print("3. 回归：所有线路都不通时，必须在有限时间内报错，不能卡死")
print("=" * 70)
bad = [
    {"host": "139.199.69.88", "port": 8080, "ws": True, "tls": False,
     "path": "/hive", "url": "ws://139.199.69.88:8080/hive"},   # 会挂 6 秒那种
    {"host": "139.199.69.88", "port": 18765, "ws": False, "tls": False,
     "path": "/hive-relay", "url": "hive4://139.199.69.88:18765"},
    {"host": "139.199.69.88", "port": 9998, "ws": True, "tls": False,
     "path": "/hive-relay", "url": "ws://139.199.69.88:9998/hive-relay"},
]
t0 = time.time()
ok, used, au, st, lk = one_round(bad, "bad-%d" % int(time.time()),
                                 timeout=60.0, stop_after_online=False)
total = round(time.time() - t0, 2)
lk._stop = True
rec("3.1 全部不通时正确报 error", st == "error", "state=%s" % st)
rec("3.2 没有永久卡死（< 60 秒内出结果）", total < 60.0, "总耗时 %.2fs" % total)
rec("3.3 错误信息可读", bool(lk.error), lk.error[:60])

# ================================================== 4 候选体检
print()
print("=" * 70)
print("4. 候选线路体检（probe_all 的实际数据）")
print("=" * 70)
cands = R.candidates_for(URL)
alive = 0
for p in cands:
    u = str(p.get("url") or "")
    try:
        r = R.probe(u, TOKEN, timeout=3.0)
    except Exception as e:
        r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if r.get("ok"):
        alive += 1
    print("  %-40s %-4s %7s ms  %s"
          % (u, "通" if r.get("ok") else "断", r.get("ms") or 0,
             (r.get("error") or "")[:50]), flush=True)
rec("4.1 至少有 2 条线路可用（有冗余）", alive >= 2, "%d/%d 条可用" % (alive, len(cands)))

# ================================================== 汇总
print()
print("=" * 70)
npass = sum(1 for _, o, _ in results if o)
print("汇总：%d/%d 通过" % (npass, len(results)))
for name, o, detail in results:
    if not o:
        print("  未通过：%s（%s）" % (name, detail))
print("=" * 70)
sys.exit(0 if npass == len(results) else 1)
