# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把后端 + 前端 + 本地 SD 模型打成桌面应用（onedir）。

- 独立桌桌面窗口：run.py 用 pywebview 打开，不依赖外部浏览器。
- 对话/记忆/文件/知识库：由本机 Ollama（qwen3-vl:8b）推理。
- 文生图/图片微改：内置 torch + diffusers + 本地 SD 模型（sd_model 目录）。
全部离线可用。模型 OLLama 由独立安装的 Ollama 提供。
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = os.path.abspath(os.getcwd())


def _collect(pkg):
    try:
        datas, binaries, hidden = collect_all(pkg)
        return datas, binaries, hidden
    except Exception:
        return [], [], [pkg]


# —— 收集 GUI / 语音相关依赖 ——
# ⚠️⚠️ 2026-11-11（11.11c）：把本地 SD 那一整套（torch / diffusers / transformers）
#   从打包里**排除**。
#   理由：本地文生图已于 2026-11-08 下线，这些包在全仓 **0 处顶层 import**
#   （17 处全是函数内惰性导入），功能上完全用不到；但 PyInstaller 做静态分析时
#   仍然会把它们整棵树收进来 —— 11.11b 因此打出 9.6GB，事后靠手工删 torch
#   才回到 3.1GB，还差点把 models/vendor 一起删掉。
#   现在从源头排掉，产物稳定在 3GB 量级。将来要恢复文生图，把下面
#   _EXCLUDE_BIG 里对应的名字去掉、并把包加回 _pkgs 即可。
_pkgs = ['webview', 'sherpa_onnx', 'sounddevice', 'edge_tts',
         # ⚠️ 2026-11-13（11.13b）：新增向量检索后必须有这两个 ——
         #   backend/embed.py 的 ONNX 回退路径要用 onnxruntime + tokenizers，
         #   两个都是**函数内延迟导入**，静态分析抓不到。
         #   tokenizers 原来躺在下面的 _EXCLUDE_BIG 里（当初排除它是因为
         #   以为它跟 transformers 绑定），实测它**不依赖 torch**，是独立的
         #   Rust 扩展，所以从排除表里挪出来。
         'tokenizers', 'onnxruntime']

_EXCLUDE_BIG = ['torch', 'torchvision', 'diffusers', 'transformers',
                'accelerate', 'safetensors', 'huggingface_hub',
                'nvidia', 'triton', 'xformers', 'deepspeed', 'bitsandbytes']

all_datas, all_binaries, all_hidden = [], [], []
for _p in _pkgs:
    _d, _b, _h = _collect(_p)
    all_datas += _d
    all_binaries += _b
    all_hidden += _h

# —— transformers 运行时依赖校验所需的分发包元数据（dist-info）——
# transformers 启动时会用 importlib.metadata 校验 requests/filelock/numpy 等
# 依赖版本，缺这些 metadata 会在打包环境里报
# "No package metadata was found for The 'requests' distribution was not found"。
_meta_pkgs = ['requests', 'filelock', 'huggingface_hub', 'numpy', 'packaging',
              'pyyaml', 'regex', 'tokenizers', 'safetensors', 'tqdm',
              'torch', 'torchvision', 'accelerate']
for _p in _meta_pkgs:
    try:
        all_datas += copy_metadata(_p)
    except Exception:
        pass

