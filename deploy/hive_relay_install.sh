#!/usr/bin/env bash
# Hive 中继 —— 服务器端一键安装脚本（在服务器上执行，不在本机）
#
# 用法：
#   1) 把这两个文件传到服务器同一目录（比如 /root/hive/）：
#        hive_relay_server.py
#        hive_relay_install.sh
#   2) cd /root/hive && sudo bash hive_relay_install.sh 8765 '你的中继密钥'
#
# 自动识别 Ubuntu/Debian（apt）和 CentOS/TencentOS/OpenCloudOS（yum/dnf）。
# 装完会自动启动 + 开机自启，并在本机做一次自检。
set -u

PORT="${1:-8765}"
TOKEN="${2:-}"
DIR="/opt/hive"
SERVICE="hive-relay"
SRC="$(cd "$(dirname "$0")" && pwd)/hive_relay_server.py"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash $0）"
[ -f "$SRC" ] || die "同目录下找不到 hive_relay_server.py"
[ -n "$TOKEN" ] || die "缺少中继密钥。用法：sudo bash $0 $PORT '你的密钥'
   密钥在应用的「群聊 → 🌐 中继 → 生成密钥」里拿。"

say "== 1/6 识别系统 =="
OS="unknown"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in
    *ubuntu*|*debian*) OS="debian" ;;
    *centos*|*rhel*|*fedora*|*opencloud*|*tencentos*|*kylin*) OS="rhel" ;;
  esac
fi
say "   系统: ${PRETTY_NAME:-未知}  (包管理: $OS)"

say "== 2/6 确认 python3 =="
if ! command -v python3 >/dev/null 2>&1; then
  say "   没装，正在安装..."
  if [ "$OS" = "debian" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq python3
  else
    (yum install -y -q python3 || dnf install -y -q python3)
  fi
fi
command -v python3 >/dev/null 2>&1 || die "python3 安装失败"
say "   $(python3 -V)"

say "== 3/6 安装文件到 $DIR =="
mkdir -p "$DIR/data"
cp -f "$SRC" "$DIR/hive_relay_server.py"
chmod 644 "$DIR/hive_relay_server.py"
umask 077
printf '%s\n' "$TOKEN" > "$DIR/token"
chmod 600 "$DIR/token"
say "   程序与密钥已就位（token 权限 600）"

say "== 4/6 安装 systemd 服务 =="
PY="$(command -v python3)"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Hive Relay (group chat relay)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$PY $DIR/hive_relay_server.py --host 0.0.0.0 --port $PORT --data $DIR/data --token-file $DIR/token
Restart=always
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 1

say "== 5/6 放行本机防火墙 =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
  say "   ufw 已放行 ${PORT}/tcp"
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1
  say "   firewalld 已放行 ${PORT}/tcp"
else
  say "   没有 ufw/firewalld，跳过"
fi

say "== 6/6 自检 =="
if systemctl is-active --quiet "$SERVICE"; then
  say "   服务运行中 ✅"
else
  say "   服务没起来，最近日志："
  journalctl -u "$SERVICE" -n 20 --no-pager
  exit 1
fi
sleep 1
if python3 - "$PORT" <<'PY'
import socket, struct, sys, json
port = int(sys.argv[1])
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(5)
    hdr = s.recv(5)
    ln, kind = struct.unpack(">IB", hdr)
    body = b""
    while len(body) < ln:
        c = s.recv(ln - len(body))
        if not c: break
        body += c
    d = json.loads(body.decode())
    assert d.get("op") == "challenge"
    p = json.dumps({"op": "health"}).encode()
    s.sendall(struct.pack(">IB", len(p), 0) + p)
    hdr = s.recv(5)
    ln, kind = struct.unpack(">IB", hdr)
    body = b""
    while len(body) < ln:
        c = s.recv(ln - len(body))
        if not c: break
        body += c
    h = json.loads(body.decode())
    print("   本机自检通过 ✅ 版本=%s 房间=%s 在线=%s" % (h.get("ver"), h.get("rooms"), h.get("clients")))
except Exception as e:
    print("   本机自检失败: %s" % e)
    sys.exit(1)
PY
then
  IP="$(curl -s --max-time 4 ifconfig.me 2>/dev/null || echo '你的服务器IP')"
  say ""
  say "✅ 安装完成。"
  say "   应用里「群聊 → 🌐 中继」填：hive4://${IP}:${PORT}"
  say ""
  say "   ⚠️ 腾讯云轻量应用服务器还要去控制台放行端口："
  say "      轻量应用服务器 → 防火墙 → 添加规则 → TCP ${PORT}"
  say "      （控制台的防火墙独立于服务器内的 ufw，只开一边连不上）"
else
  exit 1
fi
