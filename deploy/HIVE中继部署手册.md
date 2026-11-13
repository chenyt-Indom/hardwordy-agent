# Hive 群聊中继 —— 部署手册（腾讯云轻量应用服务器）

> 这份手册对应 `backend/hive_relay_server.py`（中继服务器）+
> `backend/hive_relay_link.py`（客户端链路）。
> 2026-11-14 恢复「服务器中转」模式：P2P 直连在跨网络时受家用宽带 NAT 限制，
> 一台公网服务器最稳。

---

## 一、先搞清楚它到底是什么

| 问题 | 答案 |
|---|---|
| 服务器能看到聊天内容吗 | **不能。** 转发的是 mesh 层的 AES-256-GCM 密文，群密钥只在成员之间，服务器上没有 |
| 需要备案吗 | **不需要。** 直接 IP + 端口，不走域名和 80/443 |
| 成员要装什么 | **什么都不用装**。只在应用里填一次地址 |
| 掉线会丢消息吗 | **不会。** 每帧带 seq 落盘，重连时按「我看到哪了」补发 |
| 实时性 | 收到就转发，不是轮询。实测同机延迟 **0.9 ms**，公网就是一次 TCP 往返 |

### 它会转发哪些内容

`hive_mesh` 里所有帧都走同一条通道，所以是**全量实时**的：

- 文字消息
- @hd 的流式输出（AI 边想边出字，逐帧推送）
- 附件文件（512KB 一片）
- 成员表变化 / 踢人
- 群被群主删除

---

## 二、买什么

腾讯云 **轻量应用服务器（Lighthouse）** 就够：

| 项 | 建议 | 说明 |
|---|---|---|
| 规格 | 2 核 2G | 中继只做转发，5~20 人这个规格绰绰有余 |
| 系统 | Ubuntu 22.04 / 24.04 | 脚本自动识别，Debian / TencentOS 也行 |
| 带宽 | 3~5 Mbps | 20 人发文字+偶尔传文件够用；经常传大文件选 5M 以上 |
| 地域 | 离成员近的 | 都在一起就选同一个城市 |
| 计费 | 按月/年 | 不需要公网带宽峰值，轻量的固定带宽更划算 |

> 用不到 1 核 1G 的就别买更小的 —— 1G 内存跑 Ubuntu 会吃紧。

---

## 三、部署（三种方式，选一种）

### 方式 A：全自动（推荐，本机一条命令）

**准备两个文件**（都在项目里，不会外传）：

1. `data/deploy/server.json` —— 服务器连接信息

```json
{
  "host": "1.2.3.4",
  "port": 22,
  "user": "ubuntu",
  "key": "data/deploy/id_rsa",
  "relay_port": 8765
}
```

- `user`：Ubuntu 镜像一般是 `ubuntu`，Debian/CentOS 是 `root`
- `key`：你的 SSH 私钥路径（相对项目根或绝对路径都行）

2. `data/hive/relay.json` —— 中继密钥

在应用里打开「群聊 → 🌐 中继」，地址先随便填，点保存，会**自动生成密钥**
并弹窗显示一次。也可以自己造一个 48 位随机串填进去：

```json
{"url": "hive4://1.2.3.4:8765", "token": "你自己的密钥", "enabled": true}
```

**然后执行**：

```bash
python deploy/hive_relay_deploy.py            # 部署 + 自检
python deploy/hive_relay_deploy.py --check    # 只看配置和连通性，不动服务器
python deploy/hive_relay_deploy.py --restart  # 只重启服务
```

它会依次做：

1. SSH 连接（私钥只在 paramiko 里用，不打印、不复制到服务器）
2. 识别发行版（Ubuntu/Debian → apt，CentOS/TencentOS → yum/dnf）
3. 确认 `python3`（没有就装）
4. 上传程序到 `/opt/hive/`
5. 用 SFTP 写 `/opt/hive/token`（权限 600）——**不走命令行**，避免被 `ps` 看到
6. 装 systemd 服务并开机自启
7. 放行本机防火墙（ufw / firewalld）
8. **从本机连一次公网地址自检**

看到 `✅ 部署完成` 就成了。

---

### 方式 B：服务器上一键脚本

把这两个文件用 WinSCP / scp 传到服务器同一目录（比如 `/root/hive/`）：

- `backend/hive_relay_server.py`
- `deploy/hive_relay_install.sh`

然后：

```bash
cd /root/hive
sudo bash hive_relay_install.sh 8765 '你的中继密钥'
```

脚本会自动识别系统、装 python3、装服务、放行防火墙、做本机自检，
最后打印出该填的地址。

---

### 方式 C：宝塔面板

1. 宝塔 → **文件** → 进 `/opt/hive/`（没有就新建），上传 `hive_relay_server.py`
2. 宝塔 → **终端**，执行：

```bash
cd /opt/hive
mkdir -p data
printf '%s\n' '你的中继密钥' > token
chmod 600 token
python3 -V || (apt update && apt install -y python3)
```

3. 宝塔 → **计划任务** 不适用，改用 **终端** 起 systemd 服务：

```bash
cat > /etc/systemd/system/hive-relay.service <<'EOF'
[Unit]
Description=Hive Relay
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/hive
ExecStart=/usr/bin/python3 /opt/hive/hive_relay_server.py --host 0.0.0.0 --port 8765 --data /opt/hive/data --token-file /opt/hive/token
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hive-relay
systemctl status hive-relay --no-pager
```

