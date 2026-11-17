# 全栈快照 · 回滚机制 升级包

适用：本应用的任意实例（Windows）。**只新增文件，不修改被升级项目的任何现有文件。**

## 一、包里有什么

| 文件 | 作用 |
|---|---|
| `rescue.py` | 快照 / 回滚引擎（纯标准库，不 import backend） |
| `rollback_console.py` | 浏览器控制台（独立运行，不改对方前端/后端） |
| `start_console.bat` | 双击启动控制台 |
| `INSTALL_CN.md` | 本说明 |

## 二、安装（三步）

1. 把 `rescue.py` 和 `rollback_console.py` 复制到**对方项目的根目录**
   （根目录 = 有 `backend/` 和 `frontend/` 的那一层，通常是 `run.py` 所在目录）
2. 双击 `start_console.bat`（或 `python rollback_console.py`）
3. 浏览器自动打开 `http://127.0.0.1:8123`，能看到「全栈快照 · 回滚控制台」即安装成功

> 不需要改 `main.py`、`app.js`、启动脚本；不需要装任何第三方库；不需要 git。

## 三、怎么用

- **＋ 立即快照**：给当前状态存一份（会弹框让你起名字），手动快照独立保留 5 个
- **回滚**：点任意一行右侧的按钮，回到那个版本。回滚前会自动把当前状态存成
  「回滚前备份」，点它就能退回来
- **⟲ 回滚到最近健康版**：从新到旧逐个试，起不来的自动跳过

## 四、命令行用法（不开界面也行）

```
python rollback_console.py                 # 启动浏览器控制台（推荐）

python rescue.py --snapshot --tag "改代码前"  # 建快照
python rescue.py --list                      # 列出快照
python rescue.py --precheck                  # 只检查代码能否 import
python rescue.py --deep-check                # 真起一次服务验证（临时端口 8011）
python rescue.py --rollback --snap <文件名>   # 回滚到指定快照（加 --force 可强制）
python rescue.py --auto                      # 自动回滚到最近健康版
python rescue.py --health                    # 查主程序健康状态
python rescue.py --clean-snapshots           # 按配额清理旧快照
```

## 五、快照里有什么（重要）

**包含**：`backend/`、`frontend/`、`launcher/` 下的源码，根目录的 py/bat/json/md，
以及 `data/` 下的状态文件（技能、定时任务、自迭代计划等）。

**不包含**（有意排除）：
- `secrets.json` —— 回滚旧密钥会让当前可用的 API 直接失效
- `models/` 模型权重、`sessions/` 聊天记录、图片与媒体缓存

保留策略：自动快照 8 个、回滚前备份 2 个、手动快照 5 个，超出按时间从旧到新清理。

## 六、注意事项

1. **回滚会把 `data/` 状态一起退回快照时刻**，快照之后新建的技能/定时任务会消失。
2. 回滚后**必须重启主程序**才生效。
3. 主程序正在正常运行（8000 端口通）时，控制台回滚会带 `force` 强制放行；
   若担心，先关掉主程序再回滚。
4. 程序彻底起不来、连控制台都打不开时，用命令行 `python rescue.py --auto` 兜底
   （它只依赖标准库，不依赖主程序）。
5. 首次使用**没有任何快照**，请先点「＋ 立即快照」建一个基线，否则没东西可回滚。
