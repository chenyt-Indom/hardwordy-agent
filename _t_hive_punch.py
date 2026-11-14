"""打洞通道实测：本机两节点 UDP 打洞 + 双向收发 + 重复打洞。"""
import os, sys, json, time, socket, threading
ROOT = r"D:\local-multimodal-src"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import backend.hive_punch as HP

res = []


def chk(name, ok, detail=""):
    res.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  " + str(detail) if detail else ""))


a = HP.PunchNode()
b = HP.PunchNode()
ra = a.start(0)
rb = b.start(0)
print("A 端口", ra.get("port"), " B 端口", rb.get("port"))
chk("两节点启动", ra.get("ok") and rb.get("ok"))

# 本机测试用 127.0.0.1；真实跨网用 STUN 映射到的公网地址
pa = ra.get("port")
pb = rb.get("port")

got_b = []


def _on_tun(sock, addr):
    def _rd():
        try:
            got_b.append(sock.recv(64))
        except Exception as e:                                # noqa: BLE001
            got_b.append(b"ERR:" + str(e).encode()[:40])
    threading.Thread(target=_rd, daemon=True).start()


b.on_tunnel = _on_tun


def _brief(d):
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in ("sock", "tunnel")}

t0 = time.time()
r1 = a.connect("127.0.0.1", pb, timeout=8.0)
dt1 = (time.time() - t0) * 1000
print("第一次打洞:", json.dumps(r1, ensure_ascii=False)[:300])
chk("第一次打洞", r1.get("ok"), "%.0f ms" % dt1)

tun = a.tunnel_for(("127.0.0.1", pb))
chk("拿到隧道", tun is not None)
if tun:
    tun.sendall(b"hello-punch")
    time.sleep(0.4)
    chk("A->B 数据到达", len(got_b) > 0 and got_b[0][:11] == b"hello-punch", got_b)

# 重复打洞（上一轮修的就是「第二次必然失败」）
time.sleep(0.3)
t0 = time.time()
r2 = a.connect("127.0.0.1", pb, timeout=8.0)
dt2 = (time.time() - t0) * 1000
print("第二次打洞:", json.dumps(r2, ensure_ascii=False)[:300])
chk("第二次打洞（回归项）", r2.get("ok"), "%.0f ms" % dt2)

# STUN 映射（真实跨网必需）
m = a.mapped(timeout=3.0)
print("A 的 STUN 映射:", m)
chk("STUN 拿到公网映射", bool(m and m.get("host")), m)

st = a.status()
print("A status:", json.dumps(st, ensure_ascii=False)[:400])

a.stop()
b.stop()
ok_n = sum(1 for _, o, _ in res if o)
print("\n===== %d/%d 通过 =====" % (ok_n, len(res)))
