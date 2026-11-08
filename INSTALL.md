# 本地多模态助手 · 安装说明

> Windows 免安装版（约 100 MB）。**不含 AI 模型**——模型由 Ollama 单独下载。

## 这个包包含什么

| 有 | 无 |
|---|---|
| 对话 / 图片理解 | ❌ 文生图（需 torch + SD 模型约 10GB） |
| 知识库 / 分层记忆 | ❌ 随包分发的模型权重 |
| 文件工具 / 代码执行 | |
| 联网搜索 / 地图 / 天气 | |
| 语音唤醒与输入 | |
| **DeepSeek API 双通道** | |

---

## 第 0 步：确认系统

- **系统**：Windows 10 / 11（64 位）
- **磁盘**：至少 **25 GB** 空闲（模型 18 GB + 应用 1 GB + 余量）
- **内存**：建议 16 GB 以上
- **显卡**：可选。有 NVIDIA 显卡（8 GB+ 显存）会快 5-10 倍

---

## 第 1 步：装 Ollama

下载安装：**https://ollama.com/download/windows**

装完在 PowerShell 里确认：

```powershell
ollama --version
```

---

## 第 2 步：下载模型

打开 PowerShell，二选一：

```powershell
# ① 主力模型：30B（约 18 GB，能力强，12GB 显存会有部分卸载到内存，约 5-10 tok/s）
ollama pull qwen3-vl:30b

# ② 轻量模型：8B（约 6 GB，速度快 5 倍，30-50 tok/s）
ollama pull qwen3-vl:8b
```

> 网络慢可以设镜像（把 `your-mirror` 换成可用的加速地址）：
> ```powershell
> $env:OLLAMA_HOST="https://your-mirror"
> ```

---

## 第 3 步：解压运行

1. 解压 `本地多模态助手-Windows-x64.zip`（**整个文件夹一起解压，不要只拿 exe**）
2. 双击 **`本地多模态助手.exe`**
3. 等 3-10 秒，桌面弹出应用窗口（不是浏览器）

---

## 第 4 步：确认模型状态

看左侧状态卡片：

| 显示 | 含义 | 怎么办 |
|---|---|---|
| **模型就绪（离线）** | 一切正常 | 直接用 |
| **Ollama 在线，模型未下载** | 模型还没下 | 点下面的 **`⬇ 下载模型`**（内置下载器，有进度条，可取消/续传） |
| **Ollama 未运行** | Ollama 服务没起 | PowerShell 跑 `ollama serve`，或重启托盘图标 |

---

## 可选：接 DeepSeek API（不占显卡，最快）

1. 点右上角 **🔑 DeepSeek**
2. 去 **https://platform.deepseek.com/api_keys** 创建 Key
3. 粘进弹窗，点 **保存并校验**（会显示余额）
4. 顶部下拉选 **⚡ DeepSeek V4.1 Flash**

**好处**：不占显存、响应快、几块钱能用很久。配合本地模型切换使用最佳。

> Key 只存本标签页（sessionStorage），关掉浏览器即清空；不写磁盘、不进日志。

---

## 常见问题

**Q: 双击 exe 没反应 / 闪退？**
- 确认是**整个文件夹**解压（`_internal` 目录必须和 exe 同级）
- 杀毒软件可能误删了 `_internal` 里的 dll，关掉杀软重新解压

**Q: 提示"Ollama 未运行"？**
- 在 PowerShell 跑 `ollama serve`
- 或检查任务栏 Ollama 图标 → 右键 → 启动

**Q: 文生图用不了？**
- 轻量版不含 torch / Stable Diffusion（约 10 GB）
- 替代方案：用 DeepSeek 通道 + `web_image_search`（联网搜真实图片）
- 需要真文生图 → 用完整版

**Q: 模型下载很慢 / 失败？**
- 用应用内的 `⬇ 下载模型`（支持断点续传，失败可重试）
- 或者先在 PowerShell 里 `ollama pull`，应用会自动识别

**Q: 端口 8000 被占用？**
- 关掉其他占用 8000 的程序
- 或者：应用会自动复用已在运行的实例

---

## 目录说明（解压后）

```
本地多模态助手/
├── 本地多模态助手.exe        ← 双击运行
├── INSTALL.md               ← 本文档
└── _internal/               ← 运行时依赖（不要删、不要移动）
    ├── frontend/            ← 界面
    ├── python*.dll
    └── ...
```

---

## 卸载

直接删除整个文件夹即可。模型在 `C:\Users\<你>\.ollama\models`（或自定义目录），按需清理：

```powershell
ollama rm qwen3-vl:30b
```