# —— 前端与自带 SD 模型（只读打包资源）——
def _dir_datas(src_root, dst_root, skip_suffixes=(), skip_contains=()):
    """把目录展开为 (文件, 目标目录) 列表；可跳过指定后缀/关键词的残余文件。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for fn in filenames:
            if any(fn.endswith(s) for s in skip_suffixes):
                continue
            if any(s in fn for s in skip_contains):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(dirpath, src_root)
            dst = dst_root if rel == "." else os.path.join(dst_root, rel)
            out.append((src, dst))
    return out


# ⚠️ 路径要跟着"当前用的权重"走（2026-09-26 校正）：
#   这里原来还指着 sd-turbo（SD2.1 蒸馏版），而实际用的早已换成 SDXL base。
#   留着旧路径的后果：打包出来的 exe 里塞的是**过期权重**，画质与预期对不上，
#   还会让人以为"代码没生效"。
#   跳过 *.incomplete（没下完的残片，否则会把半个文件塞进包里）。
_SD_ROOT = r"E:\local-multimodal-models\sdxl-base"           # 文生图 / 整图微改
_INPAINT_ROOT = r"E:\local-multimodal-models\sdxl-inpaint"    # 局部重绘（精确微改）
# ⚠️ 2026-11-13（11.13e / 11.13f）：frontend 目录堆了 22 个历史备份
#   （app.js.bak / app.js.bak-cats-20261111 / style.css.bak_voice …），
#   _dir_datas 是整目录收，于是这些垃圾全被打进包里（11.13e 的包带了 22 个）。
#   11.13e 先用 endswith(".bak") 过滤，只挡住 2 个 —— `.bak_avatar_removal`
#   `.bak-cats-20261111` 这种**不以 .bak 结尾**的全漏了。
#   改成按包含判断：文件名里出现 ".bak" 一律不要。
all_datas += _dir_datas(os.path.join(root, "frontend"), "frontend",
                        skip_contains=(".bak",))
if os.path.isdir(_SD_ROOT):
    all_datas += _dir_datas(_SD_ROOT, "sd_model", skip_suffixes=(".incomplete",))
# 局部重绘模型（约 6.5GB）：缺了它"只在指定区域重绘"就用不了，
# 但**不影响**文生图/整图微改 —— 所以缺了只提示，不报错。
if os.path.isdir(_INPAINT_ROOT):
    all_datas += _dir_datas(_INPAINT_ROOT, "sd_inpaint", skip_suffixes=(".incomplete",))
else:
    print("[spec] 提示：没找到局部重绘权重 %s —— 打包后「涂抹选区精确微改」将不可用"
          % _INPAINT_ROOT)

# —— 语音识别模型（离线唤醒词 + 流式识别，约 78MB）——
_ASR_ROOT = r"D:\local-multimodal-models\sherpa-asr"
if os.path.isdir(_ASR_ROOT):
    all_datas += _dir_datas(_ASR_ROOT, "asr_model")

# —— 后端模块的显式收集 ——
all_hidden += [
    "backend.main", "backend.config", "backend.ollama_client", "backend.tools",
    "backend.t2i", "backend.memory", "backend.kb", "backend.file_tools",
    "backend.video", "backend.web_tools", "backend.voice", "backend.tts",
    # ⚠️ 2026-11-13（11.13b）新增的三个模块，必须显式列出：
    #   · backend.embed   —— 向量化后端（kb.py / memvec.py 才 import 它，
    #                        main.py 里一次都没提，靠遍历 main 的依赖找不到）
    #   · backend.memvec  —— 记忆库向量索引
    #   · backend.chatimg —— 对话图片台账
    "backend.embed", "backend.memvec", "backend.chatimg",
    # ⚠️ 2026-11-13（11.13f）：main.py 顶层有 `from . import selfdev, scheduler,
    #   perf, model_manager, selftest`，理论上静态分析能连带收进来；但这几个
    #   都是"只在函数内被调用"的模块，最容易被漏（11.13a 的 edge_tts 就是这么
    #   翻车的：源码跑正常、打包版功能全废）。显式列出，成本为零。
    "backend.scheduler", "backend.selfdev", "backend.perf",
    "backend.model_manager", "backend.selftest",
    # ⚠️ 2026-11-13（11.13f）：web_tools.py 解 brotli 压缩的网页时用
    #   `import brotli`（函数内 + try）。缺了不会崩，但会返回空 → 大量
    #   返回 br 压缩的站点直接"抓不到"。几十 KB，加上更稳。
    "brotli",
    "sounddevice", "sherpa_onnx", "tokenizers", "onnxruntime",
    # ⚠️ 2026-11-13：语音合成（backend/tts.py）用的是**函数内延迟导入** ——
    #   `import edge_tts` 和 `import win32com.client` 都写在函数体里，
    #   PyInstaller 的静态分析**抓不到**。实测：11.12a 那版打出来后
    #   edge_tts 在 PYZ 里是 0 个模块、win32com 只有 shell 没有 client，
    #   于是打包版的 TTS 直接用不了（源码跑却正常，最难查的那类）。
    #   这里两头都堵：edge_tts 走 collect_all（见上面 _pkgs），
    #   win32com 走 collect_submodules（见下面）。
    "win32com", "pythoncom", "pywintypes",
    "uvicorn", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "fastapi", "pydantic", "websockets", "multipart", "python_multipart",
    "clr", "pythonnet",
]
all_hidden += collect_submodules("uvicorn", filter=lambda n: True)
# pywin32 的 COM 客户端（win32com.client / win32com.shell 下的子模块）
# 不显式收集的话只会带进空壳，SAPI 兜底朗读会 ImportError。
for _m in ("win32com", "edge_tts"):
    try:
        all_hidden += collect_submodules(_m)
    except Exception as _e:
        print("[spec] collect_submodules(%s) 失败：%s" % (_m, _e))

a = Analysis(
    [os.path.join(root, "run.py")],
    pathex=[root],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6", "PyQt6"] + _EXCLUDE_BIG,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="本地多模态助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 大体积 torch/CUDA 库不适合 upx 压缩，易损坏且极慢
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # 桌面应用：不弹黑框控制台（失败时由 run.py 弹窗提示）
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="本地多模态助手",
)