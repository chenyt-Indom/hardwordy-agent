# -*- coding: utf-8 -*-
"""
腾讯云安全组自动放行工具（纯标准库，无需安装 SDK）

背景：这台 CVM 的公网 IP 是 NAT 映射（网卡上只有 10.1.0.10），
      入站包在虚拟网络层就被安全组拦掉，服务器本机 iptables 改不了。
      实测：iptables 对 5000 和 8000 都是 ACCEPT，但 5000 外部不通、8000 通
            → 唯一决定因素是云安全组。

凭据来源（按优先级自动探测，都不需要写在命令行里）：
  1. 环境变量  TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY
  2. tccli 配置  ~/.tccli/default.credential
  3. CVM 实例角色（metadata）  —— 推荐：控制台给实例绑一个 CAM 角色即可，永久免密钥

用法：
  python deploy/open_ports.py --list                  # 列出安全组和当前规则
  python deploy/open_ports.py --port 18765            # 放行 18765/tcp
  python deploy/open_ports.py --port 18765 --proto udp
  python deploy/open_ports.py --port 18765 --dry-run  # 只显示会做什么

如果三种凭据都没有，脚本会打印清楚的操作指引，不会静默失败。
"""
import os
import sys
import json
import time
import hmac
import hashlib
import argparse
import urllib.request
import urllib.error

REGION = "ap-guangzhou"
INSTANCE_ID = "ins-ohbz3pl2"
HOST = "cvm.tencentcloudapi.com"
SERVICE = "cvm"
VERSION = "2017-03-12"


# ---------------------------------------------------------------- 凭据探测
def _from_env():
    sid = os.environ.get("TENCENTCLOUD_SECRET_ID")
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY")
    if sid and skey:
        return {"SecretId": sid, "SecretKey": skey, "Token": os.environ.get("TENCENTCLOUD_TOKEN", "")}
    return None


def _from_tccli():
    p = os.path.expanduser("~/.tccli/default.credential")
    if not os.path.exists(p):
        return None
    try:
        cfg = json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        return None
    sid = cfg.get("secretId") or cfg.get("SecretId")
    skey = cfg.get("secretKey") or cfg.get("SecretKey")
    if sid and skey:
        return {"SecretId": sid, "SecretKey": skey, "Token": cfg.get("token", "")}
    return None


def _from_metadata():
    """CVM 实例角色：绑了 CAM 角色就能免密钥拿到临时凭据"""
    base = "http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/"
    try:
        role = urllib.request.urlopen(base, timeout=3).read().decode().strip()
        if not role or "<" in role:
            return None
        body = urllib.request.urlopen(base + role, timeout=3).read().decode()
        c = json.loads(body)
        if c.get("TmpSecretId"):
            return {"SecretId": c["TmpSecretId"], "SecretKey": c["TmpSecretKey"], "Token": c.get("Token", "")}
    except Exception:
        pass
    return None


def load_credential():
    for name, fn in (("环境变量", _from_env), ("tccli 配置", _from_tccli), ("实例角色", _from_metadata)):
        c = fn()
        if c:
            print(f"[凭据] 使用 {name}（SecretId: {c['SecretId'][:8]}***）")
            return c
    return None


