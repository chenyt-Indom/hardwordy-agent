"""Hive 中继 —— 一键部署到腾讯云（SSH 全自动）。

用法
----
1. 在项目里准备两个文件（都不会被提交、不会被打印）：

   data/deploy/server.json        服务器连接信息
   data/deploy/id_rsa             你的 SSH 私钥（OpenSSH 格式）
   data/hive/relay.json           中继密钥（应用里点「生成密钥」会自动写好）

   server.json 长这样：

       {
         "host": "1.2.3.4",
         "port": 22,
         "user": "ubuntu",
         "key": "data/deploy/id_rsa",
         "relay_port": 8765
       }

   腾讯云轻量应用服务器的默认用户名一般是 `ubuntu`（Ubuntu 镜像）或
   `root`（Debian / CentOS 镜像）；不确定就填 root。

2. 运行：

       python deploy/hive_relay_deploy.py            # 部署 + 自检
       python deploy/hive_relay_deploy.py --check    # 只看配置和连通性，不动服务器
       python deploy/hive_relay_deploy.py --restart  # 只重启服务

它会做什么
----------
· 探测服务器发行版（Ubuntu / Debian / CentOS / TencentOS 都支持）
· 确认 python3 存在（没有就装）
· 上传 backend/hive_relay_server.py 到 /opt/hive/
· 写 /opt/hive/token（权限 600，内容不打印、不落日志）
· 装 systemd 服务并设为开机自启
· 放行防火墙端口（ufw / firewalld）
· 从**本机**连一次公网地址做自检

密钥安全
--------
私钥只在 paramiko 里用，不打印、不复制到服务器。
中继密钥用 SFTP 直接写进 600 权限的文件，全程不出现在命令行里
（避免被 `ps` 看到）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SERVER_CFG = os.path.join(ROOT, "data", "deploy", "server.json")
RELAY_CFG = os.path.join(ROOT, "data", "hive", "relay.json")
RELAY_SRC = os.path.join(ROOT, "backend", "hive_relay_server.py")
WS_SRC = os.path.join(ROOT, "backend", "ws_min.py")
REMOTE_DIR = "/opt/hive"
SERVICE = "hive-relay"


def _log(msg: str) -> None:
    # Windows 控制台默认 GBK，打印 emoji / 部分符号会抛 UnicodeEncodeError
    # 直接把部署脚本崩掉（实测踩过）。这里降级成 '?' 而不是中断。
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
        sys.stdout.flush()


def load_server() -> dict:
    if not os.path.exists(SERVER_CFG):
        raise SystemExit(
            "缺少 %s\n"
            "请按下面的格式建一个（这是你服务器的连接信息，不会外传）：\n"
            '  {"host":"1.2.3.4","port":22,"user":"root",'
            '"key":"data/deploy/id_rsa","relay_port":8765}' % SERVER_CFG)
    with open(SERVER_CFG, "r", encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("host"):
        raise SystemExit("server.json 里缺 host")
    d.setdefault("port", 22)
    d.setdefault("user", "root")
    d.setdefault("relay_port", 8765)
    key = d.get("key") or "data/deploy/id_rsa"
    if not os.path.isabs(key):
        key = os.path.join(ROOT, key)
    d["key_path"] = key
    if not os.path.exists(key):
        raise SystemExit("找不到 SSH 私钥：%s" % key)
    return d


def load_token() -> str:
    if not os.path.exists(RELAY_CFG):
        raise SystemExit(
            "缺少 %s\n"
            "先在应用里打开「群聊 → 🌐 中继」，点「生成密钥」，会自动写到这个文件。" % RELAY_CFG)
    with open(RELAY_CFG, "r", encoding="utf-8") as f:
        d = json.load(f)
    tok = str(d.get("token") or "").strip()
    if not tok:
        raise SystemExit("relay.json 里没有 token，先在应用里生成一次")
    return tok


def connect(cfg: dict):
    try:
        import paramiko
    except Exception:
        raise SystemExit("缺 paramiko：pip install paramiko")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    _log("[1/7] 连接 %s@%s:%s ..." % (cfg["user"], cfg["host"], cfg["port"]))
    cli.connect(hostname=cfg["host"], port=int(cfg["port"]), username=cfg["user"],
                key_filename=cfg["key_path"], timeout=15,
                allow_agent=False, look_for_keys=False)
    _log("      连接成功")
    return cli


def run(cli, cmd: str, timeout: int = 120, quiet: bool = False):
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if not quiet and out.strip():
        for line in out.strip().splitlines()[:12]:
            _log("      " + line)
    return rc, out, err


def detect_os(cli) -> str:
    rc, out, _ = run(cli, "cat /etc/os-release 2>/dev/null | head -3", quiet=True)
    low = out.lower()
    for name, key in (("ubuntu", "ubuntu"), ("debian", "debian"),
                      ("tencentos", "tencentos"), ("centos", "centos"),
                      ("rocky", "centos"), ("almalinux", "centos"),
                      ("opencloudos", "centos"), ("kylin", "centos"),
                      ("uos", "debian")):
        if key in low:
            return name
    return "unknown"


def pkg_install_cmd(osname: str) -> str:
    if osname in ("ubuntu", "debian"):
        return "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3"
    return "yum install -y -q python3 || dnf install -y -q python3"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hive 中继一键部署")
    ap.add_argument("--check", action="store_true", help="只检查配置和连通性")
    ap.add_argument("--restart", action="store_true", help="只重启服务")
    ap.add_argument("--port", type=int, default=0, help="覆盖 relay_port")
    a = ap.parse_args(argv)

    cfg = load_server()
    token = load_token()
    if a.port:
        cfg["relay_port"] = a.port
    port = int(cfg["relay_port"])
    _log("服务器 %s:%s  中继端口 %d  密钥 %s***"
         % (cfg["host"], cfg["port"], port, token[:3]))

    if a.check:
        rc = _local_probe(cfg["host"], port)
        return rc

    if not os.path.exists(RELAY_SRC):
        raise SystemExit("找不到 %s" % RELAY_SRC)

    cli = connect(cfg)
    try:
        osname = detect_os(cli)
        _log("[2/7] 系统识别: %s" % osname)

        rc, out, _ = run(cli, "command -v python3 || true", quiet=True)
        if not out.strip():
            _log("[3/7] 没装 python3，正在安装 ...")
            rc, out, err = run(cli, pkg_install_cmd(osname), timeout=600)
            if rc != 0:
                _log("      安装失败: " + (err or out)[:300])
                return 1
        else:
            rc, ver, _ = run(cli, "python3 -V", quiet=True)
            _log("[3/7] python3 已就绪: %s" % ver.strip())

        _log("[4/7] 创建 %s ..." % REMOTE_DIR)
        run(cli, "mkdir -p %s/data && chmod 755 %s" % (REMOTE_DIR, REMOTE_DIR), quiet=True)

        _log("[5/7] 上传中继程序 ...")
        sftp = cli.open_sftp()
        try:
            sftp.put(RELAY_SRC, REMOTE_DIR + "/hive_relay_server.py")
            # ws_min.py 必须一起传：中继靠它支持 WebSocket（挂 nginx 反代用）。
            # 两个文件放同一目录，服务端 `import ws_min` 就能找到。
            sftp.put(WS_SRC, REMOTE_DIR + "/ws_min.py")
            # 密钥直接写文件，不走命令行（避免 ps 里看到）
            with sftp.open(REMOTE_DIR + "/token", "w") as f:
                f.write(token + "\n")
            try:
                sftp.chmod(REMOTE_DIR + "/token", 0o600)
            except Exception:
                pass
        finally:
            sftp.close()
        run(cli, "chmod 600 %s/token && chmod 644 %s/hive_relay_server.py %s/ws_min.py"
            % (REMOTE_DIR, REMOTE_DIR, REMOTE_DIR), quiet=True)

        _log("[6/7] 安装 systemd 服务 ...")
        unit = """[Unit]
