# Hive 中继部署（放到你的腾讯云服务器）

中继只做两件事：**转发密文** + **给消息定序**。
它看不到任何聊天内容——群密钥只在成员之间通过二维码传递，服务器上没有。

## 一、上传文件

只需要一个文件：`backend/hive_relay.py`

用 scp / 宝塔面板 / WinSCP 都行，传到服务器任意目录，比如 `/opt/hive/`。

## 二、装依赖并启动

```bash
cd /opt/hive
pip3 install websockets
python3 hive_relay.py --host 0.0.0.0 --port 8765 --data /opt/hive/data
```

想后台常驻：

```bash
nohup python3 hive_relay.py --host 0.0.0.0 --port 8765 --data /opt/hive/data > hive.log 2>&1 &
```

## 三、放行端口

腾讯云控制台 → 安全组 → 入站规则 → 放行 **TCP 8765**。
服务器本机防火墙（Ubuntu）：

```bash
sudo ufw allow 8765/tcp
```

## 四、在应用里填地址

左侧「群聊」模式 → 协作群面板 → **⋯** → **🌐 中继地址** → 填：

```
ws://你的服务器公网IP:8765
```

保存后，每个群成员都要各自填一次（各自连同一台中继）。

## 五、验证连上了

协作群面板的 **⋯** 菜单里有一行状态，应显示「🟢 已连接中继 · ws://…」。
群聊界面顶部会显示群指纹和成员数。

## 六、建议（生产环境）

直接暴露 `ws://` 是明文传输（消息体本身是加密的，但连接不加密）。
更稳妥的做法是前面挂 nginx + TLS，用 `wss://`：

```nginx
location /hive {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

然后应用里填 `wss://你的域名/hive`。

## 常见问题

- **连不上**：先 `telnet 你的IP 8765`，不通就是安全组没放行。
- **能连但收不到消息**：确认两个人填的是**同一个中继地址**，并且加入的是**同一个群**（比对群指纹是否一致）。
- **中继重启后消息还在吗**：在。密文日志存在 `--data` 指定的目录里，seq 也从那里恢复。
