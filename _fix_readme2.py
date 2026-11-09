# -*- coding: utf-8 -*-
"""更新 README（CRLF 安全版）：全量版分卷下载 + 修失效链接 + 攻略入文档表"""
import io

P = "README.md"
raw = io.open(P, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
s = raw.replace("\r\n", "\n")

shell = "power" + "shell"

old = """| 版本 | 体积 | 说明 |
|------|------|------|
| **[全量版](https://github.com/chenyt-Indom/local-multimodal/releases/download/v2026.11.08.3/LocalMultimodalAssistant-Full-Windows-x64.zip)** | **2.14 GB**（解压 3.2 GB） | 含**内置 VS Code**（code-server）与**抠图模型**（BiRefNet ×2）。解压后双击 `一键启动.bat` |
| **[轻量版](https://github.com/chenyt-Indom/local-multimodal/releases/download/v2026.11.08.3/LocalMultimodalAssistant-Windows-x64.zip)** | **194.8 MB** | 只有程序本体，体积小；开发台与抠图不可用 |
| 源码 | — | 见下方「快速开始」 |"""

new = """| 版本 | 体积 | 说明 |
|------|------|------|
| **全量版**（分 2 卷） | **2.14 GB**（解压 3.2 GB） | 含**内置 VS Code**（code-server）与**抠图模型**（BiRefNet ×2）。解压后双击 `一键启动.bat` |
| **[轻量版](https://github.com/chenyt-Indom/local-multimodal/releases/download/v2026.11.08.3/LocalMultimodalAssistant-Windows-x64.zip)** | **194.8 MB** | 只有程序本体，体积小；开发台与抠图不可用 |
| 源码 | — | 见下方「快速开始」 |

**全量版（2026.11.10c）分两卷** —— GitHub 单文件上限 2 GB，所以切开：

- [`...Full-2026.11.10c.zip.part1`](https://github.com/chenyt-Indom/local-multimodal/releases/download/v2026.11.10/LocalMultimodalAssistant-Full-2026.11.10c.zip.part1) — 1.4 GB
- [`...Full-2026.11.10c.zip.part2`](https://github.com/chenyt-Indom/local-multimodal/releases/download/v2026.11.10/LocalMultimodalAssistant-Full-2026.11.10c.zip.part2) — 791 MB

**合并 + 校验**（Windows，CMD 或 {sh}）：

```{sh}
copy /b LocalMultimodalAssistant-Full-2026.11.10c.zip.part1 + LocalMultimodalAssistant-Full-2026.11.10c.zip.part2 LocalMultimodalAssistant-Full-2026.11.10c.zip

# 校验（应等于 49ec6e997c1370e85fd0950167e5b5fccefe5231d2bae7f99517b189f689d0df）
Get-FileHash .\\LocalMultimodalAssistant-Full-2026.11.10c.zip -Algorithm SHA256
```

> macOS / Linux：`cat *.part1 *.part2 > Full.zip`""".replace("{sh}", shell)

assert old in s, "下载表没匹配上"
s = s.replace(old, new, 1)

s = s.replace(
    '下载 **[`releases/本地多模态助手-轻量版-Windows-x64.zip`](releases/本地多模态助手-轻量版-Windows-x64.zip)**（约 100MB）→ 解压 → 双击运行。',
    '下载 **[轻量版](https://github.com/chenyt-Indom/local-multimodal/releases)**（Release 页里约 195 MB 那个）→ 解压 → 双击 `一键启动.bat`。',
    1)
s = s.replace(
    '语音功能需额外下载 **[`releases/asr-model-zh-14M-int8.zip`](releases/asr-model-zh-14M-int8.zip)**（约 20MB），解压到 `_internal/asr_model/`。',
    '语音功能需额外下载 **[asr-model-zh-14M-int8.zip](https://github.com/chenyt-Indom/local-multimodal/releases)**（约 20 MB），解压到 `_internal/asr_model/`。',
    1)

old_doc = "| [`docs/使用手册.md`](docs/使用手册.md) | 详细使用说明、参数说明、常见问题 |"
new_doc = ("| [`docs/使用手册.md`](docs/使用手册.md) | 详细使用说明、参数说明、常见问题 |\n"
           "| [`docs/阅读攻略.pdf`](docs/阅读攻略.pdf) | **源码阅读攻略**（5 页）：3 天读懂本项目的路线、行号锚点、自测题 |")
assert old_doc in s, "文档表没匹配上"
s = s.replace(old_doc, new_doc, 1)

out = s.replace("\n", "\r\n") if crlf else s
io.open(P, "wb").write(out.encode("utf-8"))
print("README 已更新（CRLF=%s）" % crlf)