Description=Hive Relay (group chat relay)
After=network.target

[Service]
Type=simple
WorkingDirectory=%(dir)s
ExecStart=%(py)s %(dir)s/hive_relay_server.py --host 0.0.0.0 --port %(port)d --data %(dir)s/data --token-file %(dir)s/token
Restart=always
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
""" % {"dir": REMOTE_DIR, "port": port, "py": "/usr/bin/python3"}
        sftp = cli.open_sftp()
        try:
            with sftp.open("/tmp/%s.service" % SERVICE, "w") as f:
                f.write(unit)
        finally:
            sftp.close()
        run(cli, "mv /tmp/%s.service /etc/systemd/system/%s.service" % (SERVICE, SERVICE), quiet=True)
        rc, out, err = run(cli, "systemctl daemon-reload && systemctl enable %s && systemctl restart %s && sleep 1 && systemctl is-active %s"
                        % (SERVICE, SERVICE, SERVICE))
        if "active" not in out:
            _log("      服务启动失败，最近日志：")
            run(cli, "journalctl -u %s -n 20 --no-pager" % SERVICE)
            return 1

        # 防火墙（服务器本机；腾讯云控制台的安全组仍需手动放行）
        if osname in ("ubuntu", "debian"):
            run(cli, "ufw allow %d/tcp >/dev/null 2>&1 || true" % port, quiet=True)
        else:
            run(cli, "(firewall-cmd --permanent --add-port=%d/tcp && firewall-cmd --reload) >/dev/null 2>&1 || true" % port, quiet=True)

        _log("[7/7] 从本机自检 ...")
        time.sleep(1.0)
        # 优先用应用里已经配好的地址：如果走的是 nginx 80/443 反代（ws://host/hive-relay），
        # 拿 hive4://host:18765 去测必然超时 —— 那不是部署失败，是测错了地方。
        probe_url, via = _pick_probe_url(cfg["host"], port)
        _log("      检测目标: %s%s" % (probe_url, "（%s）" % via if via else ""))
        ok = _local_probe(probe_url) == 0
        if not ok:
            _log("")
            if via:
                _log("⚠️  通过 %s 连不上。检查 nginx 反代是否配好：" % via)
                _log("   nginx -t && systemctl reload nginx")
                _log("   配置里应有 location %s → proxy_pass http://127.0.0.1:%d" % (via, port))
            else:
                _log("⚠️  服务器上服务起来了，但从公网连不上。最常见的原因是：")
                _log("   腾讯云控制台 → 轻量应用服务器 → 防火墙 → 添加规则 → 放行 TCP %d" % port)
                _log("   （轻量的防火墙在控制台里，服务器内的 ufw/firewalld 管不到它）")
                _log("   绕过办法：用 nginx 把 80 端口反代到 %d，然后把应用里的地址填成" % port)
                _log("   ws://%s/hive-relay（这样不用改安全组）" % cfg["host"])
            return 1

        _log("")
        _log("✅ 部署完成。应用里「群聊 → 🌐 中继」填：")
        _log("      %s" % probe_url)
        return 0
    finally:
        try:
            cli.close()
        except Exception:
            pass


def _local_probe(host: str, port: int) -> int:
    """从本机连一次公网中继，确认真的可用（不是只看服务进程活着）。"""
    try:
        from backend import hive_relay_link as L
    except Exception as e:
        _log("导入失败: %s" % e)
        return 1
    r = L.probe("hive4://%s:%d" % (host, port), timeout=6.0)
    if r.get("ok"):
        h = r.get("health") or {}
        _log("      ✅ 公网可达  %sms  版本=%s 房间=%s 在线=%s 累计帧=%s"
             % (r.get("ms"), h.get("ver"), h.get("rooms"), h.get("clients"), h.get("frames")))
        return 0
    _log("      ❌ 连不上: %s" % r.get("error"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