4. 宝塔 → **安全** → 放行 `8765`（TCP）

---

## 四、放行端口（**最容易卡住的一步**）

轻量应用服务器有**两道防火墙**，只开一边连不上：

1. **腾讯云控制台**（必须）
   轻量应用服务器 → 点你的实例 → **防火墙** → 添加规则 → TCP `8765`
2. **服务器内部**（脚本已自动处理）
   `ufw allow 8765/tcp` 或 `firewall-cmd --permanent --add-port=8765/tcp`

判断方法：在你自己电脑上 `telnet 你的IP 8765`，不通就是没放行。

---

## 五、在应用里填地址

左侧「群聊」→ 群聊头部 **🌐 中继**：

| 字段 | 填什么 |
|---|---|
| 地址 | `hive4://你的公网IP:8765` |
| 密钥 | 服务器上那个 token（**留空 = 不改**；第一次留空会自动生成） |

保存后会自动测一次连通性，并弹「服务器可达 xxx ms」。

**以后建群、入群会自动走中继**，不用每次再填 —— 这就是「一体化」：
配置只做一次，之后所有人只输群密码。

> 地址写 `ws://` 或直接写 `1.2.3.4:8765` 也能识别。
> 注意：本项目用的是自定义二进制协议，**不是 WebSocket**，写 `ws://` 只是习惯。

---

## 六、验证真的「实时同时分发」

在本机可以自己做，不用等第二台机器：

```bash
python deploy/hive_relay_deploy.py --check
```

要验证**多端同时收到**，开两台机器（或同一台开两个应用实例）加入同一个群，
一端发消息，看另一端是不是**立刻**出现（不是过几秒刷出来的）。

实测数据（本机双客户端，同一中继）：

```
A→B 即时转发      延迟 0.9 ms
转发延迟 < 100ms  PASS
发送者不收回声     PASS
新成员补齐历史     PASS（带 seq）
seq 单调递增       PASS
断线期间消息能补到 PASS
不同群不串台       PASS
重启后历史不丢     PASS
```

---

## 七、安全说明

| 项 | 做法 |
|---|---|
| 聊天内容 | AES-256-GCM 加密，服务器**没有密钥**，转发的是密文 |
| 中继密钥 | 存 `data/hive/relay.json`，`chmod 600`；接口只回显 `abc***` |
| 密钥传输 | **HMAC 挑战应答**，密钥本身从不在网络上传输，抓包也拿不到 |
| 服务器上的密钥 | `/opt/hive/token`，权限 600，用 SFTP 写入（不走命令行） |
| 错误密钥 | 直接拒绝并回明确原因，不会静默连上 |
| 慢客户端 | 丢最旧的帧而不是断开（断开重连再补会造成更大流量尖峰） |

> ⚠️ 传输层本身没加密（`hive4://` 是明文 TCP）。消息体是密文，所以内容安全；
> 但**中继密钥的 HMAC 应答**在理论上可被重放 —— 实际风险仅限于「有人往群里灌垃圾帧」，
> 他仍然解不开、也伪造不了任何消息。要更强就套 nginx + TLS，见下。

---

## 八、常见问题

**Q：连不上，提示「连不上 IP:8765」**
按顺序查：① 控制台防火墙放行了吗 ② `systemctl status hive-relay` 活着吗
③ 本机 `telnet IP 8765` 通不通 ④ 地址里的端口和实际一致吗。

**Q：提示「中继密钥不对」**
应用里填的密钥和服务器 `/opt/hive/token` 不一致。重新生成一次，两处都更新。

**Q：连上了但收不到消息**
确认两人加的是**同一个群**（比对群指纹），且都填了**同一个中继地址**。

**Q：服务器重启后消息还在吗**
在。日志在 `/opt/hive/data/<路由键>.log`，seq 从那里恢复。

**Q：日志太大**
默认超过 64MB 自动压缩，只保留最近 5000 帧。

**Q：想用域名 + HTTPS**
在 nginx 里加：

```nginx
stream {
    upstream hive { server 127.0.0.1:8765; }
    server {
        listen 8765 ssl;
        ssl_certificate     /etc/nginx/cert/fullchain.pem;
        ssl_certificate_key /etc/nginx/cert/privkey.pem;
        proxy_pass hive;
    }
}
```

（`stream` 模块做 TCP 透传，比 `location` 更适合自定义协议。
应用里地址写 `wss://域名:8765` 也能识别，但**实际连的仍是明文 TCP**，
真正的 TLS 要在客户端加一层——目前没做，需要的话告诉我。）

---

## 九、文件清单

| 文件 | 作用 |
|---|---|
| `backend/hive_relay_server.py` | 中继服务器（零依赖，只用标准库） |
| `backend/hive_relay_link.py` | 客户端链路（虚拟 peer / 自动重连 / seq 补发） |
| `backend/hive_client.py` | 门面层：配置读写、start/stop/status 接入中继 |
| `deploy/hive_relay_deploy.py` | 本机一键 SSH 部署 |
| `deploy/hive_relay_install.sh` | 服务器端一键安装 |
| `data/hive/relay.json` | 本机中继配置（含密钥，权限 600，勿外传） |
| `data/deploy/server.json` | 服务器连接信息（仅自动部署用） |