# ---------------------------------------------------------------- TC3 签名
def _sign(cred, action, payload):
    ts = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    body = json.dumps(payload, separators=(",", ":"))
    ct = "application/json; charset=utf-8"

    canonical = "\n".join([
        "POST", "/", "",
        f"content-type:{ct}\nhost:{HOST}\n",
        "content-type;host",
        hashlib.sha256(body.encode()).hexdigest(),
    ])
    scope = f"{date}/{SERVICE}/tc3_request"
    to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(ts), scope,
        hashlib.sha256(canonical.encode()).hexdigest(),
    ])

    def _h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _h(f"TC3{cred['SecretKey']}".encode(), date)
    k = _h(k, SERVICE)
    k = _h(k, "tc3_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()

    auth = (f"TC3-HMAC-SHA256 Credential={cred['SecretId']}/{scope}, "
            f"SignedHeaders=content-type;host, Signature={sig}")
    headers = {"Authorization": auth, "Content-Type": ct, "Host": HOST,
               "X-TC-Action": action, "X-TC-Timestamp": str(ts),
               "X-TC-Version": VERSION, "X-TC-Region": REGION}
    if cred.get("Token"):
        headers["X-TC-Token"] = cred["Token"]
    return body, headers


def call(cred, action, payload):
    body, headers = _sign(cred, action, payload)
    req = urllib.request.Request(f"https://{HOST}", data=body.encode(), headers=headers, method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except urllib.error.HTTPError as e:
        return {"Error": {"Message": e.read().decode()[:300]}}
    if "Error" in resp.get("Response", {}):
        return {"Error": resp["Response"]["Error"]}
    return resp["Response"]


# ---------------------------------------------------------------- 主流程
def list_groups(cred):
    r = call(cred, "DescribeSecurityGroups", {"Limit": 20})
    if "Error" in r:
        print(f"[失败] {r['Error'].get('Message')}")
        return []
    gs = r.get("SecurityGroupSet", [])
    print(f"\n共 {len(gs)} 个安全组：")
    for g in gs:
        print(f"  {g['SecurityGroupId']}  {g.get('SecurityGroupName','')}  {g.get('SecurityGroupDesc','')}")
    return gs


def open_port(cred, sgid, port, proto, cidr, dry_run):
    r = call(cred, "DescribeSecurityGroupPolicies", {"SecurityGroupId": sgid})
    if "Error" in r:
        print(f"[失败] {r['Error'].get('Message')}")
        return False
    ingress = r.get("SecurityGroupPolicySet", {}).get("Ingress", [])
    print(f"\n当前入站规则 {len(ingress)} 条：")
    for p in ingress:
        print(f"  {p.get('Action'):6} {p.get('Protocol')} {p.get('Port')}  from {p.get('CidrBlock')}")

    exists = any(p.get("Protocol") == proto and p.get("Port") == str(port)
                 and p.get("CidrBlock") == cidr and p.get("Action") == "ACCEPT" for p in ingress)
    if exists:
        print(f"\n[已存在] {proto}:{port} from {cidr} 已在放行列表，无需重复添加")
        return True

    if dry_run:
        print(f"\n[演练] 将添加：ACCEPT {proto}:{port} from {cidr}")
        return True

    newrule = {"Action": "ACCEPT", "CidrBlock": cidr, "Protocol": proto,
               "Port": str(port), "Description": "hive-relay"}
    r = call(cred, "ModifySecurityGroupPolicies", {
        "SecurityGroupId": sgid,
        "SecurityGroupPolicySet": {"Version": r.get("SecurityGroupPolicySet", {}).get("Version"),
                                   "Ingress": ingress + [newrule]},
    })
    if "Error" in r:
        print(f"[失败] {r['Error'].get('Message')}")
        return False
    print(f"\n[成功] 已放行 {proto}:{port} from {cidr}")
    return True


def guide():
    print("""
没有找到可用的腾讯云凭据，因此无法通过 API 自动放行。

三个可选做法（任选其一）：

【推荐】给 CVM 实例绑定 CAM 角色 —— 一次操作，之后永久免密钥
  1. 打开 https://console.cloud.tencent.com/cam/role  新建角色 → 云服务 → CVM
  2. 给角色附加策略 QcloudCVMFullAccess（或自建只含安全组权限的策略）
  3. 打开 https://console.cloud.tencent.com/cvm/instance  找到 ins-ohbz3pl2
     更多 → 实例设置 → 绑定角色
  4. 回来重跑本脚本即可自动放行

【最快】控制台手动放行 30 秒
  https://console.cloud.tencent.com/cvm/securitygroup
  找到实例绑定的安全组 → 入站规则 → 添加规则
  协议端口：TCP:18765    来源：0.0.0.0/0    策略：允许

【临时】给脚本 API 密钥（不建议长期使用，密钥会落盘）
  Windows PowerShell:
    $env:TENCENTCLOUD_SECRET_ID="你的SecretId"
    $env:TENCENTCLOUD_SECRET_KEY="你的SecretKey"
    python deploy/open_ports.py --port 18765
""")
    print("说明：目前 4 条 nginx 线路已全部可用，18765 不放行也不影响群聊功能。\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出安全组")
    ap.add_argument("--port", type=int, help="要放行的端口")
    ap.add_argument("--proto", default="tcp", choices=["tcp", "udp"])
    ap.add_argument("--cidr", default="0.0.0.0/0")
    ap.add_argument("--sg", help="安全组 ID，不填则自动找实例绑定的")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cred = load_credential()
    if not cred:
        guide()
        return 2

    if a.list or not a.port:
        list_groups(cred)
        return 0

    sgid = a.sg
    if not sgid:
        r = call(cred, "DescribeInstances", {"InstanceIds": [INSTANCE_ID]})
        if "Error" in r:
            print(f"[失败] 查实例：{r['Error'].get('Message')}")
            return 1
        sets = r.get("InstanceSet", [])
        if not sets:
            print("[失败] 没查到实例")
            return 1
        ids = sets[0].get("SecurityGroupIds", [])
        if not ids:
            print("[失败] 实例没有绑定安全组")
            return 1
        sgid = ids[0]
        print(f"[安全组] 实例绑定的是 {sgid}")

    ok = open_port(cred, sgid, a.port, a.proto, a.cidr, a.dry_run)
    if ok and not a.dry_run:
        import socket
        print("\n验证公网可达性…")
        for i in range(3):
            try:
                s = socket.create_connection(("139.199.69.88", a.port), timeout=6)
                s.close()
                print(f"  TCP {a.port}: OPEN  放行生效")
                break
            except Exception as ex:
                print(f"  第{i+1}次: {type(ex).__name__}")
                time.sleep(2)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
