# -*- coding: utf-8 -*-
"""临时测试：密钥配置的后端函数（测完自动还原配置）"""
import os, sys, io, json, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import deploy_keys as dk

# ---------- 备份 ----------
bak = {}
for p in (dk.SERVER_CFG, dk.SSH_KEY, dk.RELAY_CFG):
    bak[p] = open(p, "rb").read() if os.path.exists(p) else None
print("已备份:", {os.path.basename(k): (len(v) if v else None) for k, v in bak.items()})

def restore():
    for p, b in bak.items():
        if b is None:
            if os.path.exists(p): os.remove(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b)
    print("已还原")

results = []
def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name + ("  " + detail if detail else ""))

try:
    # 1. 生成一把合法私钥
    import paramiko
    k = paramiko.RSAKey.generate(2048)
    tmp = os.path.join(tempfile.gettempdir(), "dp_valid_key2")
    k.write_private_key_file(tmp)
    valid = open(tmp, "r", encoding="utf-8").read()
    print("\n[1] 合法私钥", len(valid), "字节")

    # 2. 保存合法私钥
    r = dk.save_ssh_key(valid)
    print("[2] save_ssh_key ->", r)
    chk("保存合法私钥成功", bool(r.get("set") or r.get("saved") or r.get("ok")), str(r.get("hint", "")))

    # 3. 状态
    st = dk.ssh_key_status()
    print("[3] ssh_key_status ->", st)
    chk("状态为已配置", st.get("set") is True)
    chk("格式校验通过", st.get("valid") is not False, "fingerprint=" + str(st.get("fingerprint")))

    # 4. 回读明文
    txt = dk.reveal_ssh_key()
    chk("明文回读一致", txt == valid, "len=" + str(len(txt)))

    # 5. 拒绝假密钥
    bad = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
    rejected = None
    try:
        r2 = dk.save_ssh_key(bad)
        rejected = (r2.get("ok") is False)
    except ValueError as e:
        rejected = True
        print("[5] 保存假私钥 raise ValueError ->", str(e)[:60])
    chk("假密钥被拒绝", rejected)
    chk("拒绝后原私钥未被破坏", dk.reveal_ssh_key() == valid)

    # 6. 空值
    rej2 = None
    try:
        r3 = dk.save_ssh_key("")
        rej2 = (r3.get("ok") is False)
        print("[6] 空私钥 ->", r3)
    except ValueError as e:
        rej2 = True
        print("[6] 空私钥 raise ValueError ->", str(e)[:60])
    chk("空私钥被拒绝", rej2)

    # 7. 中继密钥
    tok = dk.reveal_relay_token()
    print("[7] 当前中继密钥长度:", len(tok or ""))
    r4 = dk.save_relay_token(tok)
    print("[7] save_relay_token ->", r4)
    chk("中继密钥保存成功", bool(r4.get("ok") or r4.get("set") or r4.get("saved")), str(r4.get("hint", "")))
    tst = dk.relay_token_status()
    print("[7] relay_token_status ->", tst)
    chk("中继密钥状态已配置", tst.get("set") is True)
    chk("中继密钥回读一致", dk.reveal_relay_token() == tok)

    # 8. 打码
    ov = dk.overview()
    print("[8] overview ->", json.dumps(ov, ensure_ascii=False)[:400])
    hint = (ov.get("relay_token") or {}).get("hint") or ""
    chk("中继密钥有打码提示", bool(hint) and ("*" in hint), hint)
    # preview 只应含 PEM 头那一行；真正的密钥正文（第 2 行）绝不能出现在 overview 里
    body_line = [ln for ln in valid.strip().splitlines() if ln.strip()][1][:24]
    ovs = json.dumps(ov, ensure_ascii=False)
    chk("overview 不含私钥正文", body_line not in ovs, "查:" + body_line[:12] + "...")
    chk("overview 的 preview 只有 PEM 头",
        (ov.get("ssh_key") or {}).get("preview", "").startswith("-----BEGIN"),
        (ov.get("ssh_key") or {}).get("preview", ""))
    chk("三项齐全 ready", ov.get("ready") is True, "missing=" + str(ov.get("missing")))

    # 9. 服务器信息
    s = dk.server_config()
    print("[9] server_config ->", s)
    chk("服务器信息可读", bool(s.get("host")), s.get("host", ""))
    r5 = dk.save_server_config(s.get("host") or "139.199.69.88",
                               s.get("port", 22), s.get("user", "root"),
                               s.get("relay_port", 18765))
    chk("服务器信息可写", r5.get("ok") is True, str(r5.get("error", "")))

finally:
    restore()

# ---------- 汇总 ----------
okn = sum(1 for _, c, _ in results if c)
print("\n===== 结果 %d/%d 通过 =====" % (okn, len(results)))
for n, c, d in results:
    if not c:
        print("  FAIL:", n, d)
