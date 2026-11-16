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
         'tokenizers', 'onnxruntime',
         # ⚠️ 2026-11-14（11.14）：数字人与文档读取上线后新增的两个依赖。
         #   · cv2     —— avatar2 / avatar2_expr / wav2lip_onnx 全靠它处理帧
         #   · olefile —— docread 读老式 .doc 的 OLE 复合文档
         #   两个本机都装了，但 11.13f 的包里**都没有**（那时还没模块用它们），
         #   不显式收就会在打包版上「数字人不出画面 / .doc 打不开」。
         'cv2', 'olefile',
         # ⚠️ 2026-11-15（11.15a）：backend/talkhead/phoneme.py 用 pypinyin 把
         #   汉字转拼音，再由拼音映射音素 → 口型。本机已装 0.55.0，但它是
         #   **函数内延迟导入**（没有拼音库时退回按字切分），不显式收就会
         #   在打包版上「口型和字对不上」，而源码版一切正常。
         'pypinyin']

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
    """把目录展开为 (文件, 目标目录) 列表；可跳过指定后缀/关键词的残余文件。

    ⚠️ 2026-11-16（11.16a）记录一次**决定**：曾想把 `__pycache__/*.pyc` 一并过滤
    （它们只是字节码缓存，删掉只会让 Python 首次导入时重编一次，功能零影响），
    但用户明确要求「宁多勿缺」—— 所以这里**保持原样，照样收进去**（约 459KB）。
    真要清理时再单独处理，别在打包路径上做减法。
    """
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

# ⚠️ 2026-11-15（11.15a）：backend 下有两个**按路径访问的运行时目录**，
#   它们不是模块，PyInstaller 不会自动带；少了会静默失效：
#   · _pysite/        procwin 把它塞进子进程的 PYTHONPATH，让孙进程也不弹黑窗；
#                     regress.py 里还有 `assert os.path.isdir(pysite)`，缺了直接断言失败。
#   · _feature_tests/ 新功能自带的验收断言，regress 按**文件路径**动态 import；
#                     selfevolve 还会往这个目录里写新文件（目录必须可写/可存在）。
#   目标路径必须是 backend/<名字> —— 代码是按 dirname(__file__) 拼的。
for _sub in ("_pysite", "_feature_tests"):
    _p = os.path.join(root, "backend", _sub)
    if os.path.isdir(_p):
        _files = _dir_datas(_p, "backend/" + _sub, skip_contains=("__pycache__",))
        all_datas += _files
        print("[spec] 已收集 backend/%s（%d 个文件）" % (_sub, len(_files)))
    else:
        print("[spec] 提示：backend/%s 不存在" % _sub)
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

# —— 数字人 2.0 的人脸检测模型（2026-11-14 / 11.14）——
#   backend/avatar2.py 会去 <程序根>/data/models/face/ 找 yunet onnx（232KB）。
#   不收集的后果：打包版「数字人」直接报"没人脸检测模型"，源码版却正常。
_FACE_ROOT = os.path.join(root, "data", "models", "face")
if os.path.isdir(_FACE_ROOT):
    all_datas += _dir_datas(_FACE_ROOT, "data/models/face")
else:
    print("[spec] 提示：没找到人脸检测模型 %s —— 数字人功能将不可用" % _FACE_ROOT)

# —— 3D / 2.5D 数字人的 3DMM 模型（2026-11-14 / 11.14b）——
#   backend/face3d.py 会去 <程序根>/data/models/face3d/ 找这四个文件：
#     mb1_120x120.onnx (13MB) / bfm_decoder.onnx (23MB) / tri.pkl (892KB)
#     / param_mean_std_62d_120x120.pkl
#   不收集的后果：打包版「2.5D / 3D 数字人」直接报"3D 模型文件不全"，源码版却正常
#   —— 和上一版漏掉 yunet 人脸模型是同一类坑。bfm_noneck_v3.pkl(24MB) 只有离线
#   转换脚本用，但按「宁可多不能少」一并带上。
_F3D_ROOT = os.path.join(root, "data", "models", "face3d")
if os.path.isdir(_F3D_ROOT):
    all_datas += _dir_datas(_F3D_ROOT, "data/models/face3d")
    print("[spec] 已收集 face3d 3DMM 模型")
else:
    print("[spec] 提示：没找到 %s —— 2.5D/3D 数字人将不可用" % _F3D_ROOT)

# Wav2Lip 生成式口型权重（2026-11-14 / 11.14c 起本机已有）
#   运行时只要这几个：
#     wav2lip_gan.onnx + wav2lip_gan.onnx.data（139MB，外部权重，必须同目录）
#     scrfd_2.5g_bnkps.onnx（人脸+5点）/ _mel_basis.npy（mel 基）
#     bigface_x4.onnx(.data) —— 代码里注明"合成阶段负收益不启用"，但 _paths() 会引用，
#     留着以免将来启用时缺文件（66MB）
#   ⚠️ 不收集 .pth/.pt/.ckpt：wav2lip_gan.pth 有 416MB，只在离线导出 ONNX 时用，
#      运行时代码一次都不读（已 grep 确认）。不打进去能省 416MB。
_W2L_ROOT = os.path.join(root, "data", "models", "wav2lip")
if os.path.isdir(_W2L_ROOT) and any(f.endswith(".onnx") for f in os.listdir(_W2L_ROOT)):
    all_datas += _dir_datas(_W2L_ROOT, "data/models/wav2lip",
                            skip_suffixes=(".pth", ".pt", ".ckpt"))
    print("[spec] 已收集 Wav2Lip 权重（跳过 .pth 导出权重）")
