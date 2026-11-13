# Hive 中继部署（旧说明，已迁移）

> **2026-11-14 更新**：这份文档原来对应 `backend/hive_relay.py`，
> 那个文件曾在「改纯 P2P」时被删除，现在中继以新的形式回来了，
> 文件名和协议都变了。

**请改看新文档**：`deploy/HIVE中继部署手册.md`

## 变了什么

| 项 | 旧（hive_relay.py） | 新 |
|---|---|---|
| 服务器程序 | `hive_relay.py`，依赖 `websockets` | `backend/hive_relay_server.py`，**零依赖**（只用标准库） |
| 协议 | WebSocket | 自定义定长头（`[4B 长度][1B 类型][载荷]`） |
| 客户端 | `hive_client` 直连 ws:// | `backend/hive_relay_link.py`，作为**虚拟 peer** 挂进 mesh |
| 鉴权 | 无（任何知道地址的人都能连） | HMAC 挑战应答，密钥**不在网络上传输** |
| 补发 | 密文日志 | seq + 落盘 + 断线按水位补发 |
| 部署 | 手工 scp + nohup | `deploy/hive_relay_deploy.py` 一条命令全自动 |

## 为什么换掉 WebSocket

服务器上少一个 pip 依赖，就少一个「部署时卡住」的理由。
自定义协议的帧就是 mesh 层原样的字节，中继**完全不需要解析**——
它连「这是文字还是文件」都不知道，只按路由键转发。

## 一分钟上手

```bash
# 本机（项目根目录）
python deploy/hive_relay_deploy.py
```

然后应用里「群聊 → 🌐 中继」填 `hive4://你的IP:8765`。
详见 `deploy/HIVE中继部署手册.md`。