else:
    print("[spec] 提示：本机无 Wav2Lip 权重（正常）—— 唇形同步走 avatar2 形变方案")

# ⚠️⚠️ 2026-11-16（11.16a）：这两个大目录**一直没写进 spec**，是打包时手工拷进
#   dist 的（11.15a 之前的做法），于是每次「重建 dist」它们就丢 —— 11.16a 第一次
#   重建后体积从 4.5GB 掉到 1.9GB、文件数 7608 → 1912，就是这么来的。
#   现在改成 spec 显式收集，以后重建不会再丢：
#     · models/rembg   抠图模型（BiRefNet general + portrait，1.9GB）—— 离线抠图用，
#                      源码树里没有，放在 E: 的模型集中目录；
#     · vendor/        内置 code-server（网页版 VS Code，871MB、5469 个文件）——
#                      代码区/内置编辑器靠它，在项目根目录，.gitignore 里。
_REMBG_ROOT = r"E:\local-multimodal-models\rembg"
if os.path.isdir(_REMBG_ROOT):
    _rb = _dir_datas(_REMBG_ROOT, "models/rembg")
    all_datas += _rb
    print("[spec] 已收集抠图模型 models/rembg（%d 个文件）" % len(_rb))
else:
    print("[spec] 提示：没找到抠图模型 %s —— 离线抠图将不可用" % _REMBG_ROOT)

_VENDOR_ROOT = os.path.join(root, "vendor")
if os.path.isdir(_VENDOR_ROOT):
    _vd = _dir_datas(_VENDOR_ROOT, "vendor", skip_contains=("__pycache__",))
    all_datas += _vd
    print("[spec] 已收集内置 code-server vendor（%d 个文件）" % len(_vd))
else:
    print("[spec] 提示：没找到 vendor/ —— 内置编辑器将不可用")

# —— 离线 SadTalker 的源码骨架（2026-11-16 / 11.16a）——
#   本版数字人主线改成云端生成（avatar_cloud），backend/sadtalker_cpu.py 留作
#   离线兜底：它把 <程序根>/_sadtalker_src 插进 sys.path 再 import src.*，
#   还需要 <程序根>/data/models/sadtalker 里的两份权重。
#   按「模块宁可多不能少」：源码骨架（393KB）打进去；权重（2.8GB，其中 1.4GB
#   是合并中间产物 _seg2）**不带** —— available() 会返回 False，功能显示为不可用，
#   不会报错，用户想用自己把权重放进 data/models/sadtalker 即可。
_ST_ROOT = os.path.join(root, "_sadtalker_src")
if os.path.isdir(_ST_ROOT):
    _st = _dir_datas(_ST_ROOT, "_sadtalker_src",
                     skip_suffixes=(".zip",), skip_contains=("__pycache__",))
    all_datas += _st
    print("[spec] 已收集 _sadtalker_src 源码骨架（%d 个文件，不含权重）" % len(_st))
else:
    print("[spec] 提示：没有 _sadtalker_src —— 离线 SadTalker 兜底不可用（正常）")

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

# ⚠️⚠️ 2026-11-14（11.14）：**全量收录 backend 下所有模块**，别再手工维护名单。
#   背景：backend 已经 61 个 .py，而且每版都在加（11.14 一口气加了 avatar2 /
#   avatar2_expr / wav2lip_onnx / docread / hive_punch / hive_relay_link /
#   hive_relay_server / ws_min / deploy_keys 九个）。这些模块大多是"只在
#   函数内被调用"，静态分析抓不全 —— 漏一个的后果是**打包版某个功能直接废掉，
#   源码跑却完全正常**，且日志里可能连报错都没有。
#   直接扫目录生成，宁可在 PYZ 里多几十个模块，也不能少一个。
all_hidden += ["backend." + _p.stem
               for _p in sorted(__import__("pathlib").Path(root, "backend").glob("*.py"))
               if _p.stem != "__init__"]

# ⚠️ 2026-11-15（11.15a）：上面的 glob("*.py") **扫不到子包**。
#   backend/talkhead/ 是一个目录（__init__ + engine / phoneme / render / viseme），
#   漏了它打包版「说话头」整条链路直接 ImportError，而源码版毫无异常。
#   子包一律按目录再扫一遍。
_BACKEND_DIR = os.path.join(root, "backend")
_subpkgs = []
for _name in sorted(os.listdir(_BACKEND_DIR)):
    _p = os.path.join(_BACKEND_DIR, _name)
    if not os.path.isdir(_p) or _name.startswith((".", "__")):
        continue
    if not os.path.isfile(os.path.join(_p, "__init__.py")):
        continue
    _mods = ["backend." + _name] + [
        "backend.%s.%s" % (_name, _f[:-3])
        for _f in sorted(os.listdir(_p))
        if _f.endswith(".py") and _f != "__init__.py"
    ]
    all_hidden += _mods
    _subpkgs.append("%s(%d)" % (_name, len(_mods)))
if _subpkgs:
    print("[spec] 已收集 backend 子包：" + "、".join(_subpkgs))

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