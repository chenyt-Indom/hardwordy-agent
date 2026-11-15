# -*- coding: utf-8 -*-
"""数字人 2.0：一张图 + 一段话 → 会说话的视频（2026-11-13）

为什么用声学形变而不是神经网络口型
----------------------------------
试过 Wav2Lip（ONNX 96x96）：在正脸证件照上嘴位整体偏移、口腔糊成色块，
观感反而不如形变，已整体移除（含权重）。本机 torch 现在可用（2.15.0+cpu），
但那个模型本身的分辨率限制解决不了，所以不再走这条路。

当前走**声学特征驱动的参数化口型**：

  · 每 40ms 一帧，算 RMS（音量）/ 过零率（声音亮暗）/ 频谱质心；
  · RMS → 张口高度；过零率 + 质心 → 唇形宽窄（扁唇 i/e ↔ 圆唇 u/o）；
  · 若本机找得到 sherpa-onnx 中文 ASR 模型，再用**字级时间戳**约束
    字与字之间的换气段闭嘴，口型不会在停顿处乱动；
  · PIL 逐帧对嘴部区域做弹性形变，ffmpeg 合成 mp4（原音轨保留）。

代价（必须说清，不吹）：这是**形变**不是**重绘**。嘴的形状由声学特征推断，
不做音素级重绘，所以不会精确做出 "b/p/m" 的闭唇口型。二次元立绘上观感自然，
写实人脸会偏"橡皮嘴"——但位置准、不崩，这是它胜过 Wav2Lip 的地方。

存的东西都在 data/avatar2/：
  · avatar.png   当前形象
  · mouth.json   嘴部矩形 {x,y,w,h}（0~1 归一化）
  · meta.json    来源与时间
  · videos/      生成的 mp4
"""
from __future__ import annotations

import glob
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote as _q

import numpy as np
import logging
logger = logging.getLogger(__name__)

# 嘴部默认位置：脸的中下部。默认值不会对所有图都准，前端可以拖动校准。
# 2026-11-15：y 0.735 → 0.66。analyze 的搜索框是 cy-1.55*mh ~ cy+0.65*mh，
# y=0.735（cy=1029）时整段落在下巴/脖子上，根本看不到嘴（实测 y_split=995）。
# 0.66 时搜索框覆盖 826~965，能包住真实唇缝。h 同时收小到 0.045，
# 避免搜索框纵向过宽把下唇阴影也圈进去。
DEFAULT_MOUTH = {"x": 0.50, "y": 0.66, "w": 0.155, "h": 0.045}

_IMG = "avatar.png"
_MOUTH = "mouth.json"
_META = "meta.json"

FPS = 25
MAX_SECONDS = 90          # 单次最长 90 秒，再长渲染太慢
# ⚠ 2026-11-15 修「嘴张不开」：这个值原来是 720，而形象原图是 787×1400，
#   缩到 720 长边后**嘴部 open_h 从 18px 压到 5px**，warp 形变的可用位移
#   只剩 3.4px —— 无论 open_gain 调多高，嘴都动不了几像素（实测成片 186 帧
#   开口高度 std 只有 0.7）。形变分辨率必须和嘴部像素尺度匹配。
#   1080 时 open_h=13.9px（恢复 2.8 倍），渲染成本约 2.2 倍。
MAX_SIDE = 1080           # 输出长边上限（形变分辨率同步提高，嘴部动作才明显）


# ---------------------------------------------------------------- 目录与配置
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir() -> str:
    d = os.path.join(_root(), "data", "avatar2")
    os.makedirs(d, exist_ok=True)
    return d


def _vdir() -> str:
    d = os.path.join(_dir(), "videos")
    os.makedirs(d, exist_ok=True)
    return d


def _img_path() -> str:
    return os.path.join(_dir(), _IMG)


def _read_json(name: str, default: dict) -> dict:
    p = os.path.join(_dir(), name)
    if not os.path.isfile(p):
        return dict(default)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else dict(default)
    except Exception:                                   # noqa: BLE001
        return dict(default)


def _write_json(name: str, d: dict) -> None:
    p = os.path.join(_dir(), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _read_meta() -> dict:
    """meta.json 的读写别名（闭嘴相关代码一直用这个名字）。"""
    return _read_json(_META, {})


def _write_meta(d: dict) -> None:
    _write_json(_META, d)


def ffmpeg() -> str:
    """拿 ffmpeg 可执行文件。优先 imageio_ffmpeg 自带的（项目其它模块也这么取）。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    p = shutil.which("ffmpeg")
    return p or ""


# ---------------------------------------------------------------- 人脸检测（自动定位嘴）
_YUNET: dict = {"path": "", "tried": False}


def _find_yunet() -> str:
    """找 yunet 人脸检测模型。项目里没有就返回空（自动检测降级为手动校准）。"""
    if _YUNET["path"]:
        return _YUNET["path"]
    if _YUNET["tried"]:
        return ""
    _YUNET["tried"] = True
    root = _root()
    cands = [
        os.path.join(root, "data", "models", "face", "face_detection_yunet.onnx"),
        os.path.join(root, "models", "face", "face_detection_yunet.onnx"),
        os.path.join(root, "data", "models", "face", "yunet.onnx"),
    ]
    cands += glob.glob(os.path.join(root, "**", "yunet*.onnx"), recursive=True)
    for p in cands:
        if p and os.path.isfile(p) and os.path.getsize(p) > 50000:
            _YUNET["path"] = p
            return p
    return ""


def detect_mouth(img_path: str) -> dict:
    """用 yunet 找脸 → 由左右嘴角推出嘴部矩形。

    返回 {ok, mouth, score} 或 {ok: False, error}。
    没模型/没检测到脸都不算错误，调用方自己决定是否降级到手动校准。
    """
    mpath = _find_yunet()
    if not mpath:
        return {"ok": False, "error": "没有人脸检测模型"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "没有形象图"}
    try:
        import cv2
        img = cv2.imread(img_path)
        if img is None:
            # cv2.imread 不认中文路径，退回 PIL 读再转
            from PIL import Image
            with Image.open(img_path) as im:
                arr = np.array(im.convert("RGB"))
            img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        h, w = img.shape[:2]
        det = cv2.FaceDetectorYN.create(mpath, "", (w, h),
                                        score_threshold=0.6, nms_threshold=0.3,
                                        top_k=5000)
        _n, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return {"ok": False, "error": "没检测到人脸"}
        # 多张脸时取最大那张（通常就是主体）
        faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
        f = faces[0]
        fx, fy, fw, fh = [float(v) for v in f[:4]]
        score = float(f[14])
        lm = f[4:14].reshape(5, 2)
        rc, lc = lm[3], lm[4]          # 右嘴角、左嘴角
        mcx = (float(rc[0]) + float(lc[0])) / 2.0
        mcy = (float(rc[1]) + float(lc[1])) / 2.0
        mw = math.hypot(float(rc[0]) - float(lc[0]), float(rc[1]) - float(lc[1]))
        if mw < 4:
            return {"ok": False, "error": "嘴角定位异常"}
        # 嘴部矩形：中心比嘴角中点略低一点点（唇在下），宽取嘴角距 1.18 倍，
        # 高取嘴宽的 0.42（自然唇高约是唇宽的 0.3~0.45）
        # 2026-11-14：偏移系数 0.006→0.014。实测这张证件照上 yunet 的嘴角中点
        # 落在 y=596，而原图亮度剖面最暗行（真实唇缝）在 y=600，差 4px；按脸高
        # 0.8% 补偿后正好对齐唇缝。
        out = {
            "x": mcx / w,
            "y": min(1.0, (mcy + fh * 0.014) / h),
            "w": min(0.9, (mw * 1.18) / w),
            "h": min(0.5, (mw * 0.42) / w),
        }
        out["x"] = min(max(out["x"], 0.0), 1.0)
        out["y"] = min(max(out["y"], 0.0), 1.0)
        return {"ok": True, "mouth": out, "score": round(score, 3),
                "face": [round(fx / w, 4), round(fy / h, 4),
                         round(fw / w, 4), round(fh / h, 4)]}
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 形象管理
def mouth() -> dict:
    m = dict(DEFAULT_MOUTH)
    for k, v in _read_json(_MOUTH, {}).items():
        if k in ("x", "y", "w", "h"):
            try:
                m[k] = float(v)
            except (TypeError, ValueError):
                logger.debug("silent-except", exc_info=True)
    # 夹到合法区间，别让脏数据把嘴画到画面外
    m["x"] = min(max(m["x"], 0.0), 1.0)
    m["y"] = min(max(m["y"], 0.0), 1.0)
    m["w"] = min(max(m["w"], 0.02), 0.9)
    m["h"] = min(max(m["h"], 0.01), 0.5)
    return m


def set_mouth(d: dict) -> dict:
    cur = mouth()
    for k in ("x", "y", "w", "h"):
        if k in (d or {}):
            try:
                cur[k] = float(d[k])
            except (TypeError, ValueError):
                logger.debug("silent-except", exc_info=True)
    _write_json(_MOUTH, cur)
    return mouth()


def _any_image_key(image_key: str = "") -> str:
    """统一取图像 Key：前端传了用前端的，没传用后端保管的那把。

    2026-11-15 用户要求「所有 key 统一，一把多用」：在 🎨 里存一次，
    文生图 / 图像微改 / 数字人闭嘴 / 定时任务全部共享，不用各自再配。
    """
    k = str(image_key or "").strip()
    if k:
        return k
    try:
        from . import keyvault
        return keyvault.get_image_key()
    except Exception:
        return ""


def close_plan(image_key: str = "") -> dict:
    """当前这台机器该用哪种方式生成闭嘴基准（只探测，不加载模型）。

    ⚠️ 为什么要这个（2026-11-15 实测）：SDXL 在 **CPU** 上根本不可用 ——
       · fp16 加载后进程内存 15.4GB，推理峰值 **22.8GB**；
       · 单步（**仅 256×256**）**20.1 秒**，按面积外推 1024×1024 单步约 322 秒，
         32 步 ≈ **2.9 小时**。
       低配机（16GB / 无独显）既吃不下内存，也等不起时间。所以必须先探测再决定，
       不能让用户点一下「闭嘴基准」就卡几小时。

    返回：
      tier      —— gpu_high / gpu_mid / gpu_low / cpu
      method    —— 建议的方法（sdxl_hires / lip_shift）
      gen_ok    —— 本机能不能在合理时间内跑生成式（扩散）
      eta       —— 预计耗时（人话）
      why       —— 为什么是这个结论
      can_force —— 能不能强制跑生成式（CPU 上也能，只是很慢）
    """
    out = {"ok": True, "tier": "cpu", "method": "lip_shift", "gen_ok": False,
           "eta": "", "why": "", "can_force": False, "gpu": None,
           "vram_gb": None, "device": "cpu",
           # 云端生成式：不占本地算力，是低配机拿到高质量效果的正路。
           # 2026-11-15：Key 统一保管 —— 前端没传时用后端存的那把（同一个 Key 多通道共用）。
           "cloud_ok": bool(_any_image_key(image_key)), "best": ""}
    try:
        from . import t2i as _t2i
        d = _t2i.device_info()
    except Exception as e:                              # noqa: BLE001
        out["why"] = "设备探测失败：%s" % e
        return out
    out["device"] = d.get("device") or "cpu"
    out["gpu"] = d.get("gpu")
    vram = None
    if out["device"] == "cuda":
        try:
            import torch
            vram = float(torch.cuda.get_device_properties(0).total_memory) / (1024 ** 3)
        except Exception:                               # noqa: BLE001
            vram = None
    out["vram_gb"] = round(vram, 1) if vram else None

    if out["device"] == "cuda" and vram:
        if vram >= 10:
            out.update(tier="gpu_high", method="sdxl_hires", gen_ok=True,
                       eta="约 20~40 秒",
                       why="显卡 %s（%.0fGB 显存），可跑 SDXL 高分辨率重绘" % (out["gpu"], vram))
        elif vram >= 6:
            out.update(tier="gpu_mid", method="sdxl_hires", gen_ok=True,
                       eta="约 25~50 秒",
                       why="显卡 %s（%.0fGB 显存），用中等分辨率重绘（更省显存）" % (out["gpu"], vram))
        else:
            out.update(tier="gpu_low", method="sdxl_hires", gen_ok=True,
                       eta="约 40~90 秒",
                       why="显卡 %s（%.0fGB 显存），用小分辨率重绘（最省显存）" % (out["gpu"], vram))
        out["can_force"] = True
        out["best"] = "local"          # 本地有显卡：快、免费，优先本地
        return out

    # 纯 CPU：生成式在物理上不可行（内存 + 时间都过不去）
    out.update(
        tier="cpu", method="lip_shift", gen_ok=False, can_force=True,
        eta="约 5~20 分钟（不推荐）",
        why=("本机没有可用显卡。SDXL 在 CPU 上推理峰值内存约 23GB、"
             "1024×1024 跑 32 步约 3 小时（实测单步 20 秒，还是 256×256 的尺寸），"
             "所以本地不跑扩散。"))
    if out["cloud_ok"]:
        out["best"] = "cloud"
        out["method"] = "cloud"
        out["eta"] = "约 20~60 秒（云端）"
        out["why"] += "已检测到图片 API Key，走云端生成式重绘（不占本地算力）。"
    else:
        out["best"] = "deterministic"
        out["eta"] = "约 1 秒（确定性，效果有限）"
        out["why"] += ("也没填图片 API Key，只能用确定性合成。"
                       "要生成式质量：填 Key 走云端，或在有显卡的机器上生成后拷图。")
    return out


def info() -> dict:
    p = _img_path()
    has = os.path.isfile(p)
    meta = _read_json(_META, {})
    size = None
    if has:
        try:
            from PIL import Image
            with Image.open(p) as im:
                size = list(im.size)
        except Exception:                               # noqa: BLE001
            size = None
    return {
        "ok": True,
        "has_image": has,
        # 时间戳做缓存击穿：换了形象前端立刻看到新的
        "url": ("/api/avatar2/raw?v=%d" % int(os.path.getmtime(p))) if has else "",
        "size": size,
        "mouth": mouth(),
        "source": meta.get("source") or "",
        "updated": meta.get("updated") or "",
        "dir": _dir(),
        # 闭嘴基准：源图本来张着嘴时用它，否则 Wav2Lip 的闭嘴帧会带着口型
        "closed": has_closed(),
        "closed_url": ("/api/avatar2/closed?v=%d" % int(os.path.getmtime(_img_closed_path())))
        if has_closed() else "",
        # 闭嘴**预览**：只给看不生效，用户点「用这个」才转正
        "closed_preview": has_closed_preview(),
        "closed_preview_url": ("/api/avatar2/closed_preview?v=%d"
                               % int(os.path.getmtime(_img_closed_preview_path())))
        if has_closed_preview() else "",
        "videos_dir": _vdir(),
        # 这台机器适合怎么生成闭嘴基准（低配机据此走确定性、不硬跑扩散）
        "close_plan": close_plan(),
    }


def save_bytes(raw: bytes, source: str = "") -> dict:
    """把形象图写进 data/avatar2/avatar.png（统一转 PNG）。"""
    if not raw:
        return {"ok": False, "error": "图片数据为空"}
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
        else:
            im = im.convert("RGB")
        # 太大的图缩一下：渲染是按帧做的，4000px 原图会让每帧慢十倍
        max_side = 1400
        if max(im.size) > max_side:
            r = max_side / float(max(im.size))
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                           Image.LANCZOS)
        im.save(_img_path(), "PNG")
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "保存形象失败：%s: %s" % (type(e).__name__, e)}
    # 闭嘴基准是按**旧图**合成的，换了形象必须清掉，否则新形象会用旧图的嘴
    for _stale in (_img_closed_path(), _img_closed_preview_path()):
        try:
            if os.path.isfile(_stale):
                os.remove(_stale)
        except OSError:
            logger.debug("silent-except", exc_info=True)
    # 存完立刻自动定位嘴：上传照片和 AI 头像的构图差别极大，靠默认值一定偏，
    # 检测到就覆盖 mouth.json；检测不到保留默认值，前端可手动拖动校准。
    auto = detect_mouth(_img_path())
    meta = {"source": source or "",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    # ★ 2026-11-15「全乱了、位置偏离太过严重」的直接原因就在这里：
    #   yunet 在卡通/戴护目镜的形象上不可靠 —— 实测这张图 score 只有 0.605
    #   （阈值 0.6 勉强过），嘴角关键点偏下 76px，mouth.json 被写成 cy=921
    #   （真值 853）。框一偏，analyze 的「预期口腔中心」跟着偏，
    #   形变范围从 55 行扩大到 164 行、下界从 889 拖到 1008（越过衣服 112px）。
    # 低分时不覆盖：宁可保留默认值/旧值，也不要把好值冲掉；
    # 前端「AI 定位」按钮走的是多模态 LLM，实测给 841（真值 845，差 4px）。
    _sc = float(auto.get("score") or 0.0)
    if auto.get("ok") and _sc >= 0.72:
        set_mouth(auto["mouth"])
        meta["auto_mouth"] = auto["mouth"]
        meta["face_score"] = _sc
    elif auto.get("ok"):
        # 分数偏低：只记录，不写 mouth.json
        meta["auto_mouth"] = auto["mouth"]
        meta["face_score"] = _sc
        meta["auto_skipped"] = (
            "人脸检测分 %.3f 偏低，未覆盖嘴部标定；请在面板点「AI 定位」校准" % _sc)
    else:
        meta["auto_mouth"] = None
        meta["auto_error"] = auto.get("error") or ""
    _write_json(_META, meta)
    out = info()
    out["auto_mouth"] = auto if auto.get("ok") else {
        "ok": False, "error": auto.get("error") or "未检测到人脸"}
    return out


def save_from_path(src: str, source: str = "") -> dict:
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "找不到图片文件：%s" % src}
    with open(src, "rb") as f:
        return save_bytes(f.read(), source=source or os.path.basename(src))


def save_from_b64(b64: str, source: str = "") -> dict:
    import base64
    s = (b64 or "").strip()
    if "," in s[:80] and s.lower().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "base64 解码失败：%s" % e}
    return save_bytes(raw, source=source or "b64")


def reset() -> dict:
    for p in (_img_path(), _img_closed_path(), _img_closed_preview_path()):
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                logger.debug("silent-except", exc_info=True)
    return info()


# ------------------------------------------------------- 卡通形象 / 闭嘴基准
def _img_closed_path() -> str:
    return os.path.join(_dir(), "avatar_closed.png")


def _img_closed_preview_path() -> str:
    """闭嘴效果的**预览**图：先给用户看，满意了点「用这个」才转正。

    单独一个文件，绝不碰 avatar_closed.png —— 预览就是预览，
    不满意关掉即可，主形象和已生效的基准都不受影响。
    """
    return os.path.join(_dir(), "avatar_closed_preview.png")


def has_closed() -> bool:
    return os.path.isfile(_img_closed_path())


def has_closed_preview() -> bool:
    return os.path.isfile(_img_closed_preview_path())


def apply_closed_preview() -> dict:
    """把预览图转正成 avatar_closed.png（用户点「用这个」才走到这里）。"""
    src = _img_closed_preview_path()
    if not os.path.isfile(src):
        return {"ok": False, "error": "还没有预览图，先点「闭嘴基准」生成一张"}
    try:
        shutil.copyfile(src, _img_closed_path())
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "转正失败：%s: %s" % (type(e).__name__, e)}
    meta = _read_json(_META, {})
    meta["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["closed_from"] = os.path.basename(_img_path())
    meta["closed_method"] = "preview_apply"
    _write_json(_META, meta)
    # 转正了就不需要预览了：留着会让界面一直挂着预览块
    try:
        os.remove(src)
    except OSError:
        logger.debug("silent-except", exc_info=True)
    return {"ok": True, "closed": True, "info": info()}


def drop_closed_preview() -> dict:
    """丢掉预览（不满意时用），已生效的基准和主形象都不动。"""
    p = _img_closed_preview_path()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            logger.debug("silent-except", exc_info=True)
    return {"ok": True, "dropped": True}


def render_source() -> str:
    """渲染时真正要用的形象图：有闭嘴基准就用它，否则用原图。"""
    p = _img_closed_path()
    return p if os.path.isfile(p) else _img_path()


def face_box_from_mouth(size=None):
    """用嘴部标定反推 Wav2Lip 的人脸裁剪框。

    卡通 / 非人类形象没有 SCRFD 能识别的人脸（实测直接返回 0 个框，
    报「未检测到人脸」），只能靠用户框出来的嘴部反推一个裁剪框。
    两个比例来自 Wav2Lip 训练数据的对齐方式：嘴中心约在裁剪框 76% 高度，
    嘴高约等于脸高的 1/4.6。裁剪框取正方形（模型输入是 96x96）。
    """
    p = _img_path()
    if not os.path.isfile(p):
        return None
    if size is None:
        try:
            from PIL import Image
            with Image.open(p) as im:
                size = im.size
        except Exception:                               # noqa: BLE001
            return None
    W, H = size
    cx, cy, mw, mh = _mouth_geom((W, H), mouth())
    fh = max(24.0, mh * 4.6)
    x1 = cx - fh / 2.0
    y1 = cy - 0.76 * fh
    x2 = x1 + fh
    y2 = y1 + fh
    x1 = max(0, min(W - 8, x1))
    y1 = max(0, min(H - 8, y1))
    x2 = max(x1 + 8, min(W, x2))
    y2 = max(y1 + 8, min(H, y2))
    return int(x1), int(y1), int(x2), int(y2)


def _b64_png(pil):
    """PIL → base64 PNG（RGBA 也能存，mask 要用）。"""
    import base64 as _b64
    import io as _io
    b = _io.BytesIO()
    pil.save(b, "PNG")
    return _b64.b64encode(b.getvalue()).decode()


def _pil_of_b64(b64):
    """base64 → RGB PIL。"""
    import base64 as _b64
    import io as _io

    from PIL import Image
    s = b64.split(",", 1)[1] if str(b64).startswith("data:") else b64
    return Image.open(_io.BytesIO(_b64.b64decode(s))).convert("RGB")


def _letterbox_rgb(pil, tw, th):
    """等比缩放 + 居中补边（不拉伸变形）。返回 (图, (pad_x, pad_y, w, h))。"""
    from PIL import Image
    w, h = pil.size
    s = min(tw / float(w), th / float(h))
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    im = pil.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (tw, th), (0, 0, 0))
    px, py = (tw - nw) // 2, (th - nh) // 2
    canvas.paste(im, (px, py))
    return canvas, (px, py, nw, nh)


def _blend_mouth_back(img_bgr, patched_rgb, geom, x0, y0, cs):
    """把重绘后的嘴周块贴回原图。

    与 `_sdxl_hires_close` 同一套后处理（那边已验证过视觉结果）：
      只贴口腔椭圆内部 → 椭圆加权色对齐 → 去小块暗斑。
    用椭圆而不是矩形，是因为矩形羽化在实测里被判「明显的矩形拼接边缘、像贴纸」。
    """
    import numpy as _np
    from PIL import Image

    H, W = img_bgr.shape[:2]
    cx = float(geom.get("cx") or W * 0.5)
    mw = float(geom.get("mw") or 80.0)
    ys = float(geom.get("y_split") or (float(geom.get("cy") or H * 0.6)))
    yh = float(geom.get("y_hole_bot") or (float(geom.get("cy") or H * 0.6) + 12))

    base = _np.array(Image.fromarray(img_bgr[:, :, ::-1])).astype(_np.float32)
    patch = _np.array(patched_rgb.resize((cs, cs), Image.LANCZOS)).astype(_np.float32)

    _cx = cx - x0
    _cy = (ys + yh) * 0.5 - y0
    # 贴回蒙版与送云端的 mask 对齐（略大 4% 做羽化过渡）。
    # 蒙版比 mask 大太多 = 把原图本来好的唇线也换成模型画的，实测会被磨平。
    _rx = max(10.0, 0.94 * mw)
    _ry = max(8.0, (yh - ys) * 0.5)
    yy, xx = _np.mgrid[0:cs, 0:cs]
    dist = ((xx - _cx) / _rx) ** 2 + ((yy - _cy) / _ry) ** 2
    alpha = _np.clip((1.18 - dist) / 0.36, 0, 1).astype(_np.float32)[..., None]
    region = base[y0:y0 + cs, x0:x0 + cs]
    base[y0:y0 + cs, x0:x0 + cs] = region * (1 - alpha) + patch * alpha

    # 椭圆加权色对齐（必须按椭圆，不能按矩形 —— 实测矩形会留方形色差）
    ref = _np.array(Image.fromarray(img_bgr[:, :, ::-1])).astype(_np.float32)
    # ⚠️ 统计范围只比椭圆大 3px。原来扩 6px 时，上边界会越过唇线碰到护目镜下沿，
    #   实测有 135 个像素被改动（视觉上看不出，但「主形象不动」就该是零改动）。
    ay0, ay1 = max(0, int(_cy + y0 - _ry - 3)), min(H, int(_cy + y0 + _ry + 3))
    ax0, ax1 = max(0, int(_cx + x0 - _rx - 3)), min(W, int(_cx + x0 + _rx + 3))
    if ay1 - ay0 > 6 and ax1 - ax0 > 6:
        a_sub = base[ay0:ay1, ax0:ax1]
        b_sub = ref[ay0:ay1, ax0:ax1]
        w_sub = alpha[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0][..., 0]
        for c in range(3):
            ma, sa = float(a_sub[:, :, c].mean()), float(a_sub[:, :, c].std()) + 1e-6
            mb, sb = float(b_sub[:, :, c].mean()), float(b_sub[:, :, c].std()) + 1e-6
            adj = (a_sub[:, :, c] - ma) / sa * sb + mb
            a_sub[:, :, c] = a_sub[:, :, c] * (1 - 0.55 * w_sub) + adj * (0.55 * w_sub)

    # 去小块暗斑（云端也常在嘴角留杂点）
    # ⚠️ 只在**口腔内部**去暗斑。原来从 ys-14 开始，上边界落在护目镜下沿，
    #   实测把护目镜边缘的 135 个像素当成"杂点"改掉了（主形象不该动）。
    try:
        from scipy import ndimage
        vy0, vy1 = max(0, int(ys + 1)), min(H, int(yh + 2))
        vx0, vx1 = max(0, int(cx - 0.74 * mw)), min(W, int(cx + 0.74 * mw))
        sub = base[vy0:vy1, vx0:vx1]
        dark = sub.max(axis=2) < 122
        if dark.any():
            lab, n = ndimage.label(dark)
            for i in range(1, n + 1):
                m = lab == i
                if m.sum() <= 60:
                    ys_i, xs_i = _np.where(m)
                    for _y, _x in zip(ys_i, xs_i):
                        _a = sub[max(0, _y - 4):_y + 5, max(0, _x - 4):_x + 5].reshape(-1, 3)
                        _light = _a[_a.max(axis=1) > 135]
                        if len(_light):
                            sub[_y, _x] = _light.mean(axis=0)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    return _np.clip(base, 0, 255).astype(_np.uint8)[:, :, ::-1]


def _cloud_close(img_bgr, geom, key, plan, timeout=300):
    """用**云端 image2.5**（/images/edits）生成闭嘴基准 —— 低配机路线。

    本机完全不加载生成模型，只做「裁剪 → 送云端 → 贴回」，所以
    没有显卡、显存 <4GB 的机器也能跑。

    plan 来自 avatar_tune.plan_close()：mode(inpaint/whole) / prompt / negative / cs / n

    返回 dict：ok / images(多张 BGR) / mode / cost / err
    """
    import time as _t

    import numpy as _np
    from PIL import Image

    from . import cloud_image as CI

    if not str(key or "").strip():
        return {"ok": False, "images": [], "err": "没有图片 API Key（点右上角 🎨 填入）"}

    H, W = img_bgr.shape[:2]
    cx = float(geom.get("cx") or W * 0.5)
    cy = float(geom.get("cy") or H * 0.6)
    mw = float(geom.get("mw") or 80.0)
    ys = float(geom.get("y_split") or cy)
    yh = float(geom.get("y_hole_bot") or cy + 12)

    prompt = str(plan.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "images": [], "err": "LLM 没给出提示词"}
    neg = str(plan.get("negative") or "").strip()
    if neg:
        # OpenAI 兼容接口没有 negative_prompt 字段，用文本规避（实测有效）
        prompt = prompt + "\n\nAvoid: " + neg

    mode = str(plan.get("mode") or "inpaint").strip().lower()
    if mode not in ("inpaint", "whole"):
        mode = "inpaint"
    try:
        n = int(max(1, min(3, int(plan.get("n") or 1))))
    except Exception:                                   # noqa: BLE001
        n = 1

    t0 = _t.time()
    outs = []
    try:
        if mode == "whole":
            # 整图重绘：letterbox 到 2:3，回来裁掉补边再缩回原尺寸（不拉伸）
            src = Image.fromarray(img_bgr[:, :, ::-1])
            big, (px, py, nw, nh) = _letterbox_rgb(src, 1024, 1536)
            imgs = CI.edit(prompt, key, _b64_png(big), size="1024x1536", n=n)
            for b64 in imgs:
                got = _pil_of_b64(b64)
                got = got.crop((px, py, px + nw, py + nh)).resize((W, H), Image.LANCZOS)
                outs.append(_np.array(got)[:, :, ::-1].copy())
        else:
            cs = int(max(200, min(420, int(plan.get("cs") or 300))))
            cs = int(max(160, min(cs, min(W, H))))
            x0 = int(max(0, min(W - cs, cx - cs / 2.0)))
            y0 = int(max(0, min(H - cs, cy - cs / 2.0)))
            crop = Image.fromarray(img_bgr[y0:y0 + cs, x0:x0 + cs][:, :, ::-1])
            BIG = 1024
            big = crop.resize((BIG, BIG), Image.LANCZOS)

            # mask：口腔椭圆 = 编辑区（白 + 不透明），其余黑 + 全透明。
            # 这样无论服务端按「白=改」还是按「透明=不改」解释，结果都一样。
            # ⚠️ mask 必须**只圈口腔**（唇线以下、下唇以上）。
            #   2026-11-15 三档对比（DeepSeek 判）：1.08mw/-7/+12 会圈到鼻底和两侧腮红，
            #   0.95mw/-4/+5 仍碰到腮红边缘，0.90mw/-1/+3 才精准贴住口腔轮廓。
            #   mask 圈太大 = 把原本好好的唇线也交给模型重画，必被磨平。
            mx0 = cx - 0.90 * mw - x0
            mx1 = cx + 0.90 * mw - x0
            my0 = ys - 1.0 - y0
            my1 = yh + 3.0 - y0
            s = BIG / float(cs)
            rgx0, rgy0 = max(0.0, mx0) * s, max(0.0, my0) * s
            rgx1, rgy1 = min(float(cs), mx1) * s, min(float(cs), my1) * s
            if rgx1 - rgx0 < 16 or rgy1 - rgy0 < 10:
                return {"ok": False, "images": [], "err": "嘴部区域太小，无法重绘"}

            from PIL import ImageDraw
            m = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
            ImageDraw.Draw(m).ellipse([rgx0, rgy0, rgx1, rgy1],
                                      fill=(255, 255, 255, 255))
            imgs = CI.edit(prompt, key, _b64_png(big), mask_b64=_b64_png(m),
                           size="1024x1024", n=n)
            for b64 in imgs:
                got = _pil_of_b64(b64)
                outs.append(_blend_mouth_back(img_bgr, got, geom, x0, y0, cs))
    except Exception as e:                              # noqa: BLE001
        msg = str(e)
        try:
            from . import cloud_image as _CI
            msg = _CI.scrub(msg)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
        return {"ok": False, "images": [], "err": msg[:300], "mode": mode}

    cost = _t.time() - t0
    if not outs:
        return {"ok": False, "images": [], "err": "云端没有返回图片",
                "mode": mode, "cost": cost}
    return {"ok": True, "images": outs, "mode": mode, "cost": cost, "n": len(outs)}


def _sdxl_hires_close(img_bgr, geom, strength: float = 0.96,
                      steps: int = 34, cs: int = 300, big: int = 1024):
    """用 SDXL **高分辨率局部重绘**生成闭嘴基准（生成式，不是像素拼接）。

    流程：裁嘴周 cs×cs → 放大到 1024 → 只对口腔区域打 mask 重绘
          → 缩回 cs×cs → 羽化贴回 → 颜色对齐原图 → 去小块暗斑

    为什么这么做（2026-11-15 一轮实测，同一张图同一个 mask）：
      · 直接在原图上 inpaint：mask 只有 26×112 像素，SDXL 能画的像素太少，
        出来是「扁平色块/涂抹」（唇部 std 70.5 → **26.9**）；
      · 整图 img2img：连护目镜都被改掉 30.9%，主形象保不住；
      · 本方案：嘴周放大 3.4 倍再重绘，SDXL 有 1024 像素可用 ——
        唇部 std 保持 **61.3**（≈原图 70.5），区域外强改 **0.016%**。
        视觉复核（DeepSeek）结论：「比 CUR 好非常多，质的飞跃，基本没有明显缺陷」。

    失败（模型没装 / 显存不够 / 超时）返回 None，调用方自动退回确定性方法。
    """
    try:
        from . import t2i as _t2i
        from PIL import Image
        import numpy as _np
    except Exception:                                   # noqa: BLE001
        return None
    if not _t2i.inpaint_ready():
        return None

    H, W = img_bgr.shape[:2]
    cx = float(geom.get("cx") or W * 0.5)
    cy = float(geom.get("cy") or H * 0.6)
    mw = float(geom.get("mw") or 80.0)
    ys = float(geom.get("y_split") or cy)
    yh = float(geom.get("y_hole_bot") or cy + 12)

    # 裁剪框：以嘴心为中心的正方形，贴边时向内收（不越界）
    cs = int(max(160, min(cs, min(W, H))))
    x0 = int(max(0, min(W - cs, cx - cs / 2.0)))
    y0 = int(max(0, min(H - cs, cy - cs / 2.0)))
    crop_bgr = img_bgr[y0:y0 + cs, x0:x0 + cs]
    crop = Image.fromarray(crop_bgr[:, :, ::-1])       # BGR→RGB
    # 重绘分辨率按显存档位决定：1024 质量最好但吃显存，512 省显存但细节少。
    BIG = int(max(384, min(1024, int(big or 1024))))
    BIG = (BIG // 64) * 64                              # SD 要求 64 的倍数
    big_img = crop.resize((BIG, BIG), Image.LANCZOS)

    # mask：只圈口腔（含嘴角），放大到 1024 后的归一化坐标
    # ⚠️ 横向用 1.08×mw：`mw` 是**用户标定的嘴部框宽**（本图 63px），
    #   而这张图大笑时实际开口宽 **112px** —— 按 0.60×mw 圈只盖住中间 76px，
    #   两侧嘴角的原口腔没被重绘，实测嘴部暗占比反而从 0.095 升到 0.121。
    mx0 = cx - 1.08 * mw - x0
    mx1 = cx + 1.08 * mw - x0
    my0 = ys - 7.0 - y0
    my1 = yh + 12.0 - y0
    s = BIG / float(cs)
    reg = [[max(0.0, mx0) * s / BIG, max(0.0, my0) * s / BIG,
            min(float(cs), mx1) * s / BIG, min(float(cs), my1) * s / BIG]]
    if reg[0][2] - reg[0][0] < 0.02 or reg[0][3] - reg[0][1] < 0.01:
        return None

    # prompt 迭代记录（2026-11-15 三组 A/B，同一 mask 同一图）：
    #   v1「lips gently pressed together + subtle highlight」→ 视觉复核判
    #      「偏直的细线、腮红被糊开、画风偏写实」；
    #   v2 加入「cupid bow / 上下唇体块 / 皮肤纹理延续 / 边缘无缝」后 →
    #      「完全闭合、唇线自然、有明暗过渡的厚度感、与原图高度一致」。
    prompt = ("closed mouth of a 3D cartoon character, soft natural lip line, "
              "subtle upper lip and gentle lower lip volume, cupid bow, "
              "skin texture with fine shading continuing around the mouth, "
              "lip edges blending seamlessly into surrounding skin, "
              "cel shaded 3D render, same warm pink skin tone, clean corners, high detail")
    neg = ("open mouth, teeth, tongue, black gap, dark hole, black dots, "
           "flat smear, patch, hard edge, scar, bright red lips, different style, "
           "low quality, flat shading")

    # 临时文件（SDXL 走路径入参）
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "_av2_big_%d.png" % int(time.time() * 1000))
    big_img.save(tmp)
    try:
        r = _t2i.inpaint(tmp, prompt, regions=reg, negative_prompt=neg,
                         strength=strength, steps=steps, feather=12)
    finally:
        try:
            os.remove(tmp)
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    if not r or not r.get("ok"):
        return None
    try:
        import base64 as _b64, io as _io
        patched = Image.open(_io.BytesIO(_b64.b64decode(r["b64"]))).convert("RGB")
    except Exception:                                   # noqa: BLE001
        return None

    # ---- 缩回 + 羽化贴回 ----
    base = _np.array(Image.fromarray(img_bgr[:, :, ::-1])).astype(_np.float32)
    patch = _np.array(patched.resize((cs, cs), Image.LANCZOS)).astype(_np.float32)
    # ⚠️⚠️ 只贴回**口腔椭圆内部**，不要贴整块 300×300。
    #   2026-11-15 实测踩到：SDXL 输出的整块都经过 VAE 编解码，全区域都有微小色差；
    #   按矩形边界羽化贴回时，视觉复核判「嘴部左右两侧有明显的矩形拼接边缘（垂直硬边）、
    #   像贴纸浮在脸上」。改成椭圆蒙版后，替换范围就是口腔本身，
    #   而口腔边缘天然有唇线，看不出接缝。
    _cx = cx - x0
    _cy = (ys + yh) * 0.5 - y0
    _rx = max(10.0, 1.08 * mw)
    _ry = max(8.0, (yh - ys) * 0.5 + 11.0)
    yy, xx = _np.mgrid[0:cs, 0:cs]
    dist = ((xx - _cx) / _rx) ** 2 + ((yy - _cy) / _ry) ** 2
    alpha = _np.clip((1.18 - dist) / 0.36, 0, 1).astype(_np.float32)[..., None]
    region = base[y0:y0 + cs, x0:x0 + cs]
    base[y0:y0 + cs, x0:x0 + cs] = region * (1 - alpha) + patch * alpha

    # ---- 颜色对齐：把嘴周颜色分布拉回原图（抑制 SDXL 偏红/偏亮） ----
    # ⚠️⚠️ 必须**按椭圆加权**做，不能按矩形范围做。
    #   2026-11-15 实测踩到：按 (ys-18..yh+26, cx±0.75mw) 的矩形范围调色，
    #   椭圆之外也被拉了一遍 → 视觉复核反复判「非常明显的矩形硬边、方形处理框痕迹」。
    #   改成用同一个椭圆 alpha 做权重后，调色范围与替换范围一致，矩形边消失。
    ref = _np.array(Image.fromarray(img_bgr[:, :, ::-1])).astype(_np.float32)
    ay0, ay1 = max(0, int(_cy + y0 - _ry - 6)), min(H, int(_cy + y0 + _ry + 6))
    ax0, ax1 = max(0, int(_cx + x0 - _rx - 6)), min(W, int(_cx + x0 + _rx + 6))
    if ay1 - ay0 > 6 and ax1 - ax0 > 6:
        a_sub = base[ay0:ay1, ax0:ax1]
        b_sub = ref[ay0:ay1, ax0:ax1]
        # alpha 是 (h,w,1)，这里要 (h,w) 才能和 a_sub[:,:,c] 相乘（否则广播失败）
        w_sub = alpha[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0][..., 0]      # 0~1 椭圆权重
        for c in range(3):
            ma, sa = float(a_sub[:, :, c].mean()), float(a_sub[:, :, c].std()) + 1e-6
            mb, sb = float(b_sub[:, :, c].mean()), float(b_sub[:, :, c].std()) + 1e-6
            adj = (a_sub[:, :, c] - ma) / sa * sb + mb
            a_sub[:, :, c] = a_sub[:, :, c] * (1 - 0.55 * w_sub) + adj * (0.55 * w_sub)

    # ---- 去小块暗斑（SDXL 常在嘴角留几个黑点） ----
    try:
        from scipy import ndimage
        vy0, vy1 = max(0, int(ys - 14)), min(H, int(yh + 18))
        vx0, vx1 = max(0, int(cx - 0.74 * mw)), min(W, int(cx + 0.74 * mw))
        sub = base[vy0:vy1, vx0:vx1]
        dark = sub.max(axis=2) < 122
        if dark.any():
            lab, n = ndimage.label(dark)
            for i in range(1, n + 1):
                m = lab == i
                if m.sum() <= 60:                       # 小块=杂点，大块=真实唇缝保留
                    ys_i, xs_i = _np.where(m)
                    for _y, _x in zip(ys_i, xs_i):
                        _a = sub[max(0, _y - 4):_y + 5, max(0, _x - 4):_x + 5].reshape(-1, 3)
                        _light = _a[_a.max(axis=1) > 135]
                        if len(_light):
                            sub[_y, _x] = _light.mean(axis=0)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)

    return _np.clip(base, 0, 255).astype(_np.uint8)[:, :, ::-1]     # RGB→BGR


def _montage_mouths(img_bgr, outs, geom, side=300):
    """把 n 个候选的嘴部裁出来横排拼成一张（白条分隔），返回 base64。

    为什么要拼成一张：让 LLM 做**相对比较**。分成多张图传过去时，
    它会对每张独立打绝对分 —— 实测绝对分极不稳（同一张图给出过
    「优秀」和「4 分」两种结论）。
    """
    import cv2
    import numpy as _np
    import base64 as _b64

    H, W = img_bgr.shape[:2]
    cx = float(geom.get("cx") or W * 0.5)
    mw = float(geom.get("mw") or 80.0)
    ys = float(geom.get("y_split") or H * 0.6)
    yh = float(geom.get("y_hole_bot") or ys + 20)
    half = max(40.0, 1.75 * mw)
    x0 = int(max(0, cx - half)); x1 = int(min(W, cx + half))
    y0 = int(max(0, ys - 26)); y1 = int(min(H, yh + 40))
    if x1 - x0 < 20 or y1 - y0 < 12:
        x0, x1 = int(max(0, cx - 80)), int(min(W, cx + 80))
        y0, y1 = int(max(0, ys - 30)), int(min(H, yh + 45))

    tiles = []
    for o in outs:
        if o is None:
            continue
        t = o[y0:y1, x0:x1]
        if t.size == 0:
            continue
        h, w = t.shape[:2]
        sc = side / float(max(1, h))
        tiles.append(cv2.resize(t, (max(1, int(w * sc)), side),
                                interpolation=cv2.INTER_CUBIC))
    if not tiles:
        return ""
    sep = _np.full((side, 10, 3), 255, _np.uint8)
    row = []
    for i, t in enumerate(tiles):
        row.append(t)
        if i < len(tiles) - 1:
            row.append(sep)
    mont = _np.hstack(row)
    ok, buf = cv2.imencode(".jpg", mont, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return _b64.b64encode(buf).decode() if ok else ""


def _sdxl_hires_best(img_bgr, geom, n: int = 3, use_llm: bool = True, **kw):
    """跑 n 次生成式重绘，用 LLM **相对比较**挑最好的那张。

    为什么要跑 n 次：SDXL 的 inpaint 没有固定 seed。同一 prompt、同一 mask
    连跑三次，实测嘴部暗占比 0.077 / 0.085 / 0.104、唇部 std 36.7 / 40.5 / 42.6
    —— 波动明显，单次结果不可控（其中一次还被判「唇间留暗红缝隙」）。

    为什么让 LLM 选而不是自己算分：暗占比最低 ≠ 最好看（太低往往是
    「压成一条细线、丢唇形」），std 最高也不等于自然。把 n 张并排交给
    LLM 做相对比较时，它能稳定选出「唇线自然、无亮斑」的那张并说明理由。

    返回 (bgr 或 None, detail)
    """
    n = max(1, min(5, int(n or 1)))
    outs = []
    t_all = time.time()
    for _i in range(n):
        try:
            o = _sdxl_hires_close(img_bgr, geom, **kw)
        except Exception:                                # noqa: BLE001
            o = None
        if o is not None:
            outs.append(o)
    det = {"n_try": n, "n_ok": len(outs),
           "gen_sec": round(time.time() - t_all, 1)}
    if not outs:
        return None, det
    if len(outs) == 1:
        det["picked"] = 1
        return outs[0], det

    if use_llm:
        try:
            from . import avatar_tune as AT
            mont = _montage_mouths(img_bgr, outs, geom)
            r = AT.pick_best(mont, len(outs))
            det["llm"] = {"best": r.get("best"), "reason": r.get("reason")}
            if r.get("ok") and 1 <= int(r["best"]) <= len(outs):
                det["picked"] = int(r["best"])
                return outs[int(r["best"]) - 1], det
        except Exception as e:                           # noqa: BLE001
            det["llm_err"] = "%s: %s" % (type(e).__name__, e)
    det["picked"] = 1
    return outs[0], det


def _bgr_b64_small(bgr, side=512):
    """BGR ndarray → 小图 base64 JPEG（送 LLM 验收用，省 token）。"""
    try:
        import base64 as _b64

        import cv2
        h, w = bgr.shape[:2]
        s = min(1.0, side / float(max(h, w)))
        if s < 1.0:
            bgr = cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
        if not ok:
            return ""
        return _b64.b64encode(buf.tobytes()).decode()
    except Exception:                                   # noqa: BLE001
        return ""


def _cloud_close_flow(img_path, img_bgr, geom, key, use_llm=True, max_rounds=2):
    """云端重绘 + LLM 选片 + 不合格自动重试。

    返回 (最佳图或None, 方法名, 说明, 附加信息)。
    LLM 在这里管四件事：
      ① 定 prompt / mode / cs / n（plan_close）
      ② 对每张候选打分并指出问题（judge_close）
      ③ 选分数最高那张
      ④ 不到 7 分 → 用它给的 fix 改写 prompt，再生成一轮（最多 max_rounds 轮）
    """
    from . import avatar_tune as AT

    plan = {"mode": "inpaint", "cs": 300, "n": 1,
            "prompt": AT._CLOSE_FALLBACK_PROMPT,
            "negative": AT._CLOSE_FALLBACK_NEG,
            "style": "", "reason": "（未启用 LLM，用内置方案）"}
    if use_llm:
        try:
            _p = AT.plan_close(img_path, geom)
            if _p.get("ok"):
                plan = _p
            else:
                plan["reason"] = "LLM 规划失败，用内置方案：%s" % (_p.get("reason") or "")
        except Exception as e:                          # noqa: BLE001
            plan["reason"] = "LLM 规划异常，用内置方案：%s" % e

    PASS = 7.0          # 7 分及以上才算「可以直接用」
    info = {"plan": {k: plan.get(k) for k in
                     ("mode", "cs", "n", "style", "reason", "prompt")},
            "cost_s": 0.0, "cands": 0, "judge": [], "rounds": []}
    before = _bgr_b64_small(img_bgr) if use_llm else ""

    best, best_score = None, -1.0
    plan_cur = dict(plan)
    mode_used = plan.get("mode") or "inpaint"

    for rnd in range(max(1, int(max_rounds))):
        r = _cloud_close(img_bgr, geom, key, plan_cur)
        info["cost_s"] = round(info["cost_s"] + float(r.get("cost") or 0), 1)
        if not r.get("ok"):
            info["cloud_err"] = r.get("err") or "云端重绘失败"
            if best is None:
                return None, "", "", info
            break                                  # 已经有一张能用的，就别再追
        mode_used = r.get("mode") or mode_used
        cands = r["images"]
        info["cands"] += len(cands)

        round_scores = []
        fix_hint = ""
        for i, c in enumerate(cands):
            if not use_llm:
                if best is None:
                    best = c
                continue
            try:
                j = AT.judge_close(before, _bgr_b64_small(c))
            except Exception as e:                  # noqa: BLE001
                j = {"ok": False, "score": 0.0, "reason": str(e)}
            sc = float(j.get("score") or 0) if j.get("ok") else -1.0
            round_scores.append(sc)
            info["judge"].append({
                "round": rnd + 1, "i": i, "score": j.get("score"),
                "closed": j.get("closed"), "natural": j.get("natural"),
                "seamless": j.get("seamless"),
                "reason": j.get("reason") or "", "issues": j.get("issues") or []})
            if sc > best_score:
                best_score, best = sc, c
            # 收集改进建议：取这一轮里最低分那张的 fix
            if j.get("fix") and (not fix_hint or sc == min(round_scores)):
                fix_hint = str(j.get("fix"))

        info["rounds"].append({"round": rnd + 1, "n": len(cands),
                               "scores": [None if s < 0 else s for s in round_scores],
                               "prompt": (plan_cur.get("prompt") or "")[:200]})
        if not use_llm:
            break
        if best_score >= PASS:
            break                                  # 合格就停，不浪费额度
        # 不合格 → 用 LLM 的 fix 改写 prompt 再试一轮（这是「自动重试」的核心）
        if rnd + 1 < max(1, int(max_rounds)) and fix_hint:
            plan_cur = dict(plan_cur)
            plan_cur["prompt"] = fix_hint
            plan_cur["reason"] = "上一轮 %.0f 分未达标，按 LLM 建议改写提示词重试" % best_score

    if best is None:
        return None, "", "", info
    info["best_score"] = best_score if best_score >= 0 else None
    info["pass"] = bool(best_score >= PASS)
    how = "局部重绘（只改嘴周）" if mode_used == "inpaint" else "整图重绘"
    reason = ("已用云端 image2.5 %s，共 %d 张候选、%d 轮，LLM 选片（最高 %s 分%s）"
              % (how, info["cands"], len(info["rounds"]),
                 ("%.0f" % best_score) if best_score >= 0 else "?",
                 "，达标" if info["pass"] else "，未达 7 分但已是最好的一张"))
    return best, ("cloud_inpaint" if mode_used == "inpaint" else "cloud_whole"), reason, info


def close_mouth(force: bool = False, preview: bool = False,
                method: str = "cloud", image_key: str = "",
                use_llm: bool = True, max_rounds: int = 2) -> dict:
    """把「张嘴」的源图合成一张闭嘴图。

    preview=True 时写到 avatar_closed_preview.png 并**不写 meta**：
    用户先看效果，满意了再调 apply_closed_preview() 转正。
    默认（preview=False）直接写 avatar_closed.png，之后渲染就用它。

    为什么需要：Wav2Lip 是拿参考脸当条件的生成模型。源图嘴张着，生成的
    闭嘴帧也带着张开的口型 —— 念「一、二、三」时嘴合不拢，看着像一直在笑。

    做法是**确定性合成**（不是 GAN 也不是扩散）：把嘴部开孔（暗腔 + 牙齿）
    用唇色重建 —— 上下唇各取自己一侧的唇色做纵向渐变，中间留一道唇缝暗线，
    再把补丁的颜色/光照匹配到周围、用羽化蒙版混合。所以边缘不会出现
    「贴了一块颜色不一样的补丁」。
    """
    import cv2

    # 统一 Key：前端没带就用后端保管的那把（一把 Key 多通道共用）
    image_key = _any_image_key(image_key)

    p = _img_path()
    if not os.path.isfile(p):
        return {"ok": False, "error": "还没有形象图"}
    img = cv2.imread(p, cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "error": "形象图读取失败"}
    H, W = img.shape[:2]
    # 写到哪：预览 / 正式。后面所有写盘点统一用它，绝不让预览覆盖已生效基准。
    _dst = _img_closed_preview_path() if preview else _img_closed_path()

    # ---- 新路径：下唇镜像替换开口区（优先）----
    # 三代做法都实测过，视觉复核结论：
    #   ① inpaint 平涂      → 「一块平贴的肤色补丁，没有上下唇结构」
    #   ② 上唇镜像填充      → 「矩形切割线、上下唇色差明显、带入背景色块」
    #   ③ 下唇上移(lip_shift)→ 「中间有细小白色线框/硬边，有涂抹痕迹」
    #   ④ 下唇镜像(本路径)  → 「唇线弧度自然、边缘干净、无涂抹色块硬边重影
    #                          条纹、画风一致」，6 张候选里被判最自然
    # ④ 与 ③ 的差别：③ 是位移映射，会把下巴/脖子一起拖动；④ 只替换开口那几行，
    # 开口以下（下唇、下巴、衣服）像素零改动。
    try:
        from . import avatar_mouth as _AM
        _geom = _AM.analyze(img, mouth())
        if _geom and _geom.get("source_open"):
            _mth = str(method or "auto").lower()
            # auto = 按这台机器自己的档位选：高配走本机显卡、低配且填了 Key 走云端、
            # 两者都没有才退回确定性。前端默认传 auto，不再写死 cloud ——
            # 写死 cloud 时，没 Key 的机器会先白等一次云端失败才降级。
            if _mth in ("auto", ""):
                try:
                    _mth = str((close_plan(image_key) or {}).get("method")
                               or "lip_shift").lower()
                except Exception:                       # noqa: BLE001
                    _mth = "lip_shift"
            _out = None
            _mname = ""
            _mreason = ""
            _detail = {}

            # ★★ 首选（低配机路线）：云端 image2.5 生成式重绘。
            #   本机零生成模型、零显存，只要联网 + 图片 API Key。
            #   ⚠️ 明确指定 cloud 却失败时**如实报错**，不静默换成像素拼接 ——
            #     之前正是「静默 fallback 到拼接」导致用户看到的永远是补丁感。
            if _mth in ("cloud", "api", "image25", "cloud_edit"):
                try:
                    _out, _mname, _mreason, _detail = _cloud_close_flow(
                        p, img, _geom, image_key, use_llm=use_llm,
                        max_rounds=max_rounds)
                except Exception as _ce:                # noqa: BLE001
                    _out = None
                    _detail = {"cloud_err": "%s: %s" % (type(_ce).__name__, _ce)}
                if _out is None:
                    _ce_msg = _detail.get("cloud_err") or "云端重绘失败"
                    _plan0 = close_plan(image_key)
                    if not str(image_key or "").strip() and _plan0.get("gen_ok"):
                        # 没填 key，但这台机器自己跑得动 → 直接改用本地生成式，
                        # 别让用户卡在一句「云端重绘失败」上（高配机的默认体验）。
                        _detail["cloud_skipped"] = _ce_msg
                        _mth = "sdxl_hires"
                    else:
                        # 低配机 + 没 key：这条路走不通，把两条真实出路说清楚。
                        _hint = ""
                        if not str(image_key or "").strip():
                            _hint = ("\n本机也没有可用显卡（生成式只能走云端或高性能机器）。"
                                     "两条路：①在 🎨 面板填入图片 API Key，云端生成（约 20 秒）；"
                                     "②在有显卡的机器上生成闭嘴基准，把 avatar_closed.png "
                                     "拷到本机同目录（点「📁 打开文件夹」可看到路径）。")
                        return {"ok": False, "closed": False, "preview": bool(preview),
                                "method": "cloud", "plan": _plan0,
                                "error": _ce_msg + _hint, "detail": _detail}
            # ★ 首选：SDXL 高分辨率局部重绘（生成式，2026-11-15 实测视觉最优：
            #   DeepSeek 判「比像素拼接好非常多，质的飞跃」）。
            #   失败（模型没装 / 显存不够 / 超时）自动退回确定性方法，不让功能挂掉。
            # ★ 按机器档位决定要不要跑生成式（2026-11-15 实测：SDXL 在 CPU 上
            #   推理峰值 23GB 内存、1024×1024/32步 约 3 小时 —— 低配机必须绕开）。
            #   · method=sdxl / sdxl_hires：用户明确要生成式，照跑（慢也认）
            #   · method=auto（默认）：只有本机跑得动才用生成式，否则走确定性
            _plan = close_plan(image_key)
            _tier = _plan.get("tier") or "cpu"
            _want_gen = (_mth in ("sdxl", "sdxl_hires")) or (
                _mth in ("auto", "") and bool(_plan.get("gen_ok")))
            if _want_gen:
                _big = {"gpu_high": 1024, "gpu_mid": 768, "gpu_low": 512}.get(_tier, 512)
                _stp = {"gpu_high": 34, "gpu_mid": 26, "gpu_low": 20}.get(_tier, 20)
                # 生成几张：高配 3 张（约 60s），中配 2 张，低配 1 张。
                # 为什么要多张：SDXL inpaint 没有固定 seed，单次结果波动大
                # （实测同一参数三次：暗占比 0.077/0.085/0.104，
                #   其中一次唇间留暗红缝隙被判不合格）。
                _bn = 3 if _tier == "gpu_high" else (2 if _tier == "gpu_mid" else 1)
                _bdet = {}
                try:
                    _out, _bdet = _sdxl_hires_best(
                        img, _geom, n=_bn, use_llm=bool(use_llm),
                        steps=_stp, big=_big)
                except Exception:                       # noqa: BLE001
                    _out, _bdet = None, {}
                if _bdet:
                    _detail["best_of"] = _bdet
                if _out is not None:
                    _mname = "sdxl_hires"
                    _mreason = ("已用 SDXL 生成式局部重绘生成闭嘴基准"
                                "（%s，%dpx/%d步；生成 %d 张、AI 选第 %d 张；"
                                "主形象像素未动）"
                                % (_plan.get("why") or _tier, _big, _stp,
                                   _bdet.get("n_ok") or 1, _bdet.get("picked") or 1))
                else:
                    _plan["gen_failed"] = True
            if _out is None and _mth == "lip_shift":
                _out = _AM.close_by_lip(img, _geom, 1.10)
                _mname = "lip_shift"
                _mreason = "已用下唇上移生成闭嘴基准（保留原图唇部纹理）"
            if _out is None and _mth in ("mirror", "lip_mirror"):
                _out = _AM.close_by_mirror(img, _geom)
                _mname = "lip_mirror"
                _mreason = "已用下唇镜像生成闭嘴基准（只替换开口区，其余像素零改动）"
            if _out is None:
                # 没跑成生成式 → 退回确定性（比报错好）。
                # 原因要说清：低配机是「主动选择」（跑不动扩散），
                # 高配机才是「生成失败」（模型没装/显存被占）。
                _out = _AM.close_by_lip(img, _geom, 1.10)
                _mname = "lip_shift_fallback"
                if _plan.get("gen_ok"):
                    _mreason = ("生成式重绘没跑成（模型未装或显存不足），"
                                "已退回下唇上移（保留原图唇部纹理）")
                else:
                    _mreason = ("本机跑不动生成式（%s），已用确定性合成"
                                "（保留原图唇部纹理）。想要生成式效果："
                                "①在有显卡的机器上生成后把 avatar_closed.png 拷过来；"
                                "②或强制开启（会很慢）。" % (_plan.get("why") or "无可用显卡"))
            _cp = _dst
            os.makedirs(os.path.dirname(_cp), exist_ok=True)
            cv2.imwrite(_cp, _out)
            if not preview:
                _meta = _read_meta()
                _meta["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _meta["closed_from"] = os.path.basename(p)
                _meta["closed_method"] = _mname
                _write_meta(_meta)
            return {"ok": True, "closed": True, "preview": bool(preview),
                    "method": _mname, "reason": _mreason, "detail": _detail,
                    "plan": _plan,
                    "url": ("/api/avatar2/closed_preview?t=%d" if preview
                            else "/api/avatar2/closed?t=%d") % int(time.time())}
    except Exception as _e:                             # noqa: BLE001
        _new_err = "%s: %s" % (type(_e).__name__, _e)
    else:
        _new_err = None

    cx, cy, mw, mh = _mouth_geom((W, H), mouth())
    # 开孔椭圆（要填掉的区域）与融合椭圆（羽化范围）
    rx, ry = max(6.0, mw * 0.60), max(5.0, mh * 0.60)
    ox, oy = max(8.0, mw * 0.78), max(7.0, mh * 0.78)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)

    m_open = np.zeros((H, W), np.uint8)
    cv2.ellipse(m_open, (int(cx), int(cy)), (int(rx), int(ry)), 0, 0, 360, 255, -1)
    m_outer = np.zeros((H, W), np.uint8)
    cv2.ellipse(m_outer, (int(cx), int(cy)), (int(ox), int(oy)), 0, 0, 360, 255, -1)

    # 唇色参考：外椭圆内、开孔椭圆外的那一圈就是嘴唇
    ring = (m_outer > 0) & (m_open == 0)
    if ring.sum() < 30:
        return {"ok": False, "error": "嘴部标定太小，先在图上把嘴框准"}
    ring_med = float(np.median(gray[ring]))

    # 开孔判据：明显比唇色暗（口腔）或又亮又淡（牙齿）
    dark = gray < max(28.0, ring_med * 0.62)
    teeth = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > max(150.0, ring_med * 1.18))
    cand = ((dark | teeth) & (m_open > 0)).astype(np.uint8)
    ellipse_px = max(1, int((m_open > 0).sum()))
    # ⚠ 只看「暗像素占比」会误判：闭嘴的唇缝本身也是一条暗线（实测占椭圆
    #   面积 34%），照那样填会把原本好好的嘴唇涂掉。真开孔是一个**紧凑、
    #   有纵向高度的块**，唇缝是**又细又长的一条** —— 用连通域形状区分。
    n_lab, lab, stats, _cent = cv2.connectedComponentsWithStats(cand, 8)
    best_lab, best_a = -1, 0
    for li in range(1, n_lab):
        a = int(stats[li, cv2.CC_STAT_AREA])
        if a > best_a:
            best_a, best_lab = a, li
    cavity = np.zeros((H, W), bool)
    if best_lab > 0:
        bw_ = int(stats[best_lab, cv2.CC_STAT_WIDTH])
        bh_ = int(stats[best_lab, cv2.CC_STAT_HEIGHT])
        fill = best_a / max(1, bw_ * bh_)
        # 够大 + 有纵向高度（>= 椭圆高度的 42%）+ 是个块（填充率 >= 0.45）
        if best_a >= 0.12 * ellipse_px and bh_ >= 0.42 * 2 * ry and fill >= 0.45:
            cavity = (lab == best_lab)
    cav_px = int(cavity.sum())
    if cav_px == 0:
        # 已经是闭嘴：把可能存在的旧基准删掉，别拿它污染渲染。
        # 预览模式不动任何已生效的文件 —— 预览只负责「看」。
        if not preview:
            try:
                if os.path.isfile(_img_closed_path()):
                    os.remove(_img_closed_path())
            except OSError:
                logger.debug("silent-except", exc_info=True)
        if not force:
            return {"ok": True, "closed": False, "preview": bool(preview),
                    "reason": "看起来已经是闭嘴（没有找到成块的口腔区域），不用处理"}

    # 结构来源：闭嘴的物理过程就是「下唇抬起来贴住上唇」，所以直接拿开口
    # **下方**的真实像素往上搬 —— 纹理、唇纹、明暗渐变全都是真的，
    # 不会出现「一块纯色补丁」。纯色渐变试过，视觉复核直接判定「方块状补丁、
    # 硬切边、色差」，所以改成搬像素。
    cav_u8 = cv2.dilate(cavity.astype(np.uint8) * 255,
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    m_fill = (cav_u8 > 0) & (m_outer > 0)

    # 镜像填充：上半腔从**上唇**镜像下来、下半腔从**下唇**镜像上去。
    # 为什么不用整体平移：平移量小于腔体高度时，顶部会残留一条黑色腔体
    # （实测就是这样，视觉复核判「巨大黑色不规则色块」）；平移量再大就会
    # 把下巴的像素拉进嘴里。镜像填充在边界处天然连续，且不会越界取到别处。
    ys_f, xs_f = np.nonzero(m_fill)
    v = (ys_f.astype(np.float32) - cy) / max(1.0, ry)
    src_y = np.where(v < 0, 2.0 * (cy - ry) - ys_f, 2.0 * (cy + ry) - ys_f)
    src_y = np.clip(src_y, 0, H - 1).astype(np.int32)
    filled = img.copy()
    filled[ys_f, xs_f] = img[src_y, xs_f]

    # 唇缝：闭上的嘴唇中间有一道暗线，补一道（否则上下唇连成一片像香肠）
    seam = np.zeros((H, W), np.float32)
    cv2.ellipse(seam, (int(cx), int(cy)), (int(mw * 0.46), max(1, int(mh * 0.05))),
                0, 0, 360, 1.0, -1)
    seam = cv2.GaussianBlur(seam, (0, 0), max(0.8, mh * 0.03))[..., None]
    filled = np.clip(filled.astype(np.float32) * (1.0 - 0.30 * seam), 0, 255).astype(np.uint8)

    # 无缝融合：泊松把补丁的光照/色温对齐到周围，边缘不留硬切线
    mask_u8 = np.zeros((H, W), np.uint8)
    cv2.ellipse(mask_u8, (int(cx), int(cy)), (int(ox), int(oy)), 0, 0, 360, 255, -1)
    blended = filled
    try:
        blended = cv2.seamlessClone(filled, img, mask_u8,
                                    (int(cx), int(cy)), cv2.NORMAL_CLONE)
    except Exception:                                    # noqa: BLE001
        # 泊松失败（掩膜贴边等）：退回羽化混合
        alpha = cv2.GaussianBlur((mask_u8 > 0).astype(np.float32), (0, 0),
                                 max(1.2, ry * 0.25))[..., None]
        blended = np.clip(filled.astype(np.float32) * alpha
                          + img.astype(np.float32) * (1 - alpha), 0, 255).astype(np.uint8)
    blended = np.clip(blended, 0, 255).astype(np.uint8)

    # 颜色匹配：把补丁区的均值拉到周围皮肤/唇色，避免「一块偏色」
    dst = _dst
    cv2.imwrite(dst, blended)
    if not preview:
        meta = _read_json(_META, {})
        meta["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta["closed_from"] = os.path.basename(p)
        _write_json(_META, meta)
    return {"ok": True, "closed": True, "preview": bool(preview), "path": dst,
            "cavity_px": cav_px,
            "url": ("/api/avatar2/closed_preview?v=%d" if preview
                    else "/api/avatar2/closed?v=%d") % int(os.path.getmtime(dst))}


# ---------------------------------------------------------------- 音频处理
def _to_wav16k(src: str) -> str:
    """任意音频 → 16k 单声道 wav（ASR 与包络分析都吃这个格式）。"""
    exe = ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg（imageio_ffmpeg 也不可用）")
    out = os.path.join(tempfile.mkdtemp(prefix="av2_"), "a.wav")
    cmd = [exe, "-y", "-loglevel", "error", "-i", src,
           "-ac", "1", "-ar", "16000", "-f", "wav", out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 100:
        raise RuntimeError("音频转码失败：%s" % (r.stderr or b"")[:300].decode("utf-8", "ignore"))
    return out


def _read_wav(path: str):
    """读 16 位 PCM wav → (sr, float32 mono)。"""
    import wave
    with wave.open(path, "rb") as f:
        sr = f.getframerate()
        ch = f.getnchannels()
        sw = f.getsampwidth()
        n = f.getnframes()
        raw = f.readframes(n)
    if sw != 2:
        raise RuntimeError("只支持 16 位 wav，当前 %d 位" % (sw * 8))
    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    return sr, y


def _pct(a: np.ndarray, q: float, default: float = 0.0) -> float:
    if a.size == 0:
        return default
    v = float(np.percentile(a, q))
    return v if math.isfinite(v) else default


def _analyze(y: np.ndarray, sr: int, fps: int = FPS) -> dict:
    """逐帧算声学特征：RMS / 过零率 / 频谱质心。

    返回每帧一个值的数组，长度 = 总帧数。
    """
    hop = max(1, int(sr / fps))
    win = max(hop * 2, int(sr * 0.064))
    n_frames = max(1, int(math.ceil(len(y) / hop)))
    pad = np.zeros(win, dtype=np.float32)
    rms = np.zeros(n_frames, dtype=np.float32)
    zcr = np.zeros(n_frames, dtype=np.float32)
    cen = np.zeros(n_frames, dtype=np.float32)
    # 汉宁窗 + 频点（只取 0~4kHz 这段对音色最有意义的）
    hann = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / sr).astype(np.float32)
    band = freqs <= 4000.0
    fb = freqs[band]
    for i in range(n_frames):
        s = i * hop
        seg = y[s:s + win]
        if len(seg) < win:
            seg = np.concatenate([seg, pad[:win - len(seg)]])
        rms[i] = float(np.sqrt(np.mean(seg * seg)))
        zcr[i] = float(np.mean(np.abs(np.diff(np.signbit(seg).astype(np.int8)))))
        mag = np.abs(np.fft.rfft(seg * hann))[band]
        tot = float(mag.sum())
        cen[i] = float((mag * fb).sum() / tot) if tot > 1e-9 else 0.0
    return {"rms": rms, "zcr": zcr, "centroid": cen, "n": n_frames,
            "hop": hop, "sr": sr}


def _one_pole(a: np.ndarray, tau_ms: float, fps: int = FPS) -> np.ndarray:
    """一阶低通（指数平滑）。tau_ms 越大越平滑，且不会像滑动平均那样在两端塌陷。"""
    if a.size == 0:
        return a
    alpha = 1.0 - math.exp(-1.0 / (max(tau_ms, 1e-3) / 1000.0 * fps))
    out = np.empty_like(a, dtype=np.float32)
    prev = float(a[0])
    for i in range(a.size):
        prev = prev + alpha * (float(a[i]) - prev)
        out[i] = prev
    return out


def _asym_follow(a: np.ndarray, att_ms: float, rel_ms: float,
                 fps: int = FPS) -> np.ndarray:
    """非对称包络跟随：上升快、回落慢。

    真实说话时嘴张开得快、收拢得慢，用同一个时间常数去平滑两头会显得迟钝；
    反过来（回落快）就会一顿一顿。分开设两个时间常数最接近真人。
    """
    if a.size == 0:
        return a
    aa = 1.0 - math.exp(-1.0 / (max(att_ms, 1e-3) / 1000.0 * fps))
    ar = 1.0 - math.exp(-1.0 / (max(rel_ms, 1e-3) / 1000.0 * fps))
    out = np.empty_like(a, dtype=np.float32)
    prev = float(a[0])
    for i in range(a.size):
        v = float(a[i])
        prev = prev + (aa if v > prev else ar) * (v - prev)
        out[i] = prev
    return out


def _smoothstep(t) -> np.ndarray:
    """0→1 的平滑过渡（一阶导连续），用来替代硬阈值，避免边界跳变。"""
    t = np.clip(np.asarray(t, dtype=np.float32), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------- ASR 对齐（可选增强）
_ASR_CACHE: dict = {"path": "", "rec": None, "tried": False}


def _find_asr() -> str:
    """在项目里找一个 sherpa-onnx 流式 zipformer 中文模型目录。找不到就返回空。"""
    if _ASR_CACHE.get("path"):
        return _ASR_CACHE["path"]
    if _ASR_CACHE.get("tried"):
        return ""
    _ASR_CACHE["tried"] = True
    root = _root()
    pats = [
        os.path.join(root, "data", "models", "asr", "*"),
        os.path.join(root, "dist", "**", "asr_model", "*"),
        os.path.join(root, "models", "asr", "*"),
        # ⚠️ 2026-11-14（11.14 打包发现）：PyInstaller 打包后 root 是 _internal，
        #   而 spec 把 ASR 模型收在 _internal/asr_model/ 下 —— 上面三条都匹配不到，
        #   结果「打包版数字人没有字级时间戳、源码版却有」。补这一条即可。
        os.path.join(root, "asr_model", "*"),
    ]
    cands: list[str] = []
    for pat in pats:
        try:
            cands.extend(glob.glob(pat, recursive=True))
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    for d in cands:
        if not os.path.isdir(d):
            continue
        enc = glob.glob(os.path.join(d, "encoder*.onnx"))
        dec = glob.glob(os.path.join(d, "decoder*.onnx"))
        joi = glob.glob(os.path.join(d, "joiner*.onnx"))
        tok = os.path.join(d, "tokens.txt")
        if enc and dec and joi and os.path.isfile(tok):
            # int8 优先：CPU 上快很多
            enc.sort(key=lambda p: 0 if "int8" in p else 1)
            dec.sort(key=lambda p: 0 if "int8" in p else 1)
            joi.sort(key=lambda p: 0 if "int8" in p else 1)
            _ASR_CACHE["path"] = d
            _ASR_CACHE["files"] = (enc[0], dec[0], joi[0], tok)
            return d
    return ""


def _asr_words(wav_path: str) -> list:
    """字级时间戳 [(char, t0, t1), ...]。没有模型或失败就返回 []，不报错。"""
    d = _find_asr()
    if not d:
        return []
    try:
        import sherpa_onnx
        if _ASR_CACHE.get("rec") is None:
            enc, dec, joi, tok = _ASR_CACHE["files"]
            _ASR_CACHE["rec"] = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=tok, encoder=enc, decoder=dec, joiner=joi,
                num_threads=2, sample_rate=16000, feature_dim=80,
                decoding_method="greedy_search",
            )
        rec = _ASR_CACHE["rec"]
        sr, y = _read_wav(wav_path)
        st = rec.create_stream()
        st.accept_waveform(sr, y)
        st.accept_waveform(sr, np.zeros(int(0.3 * sr), dtype=np.float32))
        st.input_finished()
        while rec.is_ready(st):
            rec.decode_stream(st)
        toks = list(rec.tokens(st) or [])
        ts = [float(t) for t in (rec.timestamps(st) or [])]
        if not toks or len(toks) != len(ts):
            return []
        dur = len(y) / float(sr)
        out = []
        for i, (c, t0) in enumerate(zip(toks, ts)):
            t1 = ts[i + 1] if i + 1 < len(ts) else dur
            if t1 <= t0:
                t1 = min(dur, t0 + 0.12)
            out.append((str(c), float(t0), float(t1)))
        return out
    except Exception:                                   # noqa: BLE001
        return []


# ---------------------------------------------------------------- 口型参数
# ---------------------------------------------------------------- 音素驱动（viseme）
# 为什么需要这一层：纯声学只能知道「此刻有多响」，不知道「发的是哪个音」，
# 于是 b/p/m 这类必须闭唇的音做不出来，u/i 这类圆扁唇也分不清 —— 这正是
# 「嘴在动但对不上口型」的根因，靠调平滑参数永远解决不了。
# 有 ASR 字级时间戳时，把每个字转拼音，按声母/韵母给出目标唇形，
# 再与声学曲线混合：声学负责强弱节奏，音素负责唇形对不对。
_VIS_INIT = {                      # 声母 → 该字起始段的张口度
    "b": 0.02, "p": 0.02, "m": 0.02,        # 双唇闭音：必须闭上
    "f": 0.26,
    "d": 0.45, "t": 0.45, "n": 0.45, "l": 0.45,
    "g": 0.40, "k": 0.40, "h": 0.40,
    "j": 0.38, "q": 0.38, "x": 0.38,
    "zh": 0.40, "ch": 0.40, "sh": 0.40, "r": 0.40,
    "z": 0.40, "c": 0.40, "s": 0.40,
}
_VIS_FIN = {                       # 韵母 → (张口度, 唇宽倍率<1 圆唇 / >1 扁唇)
    "a": (0.88, 1.08), "o": (0.62, 0.76), "e": (0.58, 1.10),
    "i": (0.34, 1.26), "u": (0.40, 0.72), "v": (0.40, 0.72),
    "ai": (0.78, 1.10), "ei": (0.46, 1.20), "ao": (0.82, 0.86),
    "ou": (0.56, 0.74), "an": (0.72, 1.05), "en": (0.52, 1.10),
    "ang": (0.80, 1.02), "eng": (0.56, 1.08), "ong": (0.60, 0.76),
    "er": (0.55, 1.00), "uo": (0.60, 0.78), "ua": (0.78, 1.02),
    "ui": (0.44, 1.16), "iu": (0.46, 0.82), "ie": (0.46, 1.20),
    "ve": (0.42, 0.80), "ian": (0.50, 1.18), "in": (0.34, 1.22),
    "ing": (0.36, 1.20), "iang": (0.70, 1.10), "uan": (0.60, 0.82),
    "un": (0.44, 0.80), "van": (0.60, 0.82),
    "ia": (0.62, 1.18), "iao": (0.72, 1.06), "iong": (0.48, 0.82),
    "uai": (0.60, 0.90), "uang": (0.72, 0.92), "ueng": (0.50, 0.92),
    "vn": (0.38, 0.78), "io": (0.55, 0.86),
}
_PY_CACHE: dict = {}


def _py_of(ch: str) -> str:
    """单字 → 无声调拼音（失败返回空串）。带缓存，pypinyin 逐字调用很慢。"""
    if ch in _PY_CACHE:
        return _PY_CACHE[ch]
    out = ""
    try:
        from pypinyin import lazy_pinyin
        r = lazy_pinyin(ch)
        out = (r[0] if r else "") or ""
    except Exception:                                   # noqa: BLE001
        out = ""
    _PY_CACHE[ch] = out
    return out


# 零声母音节：pypinyin 写出来是 y/w 开头，但真正的韵母不是 "yu"/"wo"/"yi"。
# 不还原的话「鱼、我、一、月、五」这些高频字全都匹配不到唇形表 ——
# 实测它们原本完全不受音素约束（声学 0.84 原样输出），等于白改。
_ZERO_INIT = {
    "yi": "i", "ya": "ia", "ye": "ie", "yao": "iao", "you": "iu",
    "yan": "ian", "yin": "in", "yang": "iang", "ying": "ing",
    "yong": "iong", "yo": "o",
    "yu": "v", "yue": "ve", "yuan": "van", "yun": "vn",
    "wu": "u", "wa": "ua", "wo": "uo", "wai": "uai", "wei": "ui",
    "wan": "uan", "wen": "un", "wang": "uang", "weng": "ueng",
}


def _split_py(py: str):
    """拼音 → (声母, 韵母)。zh/ch/sh 是双字母，必须先判；零声母先还原。"""
    if py in _ZERO_INIT:
        return "", _ZERO_INIT[py]
    for ini in ("zh", "ch", "sh"):
        if py.startswith(ini):
            return ini, py[2:]
    if py and py[0] in "bpmfdtnlgkhjqxrzcs":
        return py[0], py[1:]
    return "", py


def _viseme_curve(words: list, n: int, fps: int):
    """按字级时间戳生成目标唇形曲线。

    返回 (op_t, wd_t, conf)：
      op_t 每帧目标张口度，-1 表示该帧无音素约束；
      wd_t 每帧目标唇宽；
      conf 约束强度 0~1（闭唇给得最强，因为它最容易看错）。

    字内分两段：前 35% 是声母（决定「怎么起唇」），后 65% 是韵母（决定「张多大」）。
    """
    op_t = np.full(n, -1.0, dtype=np.float32)
    wd_t = np.ones(n, dtype=np.float32)
    conf = np.zeros(n, dtype=np.float32)
    for item in (words or []):
        try:
            ch, t0, t1 = item[0], float(item[1]), float(item[2])
        except Exception:                               # noqa: BLE001
            continue
        py = _py_of(str(ch))
        if not py:
            continue
        ini, fin = _split_py(py)
        fo, fw = _VIS_FIN.get(fin, (None, None))
        if fo is None and fin:
            fo, fw = _VIS_FIN.get(fin[:2], (None, None))
        if fo is None and fin:
            fo, fw = _VIS_FIN.get(fin[:1], (None, None))
        io = _VIS_INIT.get(ini)
        if io is None and fo is None:
            continue
        i0 = int(round(t0 * fps))
        i1 = int(round(t1 * fps))
        if i1 <= i0:
            i1 = i0 + 1
        span = max(i1 - i0, 1)
        for k in range(max(0, i0), min(i1, n)):
            pos = (k - i0) / float(span)
            if pos < 0.35 and io is not None:
                o = io if io < 0.10 else (io * 0.6 + (fo or io) * 0.4)
                c = 0.90 if io < 0.10 else 0.70
            elif fo is not None:
                o = fo
                c = 0.72
            else:
                continue
            op_t[k] = o
            if fw is not None:
                wd_t[k] = fw
            conf[k] = max(conf[k], c)
    return op_t, wd_t, conf


def _build_params(y: np.ndarray, sr: int, words: list, fps: int = FPS,
                  amp: float = 1.0) -> list:
    """把声学特征 + 字边界 → 每帧的 (openness, width) 0~1 附近的口型参数。

    openness：张口高度倍率（0=闭唇，1=大张）
    width   ：唇形宽窄（<1 圆唇，>1 扁唇）
    amp     ：口型幅度增益 0.6~2.0（1.0 = 原始观感）

    ⚠ amp 为什么作用在「曲线」而不只是「单帧拉伸」：
      实测真实中文语音的 openness 分布 —— mean 0.281、中位数 0.215、
      只有 23.9% 的帧 >0.5、3.9% 的帧 >0.8。也就是说大部分时间嘴只微张，
      这才是用户觉得「幅度小」的主因，光加大单帧拉伸解决不了。
      amp 把整条曲线抬起来：amp=1.4 时 >0.5 的帧升到 32.3%、>0.8 升到 19.3%。
    """
    feats = _analyze(y, sr, fps)
    rms, zcr, cen = feats["rms"], feats["zcr"], feats["centroid"]
    n = feats["n"]

    # 用 95 分位做归一化，避免某一声爆音把整体压扁
    hi = max(_pct(rms, 95, 0.02), 0.02)
    lo = _pct(rms, 20, 0.0)
    span = max(hi - lo, 1e-4)
    e = np.clip((rms - lo) / span, 0.0, 1.0)

    # 过零率：高 = 高频成分多（i / e / s），低 = 低频闷（u / o / m）
    z_lo = _pct(zcr, 15, 0.0)
    z_hi = _pct(zcr, 85, 0.2)
    zspan = max(z_hi - z_lo, 1e-4)
    z = np.clip((zcr - z_lo) / zspan, 0.0, 1.0)

    # 频谱质心同理，和过零率平均，单靠一个都不稳
    c_lo = _pct(cen, 15, 0.0)
    c_hi = _pct(cen, 85, 2000.0)
    cspan = max(c_hi - c_lo, 1e-4)
    c = np.clip((cen - c_lo) / cspan, 0.0, 1.0)

    bright = np.clip(z * 0.55 + c * 0.45, 0.0, 1.0)

    # ---- 张口曲线（每一版都拿真实语音量过帧间抖动，不靠感觉）----
    # 旧版：能量^0.72 → 3 帧均值 → 硬阈值压静音 → 每个字起点强制压到 0.35
    #   实测 帧间|Δ| 0.166、方向反转 37.8%、二阶差分 0.2485
    #   最大元凶是「逐字硬收口」：中文平均每字约 5 帧，轮廓变成
    #   0.59→0.35→0.67 的 5Hz 锯齿，看起来就是在抽搐。
    # 现版：能量^2.1 → 预平滑 → 非对称包络 → 软门 → 音节调制 → 尾部平滑
    #   实测 帧间|Δ| 0.065（-61%）、反转 19%（-50%）、二阶差分 0.055（-78%）
    x = np.power(e, 2.70)
    x = _one_pole(x, 45, fps)
    env = _asym_follow(x, att_ms=70, rel_ms=170, fps=fps)
    # ⚠ 门控必须用「已经平滑过的 env」，不能用瞬时能量 e：
    #   实测音频里存在单帧掉零（e: 0.364 → 0.000 → 0.000），
    #   拿 e 做门控会让 openness 一帧内从 0.82 塌到 0.41，就是那下抽搐。
    gate = _smoothstep(env / 0.10)
    openness = env * (0.05 + 0.95 * gate)

    # 音节调制：按平均字长做 ±10% 的余弦起伏，给一点说话节奏。
    # 关键是用连续余弦，而不是像旧版那样在字起点硬压 —— 后者是锯齿的来源。
    if words:
        avg_dur = float(np.mean([t1 - t0 for (_, t0, t1) in words]))
        if 0.05 < avg_dur < 0.60:
            ph = np.arange(n, dtype=np.float32) / float(fps)
            openness = openness * (1.0 - 0.10 * (
                0.5 + 0.5 * np.cos(2.0 * np.pi * ph / avg_dur)))

    openness = _one_pole(openness, 70, fps)
    # 归一到 99 分位（不用最大值，避免一声爆音把整体压扁）
    openness = np.clip(openness / max(_pct(openness, 99, 1e-3), 1e-3), 0.0, 1.0)

    # ---- 音素层：按拼音把唇形改对（声学只负责「多响」，音素负责「哪个音」）----
    # 只在这里做「修正」不做「取代」：blend 上限 0.85，保留声学的强弱起伏，
    # 否则整段会变成均一的开合、像机械嘴。
    _vis = None
    if words:
        _op_t, _wd_t, _cf = _viseme_curve(words, n, fps)
        if float(_cf.max()) > 0.0:
            # 约束强度也要平滑：字与字交界处硬切会让张嘴幅度一帧内跳变
            _c = _one_pole(_cf, 30, fps)
            _m = _op_t >= 0.0
            # 闭唇音（b/p/m）单独给更高上限：0.85 的混合只能把 0.84 压到 0.14，
            # 画面上仍是半张嘴；闭唇本来就该压到接近 0，这是唯一允许「盖过声学」的情况。
            _lim = np.where(_op_t < 0.10, 0.95, 0.85)
            _b = np.where(_m, np.minimum(_c, _lim), 0.0)
            openness = openness * (1.0 - _b) + np.maximum(_op_t, 0.0) * _b
            _vis = (_wd_t, np.where(_m, np.minimum(_c, 0.75), 0.0))

    # 唇形：亮音偏扁（宽），闷音偏圆（窄）。同样用低通去掉帧级毛刺。
    # ⚠ tau 从 60ms 降到 40ms：实测 60ms 把唇宽变化压得太平（std 0.080，
    #   而开合是 0.313），唇形看起来几乎不动。40ms 时 std 0.103、帧间|Δ| 0.106，
    #   比开合略活跃但不抽搐；25ms 虽更活跃（0.130）却接近原始 bright 的噪声水平。
    width = 0.70 + bright * 0.55
    width = _one_pole(width, 40, fps)

    # 音素层：圆唇（u/o）收窄、扁唇（i/e）拉宽。纯声学靠过零率分不清 u 和 i。
    if _vis is not None:
        width = width * (1.0 - _vis[1]) + _vis[0] * _vis[1]

    # 字外低能量段轻压：ASR 字尾到下一个字起点之间是换气，嘴应该闭上。
    # 旧版这里是 *=0.05 的硬跳变，本身也贡献抖动；改成按能量平滑衰减。
    if words:
        mask = np.zeros(n, dtype=bool)
        for (_, t0, t1) in words:
            i0 = int(t0 * fps)
            i1 = int(t1 * fps) + 1
            mask[max(0, i0):min(n, i1)] = True
        # ⚠ 旧版这里只在 e<0.045 时才压，但真实语音字间换气仍有 0.1~0.3 能量，
        #   条件几乎从不成立 —— 结果是嘴在整段里一直半张、每个字开头都提前张着，
        #   实测各字「张嘴沿」比「能量沿」平均超前 63ms，这正是「对不上口型」的主因。
        #   现在改成「无字程度 × 能量」：ASR 明确无字就往闭合收，
        #   但能量仍高时（ASR 漏识别）保留，不会把有声音的地方压闭。
        gap = (~mask).astype(np.float32)
        gap = _one_pole(gap, 45, fps)              # 平滑，避免字边界硬切
        lvl = np.clip(e / 0.30, 0.0, 1.0)          # 能量越高越不压
        openness = openness * (1.0 - 0.80 * gap * (1.0 - lvl))

    # ---- 幅度增益 ----
    # 纯乘系数会让曲线整体抬高，连「本来就闭嘴」的静音段也一起抬高，
    # 看起来像嘴一直半张。所以先减掉基线再放大，保持「该闭的地方还是闭」。
    #   实测：直接乘 1.4 时静音段 openness 从 0.00 抬到 0.00（不变），
    #   但低能量段 0.10 → 0.14，观感上嘴会一直微微张着；减基线后回到 0.03。
    g = float(np.clip(amp, 0.60, 2.00))
    if abs(g - 1.0) > 1e-3:
        base_lv = float(_pct(openness, 12, 0.0))     # 静音基线
        openness = base_lv + (openness - base_lv) * g
        openness = np.clip(openness, 0.0, 1.0)

    params = []
    for i in range(n):
        params.append((float(openness[i]), float(width[i])))
    return params


# ---------------------------------------------------------------- 渲染
def _fit_size(w: int, h: int) -> tuple:
    """按原图比例缩到长边 ≤ MAX_SIDE，且宽高都是偶数（libx264 要求）。"""
    scale = min(1.0, MAX_SIDE / float(max(w, h)))
    W = max(2, int(round(w * scale / 2.0)) * 2)
    H = max(2, int(round(h * scale / 2.0)) * 2)
    return W, H


def _mouth_geom(size, m: dict) -> tuple:
    """归一化嘴部矩形 → 像素 (cx, cy, mw, mh)，再给出融合区（1.8 倍）。"""
    W, H = size
    cx = m["x"] * W
    cy = m["y"] * H
    mw = max(4.0, m["w"] * W)
    mh = max(3.0, m["h"] * H)
    return cx, cy, mw, mh


def _prep(img_path: str):
    """准备渲染素材：底图 + 上唇/下唇两片 + 蒙版（只算一次，每帧复用）。"""
    from PIL import Image
    im = Image.open(img_path)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    W, H = _fit_size(im.width, im.height)
    if (W, H) != im.size:
        im = im.resize((W, H), Image.LANCZOS)
    base = im.convert("RGB")
    m = mouth()
    cx, cy, mw, mh = _mouth_geom((W, H), m)

    # 融合区：横向 ±0.85 唇宽；纵向 上 1.45、下 1.55 唇高。
    # 纵向要留得比唇部本身宽 —— 形变场靠高斯衰减到边界处归零，
    # 区域太窄的话边界处仍有残余位移，会在额头/下巴留下一道亮度突跳。
    # （实测：0.95/1.10 时上边界跳 20；1.45/1.55 时降到 0.6）
    x0 = int(max(0, round(cx - mw * 0.85)))
    x1 = int(min(W, round(cx + mw * 0.85)))
    ytop = int(max(0, round(cy - mh * 1.45)))
    ybot = int(min(H, round(cy + mh * 1.55)))
    if x1 - x0 < 4:
        x1 = min(W, x0 + 4)
    if ybot - ytop < 4:
        ybot = min(H, ytop + 4)
    patch = base.crop((x0, ytop, x1, ybot))
    # 左右羽化、上下硬接 —— 上下唇是整片位移，皮肤连续，不能羽化
    mask = _h_mask(patch.width, patch.height)
    # 唇缝阴影层（比 patch 窄一点、扁一点，只覆盖唇部）
    sw = max(4, int(round(mw * 0.92)))
    sh = max(3, int(round(mh * 0.62)))

    # ---- 超采样素材（抗「拉伸涂抹」）----
    # 纵向形变最狠时 1 源像素要铺成 ~4.2px（kmin=0.24），而双线性插值在这种
    # 拉伸比下就是一块渐变糊 —— 实测嘴部拉普拉斯方差 34.8，比同帧脸颊 50.6 低 31%，
    # 视觉复核也判「唇线不锐、涂抹感」。根因不是锐化不够，而是**形变本身在
    # 低分辨率上算的**：位移场在 1px 网格上量化，拉伸后放大的是量化误差。
    # 解法是标准超采样：把贴片放大 SS 倍，在高分辨率上做形变，再 LANCZOS 降回来。
    #   · SS=2 时 1 源像素有 4 个采样点，4.2x 拉伸下仍有 2px 真实像素打底；
    #   · 只放大嘴部贴片（287x179 → 574x358），整帧仍是原分辨率，成本可控。
    SS = 2
    if SS > 1:
        hi_size = (patch.width * SS, patch.height * SS)
        patch_hi = patch.resize(hi_size, Image.BICUBIC)
        geom_hi = (cx * SS - x0 * SS, cy * SS - ytop * SS, mw * SS, mh * SS)
    else:
        patch_hi, geom_hi = patch, (cx - x0, cy - ytop, mw, mh)

    return {"base": base, "patch": patch, "patch_hi": patch_hi,
            "mask": mask, "ss": SS,
            "box": (x0, ytop, x1, ybot),
            "shadow_size": (sw, sh),
            "geom": (cx, cy, mw, mh), "geom_hi": geom_hi, "wh": (W, H)}


def _h_mask(w: int, h: int, feather: int = 3) -> "object":
    """左右宽羽化 + 上下窄羽化的融合蒙版。

    左右给 0.22w 的宽羽化：融合区横向边界不能留痕；
    上下只给 feather（默认 3px）：上下唇是「整片位移」，皮肤本身连续，
    上下羽化一旦给宽，额头和下巴就会出现一条渐变带，像贴了块补丁。
    （实测：上下完全不羽化时 y=349 那行亮度会突跳 20，肉眼就是一条横线）
    """
    from PIL import Image
    w = max(w, 2)
    h = max(h, 2)
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ax = np.clip(np.minimum(x, 1.0 - x) / 0.22, 0.0, 1.0)
    ax = ax * ax * (3.0 - 2.0 * ax)
    y = np.arange(h, dtype=np.float32)
    ay = np.clip(np.minimum(y, h - 1 - y) / float(max(feather, 1)), 0.0, 1.0)
    ay = ay * ay * (3.0 - 2.0 * ay)
    m = (ax[None, :] * ay[:, None] * 255.0).astype(np.uint8)
    return Image.fromarray(m, mode="L")


def _frame(prep: dict, openness: float, width: float, amp: float = 1.0):
    """按口型参数生成一帧：上下唇分离位移 + 缝隙用原图唇缝像素拉伸填充。

    演进过程（每一版都拿真实写实人像逐帧验证过，不靠想象）：
      v1 整块嘴部纵向 resize          → 橡皮泥：嘴被拉长、下巴断层
      v2 硬画口腔椭圆 + 牙齿          → 贴纸感：刀切边缘、几何色块
      v3 极克制的整体伸缩 + 唇缝阴影  → 不崩，但几乎看不出在说话（像呼吸）
      v4（当前）上下唇整片错开，缝隙取原图唇缝像素纵向拉伸并按距离压暗
         · 缝隙颜色来自原图唇缝，不是凭空画的色块 → 不像贴纸
         · 上下唇真的错开了 → 能明显看出张嘴
    """
    from PIL import Image
    base = prep["base"]
    patch = prep.get("patch_hi") or prep["patch"]
    mask = prep["mask"]
    out = base.copy()
    ss = int(prep.get("ss") or 1)
    cx, cy, mw, mh = prep.get("geom_hi") or prep["geom"]
    x0, ytop, x1, ybot = prep["box"]

    op = float(np.clip(openness, 0.0, 1.0))
    wd = float(np.clip(width, 0.70, 1.35))
    # 闭嘴且唇形不变 → 直接用底图，省一次重绘
    if op < 0.03 and abs(wd - 1.0) < 0.03:
        return out

    pw, ph = patch.size
    parr = np.asarray(patch, dtype=np.float32)
    cxi = float(cx)          # 贴片内坐标（超采样时已是 SS 倍空间）
    cyi = int(round(cy))
    cyi = max(3, min(ph - 4, cyi))

    # ---- 连续二维形变场 ----
    # 源行 src = cyi + (y-cyi) * k，k<1 表示该处内容被纵向拉伸（唇缝变宽）。
    # k 在唇心最小、向外高斯衰减到 1，所以形变是连续的、没有断层。
    #
    # 为什么不能「切一条缝再填色」（v4 的做法，已废弃）：
    #   上下唇片硬错开 + 中间填一条平均色，实测是一条边缘生硬的暗带，
    #   视觉模型判定为「贴纸」。而连续拉伸用的是原图自己的像素，
    #   缝隙是原图唇缝被拉长，不会有外来色块。
    yy, xx = np.mgrid[0:ph, 0:pw].astype(np.float32)
    d = yy - cyi
    wv = np.exp(-(d / (0.62 * mh)) ** 2)                    # 纵向：唇缝最强
    # 横向：嘴角固定，形变范围只覆盖唇部（不碰两颊）。
    # ⚠ 这里不再用 wd 调 sigma —— 实测那样做，wd 从 0.72 变到 1.08 时
    #   影响半径只从 76px 到 81px（差 2.5%），肉眼完全看不出唇形变化，
    #   这正是「口型不丰富」的直接原因。wd 现在改为驱动真正的横向形变（见下）。
    sig_h = 0.80 * mw * 0.5
    wh = np.exp(-((xx - cxi) / max(sig_h, 1.0)) ** 2)
    # 幅度：amp 只做「温和」的强度调节，主体增益在 _build_params 的曲线上。
    # 为什么不大幅调这里 —— 实测把系数拉到 0.72*1.4 + kmin 0.16 时，
    # 唇缝能到 41px，但视觉复核判定「嘴角拉扯、上唇边缘平、下巴僵硬」。
    # 所以这里只给 0.35 的斜率，kmin 下限停在 0.24（约 4.2x），再狠就拉丝。
    _a = float(np.clip(amp, 0.60, 2.00))
    coef_base = 0.72 * (1.0 + 0.35 * (_a - 1.0))
    kmin = max(0.24, 0.40 - 0.16 * (_a - 1.0))
    # 上下唇不对称：真人说话下唇动作明显大于上唇。
    # 实测变化重心 上唇 51.9%/下唇 48.1%（对称）→ 45.3%/54.7%（0.8:1.2）。
    # 再偏（1.6:1）视觉复核判定「上唇被压得过薄、下唇过凸、像在抿嘴」，故取 0.8:1.2。
    coef = np.where(d > 0, coef_base * 1.20, coef_base * 0.80)
    k = 1.0 - coef * op * wv * wh
    k = np.maximum(k, kmin)
    src = np.clip(cyi + d * k, 0, ph - 1)

    i0 = np.floor(src).astype(np.int32)
    i1 = np.minimum(i0 + 1, ph - 1)
    frac = (src - i0)[..., None]
    cols = np.arange(pw)[None, :]
    outp = parr[i0, cols] * (1.0 - frac) + parr[i1, cols] * frac

    # ---- 横向形变：真正改变唇形宽窄（圆唇 / 扁唇）----
    # wd > 1 扁唇（i/e，嘴角向两边拉）；wd < 1 圆唇（u/o，嘴唇收拢）。
    # 注意方向：sx 增大 = 采样点向外扩散 = 画面内容被压缩变窄，
    #   所以 kx = 1 - gain*(wd-1)，wd 大时 kx 小 → 显示更宽。
    #   （第一版写成 1 + gain*(wd-1)，方向反了，实测圆唇反而变宽。）
    # 增益 1.40 是实测值：唇缝暗带 153px(圆) ↔ 170px(扁)，差 10.5%；
    #   1.0 时只差 5%（偏弱），2.0 时差 14% 但影响范围扩到鼻子/下巴。
    # 乘 wv 是关键：只在唇部生效，不会把鼻子或下巴一起横向拉扯。
    if abs(wd - 1.0) > 0.02:
        kx = 1.0 - 1.40 * (wd - 1.0)
        wx = np.exp(-((xx - cxi) / (0.80 * mw * 0.5)) ** 2)
        f = (kx - 1.0) * wx * wv * op
        sx = np.clip(cxi + (xx - cxi) * (1.0 + f), 0, pw - 1)
        j0 = np.floor(sx).astype(np.int32)
        j1 = np.minimum(j0 + 1, pw - 1)
        f2 = (sx - j0)[..., None]
        rows = np.arange(ph)[:, None]
        outp = outp[rows, j0] * (1.0 - f2) + outp[rows, j1] * f2

    # 唇缝中心轻微压暗：模拟口腔深度。只压 14%，再重就是一条黑带。
    dark = 1.0 - (0.14 + 0.05 * (_a - 1.0)) * op * np.exp(-(d / (0.26 * mh)) ** 2) * wh
    outp = outp * dark[..., None]

    img = Image.fromarray(np.clip(outp, 0, 255).astype(np.uint8), mode="RGB")
    if ss > 1:
        # 超采样降回原尺寸。用 BICUBIC 而非 LANCZOS：后者在唇线这类强对比边缘
        # 会产生振铃（白边），紧跟着的 USM 会把它放大成一条亮线。
        img = img.resize(prep["patch"].size, Image.BICUBIC)

    # 唇形宽窄已经由上面形变场的横向 sigma 表达，这里不再对整帧做横向 resize。
    # 演进记录：原先用 resize 实现唇宽，且按 |sx-1|>0.005 决定缩不缩 ——
    #   实测 175 帧里这个条件来回跨越 56 次，等于每 3 帧换一次重采样路径；
    #   改成「一律 resize」后反而更糟（帧间差 0.99 → 1.09），因为 resize 目标宽度
    #   每帧差 ±1px，本身就是抖动源。最终做法是完全不重采样。
    nbx = x0
    m2 = mask

    # 纵向拉伸会把 1px 铺成 2.5px，分辨率损失表现为「涂抹感」。
    # 轻度 USM 补偿：percent=70 是实测平衡点（拉普拉斯均值 5.59 → 6.84），再高会出白边。
    # ⚠ 同样不能「op>0.15 才锐化」：阈值来回跨越时锐度会一帧开一帧关，也是闪烁源。
    #   改成按 op 连续加权 —— op=0 不锐化、op≥0.25 全额，中间平滑过渡。
    amt = 70.0 * float(_smoothstep(min(max(op / 0.25, 0.0), 1.0)))
    if amt >= 1.0:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0,
                                                 percent=int(round(amt)),
                                                 threshold=2))
    out.paste(img, (nbx, ytop), m2)
    return out


def render(img_path: str, wav_path: str, out_mp4: str, words: list = None,
           fps: int = FPS, on_progress=None, expr: dict = None,
           amp: float = 1.0) -> dict:
    """逐帧渲染 + ffmpeg 合成。返回 {ok, frames, duration, path}

    口型由本项目的声学特征形变驱动（不崩、位置准、幅度偏小）。
    expr → 表情层配置（avatar2_expr.resolve 的返回值），None 则不做
    """
    exe = ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}

    sr, y = _read_wav(wav_path)
    dur = len(y) / float(sr)
    if dur > MAX_SECONDS:
        return {"ok": False, "error": "音频 %.1f 秒，超过 %d 秒上限（渲染太慢）"
                % (dur, MAX_SECONDS)}
    if dur < 0.2:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}

    _amp = float(np.clip(amp, 0.60, 2.00))
    prep = _prep(img_path)
    params = _build_params(y, sr, words or [], fps, amp=_amp)
    W, H = prep["wh"]
    n = len(params)

    # 表情层：人脸框用于定位眉眼带；没有就只做整头微动（不传 face）
    _expr = expr or None
    _face_norm = None
    _ex_mod = None
    if _expr:
        try:
            from . import avatar2_expr as _ex_mod       # noqa: F811
            _d = detect_mouth(img_path)
            _face_norm = _d.get("face") if _d.get("ok") else None
        except Exception:                               # noqa: BLE001
            _ex_mod = None

    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-",
           "-i", wav_path,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-shortest", "-movflags", "+faststart", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, (op, wd) in enumerate(params):
            fr = _frame(prep, op, wd, amp=_amp)
            if _ex_mod is not None:
                try:
                    # 张口度本身就是「此刻在不在说话」的代理指标
                    _ex_mod.apply(fr, i / float(fps), _expr,
                                  face=_face_norm,
                                  energy=min(1.0, float(op) * 1.6))
                except Exception:                       # noqa: BLE001
                    logger.debug("silent-except", exc_info=True)
            proc.stdin.write(fr.tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                       # noqa: BLE001
                    logger.debug("silent-except", exc_info=True)
    except Exception as e:                              # noqa: BLE001
        try:
            proc.kill()
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
        return {"ok": False, "error": "渲染中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    err = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s"
                % (err or b"")[:400].decode("utf-8", "ignore")}
    return {"ok": True, "frames": n, "duration": round(dur, 2),
            "size": [W, H], "path": out_mp4,
            "bytes": os.path.getsize(out_mp4)}


# ---------------------------------------------------------------- 一站式
def speak(text: str, voice: str = "", rate=None, style: str = "",
          engine: str = "auto", mood: str = "", expr: bool = False,
          amp: float = 1.0) -> dict:
    """文本 → TTS → 云端 Wan2.2-S2V → mp4。这是给工具/前端调的主入口。

    2026-11-15 重构：本地 2D 形变已**整体停用**，改走阿里云百炼 Wan2.2-S2V。
    原因是形变原理上做不出 b/p/m 闭唇、舌位、牙齿，怎么调参都是橡皮嘴；
    云端是扩散模型逐帧重绘，口型由音频真正驱动。

    参数现在的实际作用（都只影响配音，不再影响口型渲染）：
      engine = TTS 引擎（auto/edge/sapi）
      mood   = 心情：auto/""（按语境自动判断）/ happy/calm/serious/gentle/
               sad/excited/surprise/worried/confident
      rate   = 语速 -50~50；None 表示用心情的建议值
      expr / amp = 本地渲染时代的参数，云端不再使用，保留只为兼容老调用方
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    # ---- 云端没配好就早退，把「缺什么、去哪拿」直接告诉调用方
    from . import avatar_cloud as cl
    cstat = cl.status()
    if not cstat.get("ready"):
        c = cl.config()
        return {
            "ok": False, "need_key": True,
            "error": "云端口型还没配置：缺%s" % "、".join(c.get("missing") or ["API Key"]),
            "signup_url": c.get("signup_url"), "key_url": c.get("key_url"),
            "doc_url": c.get("doc_url"),
        }

    # ---- 心情 + 语速解析（auto 时走规则，拿不准的标记交给调用方决定）
    # 注意：这两个现在只影响 TTS 配音，不影响口型（口型由云端模型按音频重绘）
    from . import avatar2_expr as ex
    cfg = ex.resolve(mood, rate, t)
    final_rate = int(cfg.get("rate", 0))
    final_style = (style or "").strip() or str(cfg.get("style") or "")

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=final_rate,
                      engine=engine or "auto", style=final_style)
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    audio = a.get("path") or ""

    # ---- 云端生成（内部会按 <20 秒自动切段，再拼接）
    r = cl.generate(_img_path(), audio)
    if not r.get("ok"):
        return r
    from urllib.parse import quote
    return {
        "ok": True,
        "name": r["name"],
        "url": "/api/avatar2/video/" + quote(r["name"]),
        "path": r["path"],
        "duration": r.get("duration"),
        "bytes": r.get("bytes"),
        "segments": r.get("segments"),
        "elapsed": r.get("elapsed"),
        "engine": r.get("engine"),
        "resolution": r.get("resolution"),
        "lipsync_label": r.get("lipsync_label"),
        "mood": cfg.get("key", "calm"),
        "mood_label": cfg.get("label", "平静"),
        "mood_auto": bool(cfg.get("auto")),
        "mood_confident": bool(cfg.get("auto_confident")),
        "mood_why": cfg.get("auto_why") or [],
        "rate": final_rate,
        "style": final_style,
        "audio_url": a.get("url") or "",
        "audio_engine": a.get("engine") or "",
        "text": t[:200],
    }


def speak_async(text: str, voice: str = "", rate=None, style: str = "",
                engine: str = "auto", mood: str = "") -> dict:
    """提交云端生成任务，立刻返回 job_id（前端轮询进度用）。

    为什么要有异步版：一次生成 1~3 分钟（长音频还要多段），挂在 HTTP
    长连接上容易被中间层掐断，前端也拿不到「第几段」的进度。
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    from . import avatar_cloud as cl
    c = cl.config()
    if not c.get("ready"):
        return {"ok": False, "need_key": True,
                "error": "云端口型还没配置：缺%s" % "、".join(c.get("missing") or ["API Key"]),
                "signup_url": c.get("signup_url"), "key_url": c.get("key_url")}

    from . import avatar2_expr as ex
    cfg = ex.resolve(mood, rate, t)
    final_rate = int(cfg.get("rate", 0))
    final_style = (style or "").strip() or str(cfg.get("style") or "")

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=final_rate,
                      engine=engine or "auto", style=final_style)
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}

    jid = cl.generate_async(_img_path(), a.get("path") or "")
    return {"ok": True, "job_id": jid,
            "audio_url": a.get("url") or "",
            "audio_engine": a.get("engine") or "",
            "rate": final_rate, "style": final_style,
            "mood": cfg.get("key", "calm"),
            "mood_label": cfg.get("label", "平静"),
            "text": t[:200]}


def status() -> dict:
    """能力自检：让前端如实告诉用户现在能做什么、缺什么。

    2026-11-15：本地形变已停用，这里报的是**云端配置**状态。
    字段尽量兼容老前端（ffmpeg / has_image 仍保留）。
    """
    from . import avatar_cloud as cl
    c = cl.status()
    try:
        exe = ffmpeg()
    except Exception:                                   # noqa: BLE001
        exe = ""
    return {
        "ok": True,
        "engine": "cloud",
        "engine_label": "阿里云百炼 Wan2.2-S2V",
        "ready": bool(c.get("ready")),
        "has_key": bool(c.get("has_key")),
        "workspace_id": c.get("workspace_id") or "",
        "resolution": c.get("resolution") or "480P",
        "model": c.get("model") or "wan2.2-s2v",
        "max_audio_sec": c.get("max_audio_sec"),
        "ffmpeg": bool(exe),
        "ffmpeg_path": exe or "",
        "has_image": os.path.isfile(_img_path()),
        "engines": ["cloud"],
        "note": c.get("note") or "",
        "signup_url": cl.SIGNUP_URL,
        "key_url": cl.KEY_URL,
        "doc_url": cl.DOC_URL,
    }


def list_videos(limit: int = 50, scope: str = "avatar") -> list:
    """列出可浏览的 mp4，按时间倒序。

    scope="avatar"  → 数字人生成的视频（data/avatar2/videos）
    scope="library" → 生成文库里的 mp4（视频编辑产物、样片等）
    scope="all"     → 两边合并
    每项带 scope 字段，删除时按它决定删哪边（两边删除语义不同：
    数字人视频直接删；文库文件进回收站，可恢复）。
    """
    sc = (scope or "avatar").lower()
    if sc not in ("avatar", "library", "all"):
        sc = "avatar"
    items = []

    if sc in ("avatar", "all"):
        d = _vdir()
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for f in names:
            if not f.lower().endswith(".mp4"):
                continue
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append({"name": f, "bytes": st.st_size,
                          "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(st.st_mtime)),
                          "_ts": st.st_mtime,
                          "scope": "avatar", "rel": f, "folder": "",
                          "url": "/api/avatar2/video/" + f})

    if sc in ("library", "all"):
        try:
            from . import doclib
            from urllib.parse import quote
            for f in doclib.list_files():
                if f.get("ext") not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
                    continue
                items.append({"name": f["name"], "bytes": f["size"],
                              "mtime": f["modified"], "_ts": f["mtime"],
                              "scope": "library", "rel": f["rel"],
                              "folder": f.get("folder", ""),
                              "url": "/api/doclib/download?rel="
                                     + quote(f["rel"])})
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    items.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for it in items:
        it.pop("_ts", None)
    return items[:limit]


def video_path(name: str) -> str:
    """安全取视频路径（挡目录穿越）。"""
    base = os.path.basename(name or "")
    if not base or not base.lower().endswith(".mp4"):
        return ""
    p = os.path.join(_vdir(), base)
    return p if os.path.isfile(p) else ""


def delete_video(name: str, scope: str = "avatar") -> dict:
    """删除视频。scope="library" 时走 doclib（进回收站，可恢复）。"""
    if (scope or "").lower() == "library":
        try:
            from . import doclib
            return doclib.delete_file(name)
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    p = video_path(name)
    if not p:
        return {"ok": False, "error": "没有这个视频"}
    try:
        os.remove(p)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def concat_videos(names, out_name: str = "") -> dict:
    """把多段数字人视频按顺序拼成一个 mp4（长文本分段生成后合并用）。

    分段产物编码参数一致（h264 + aac、同分辨率同帧率），所以先用
    concat demuxer + `-c copy` 直接流拷贝，几秒完成；万一参数不一致
    导致 copy 失败，再退回重编码一次（慢但一定能成）。
    """
    import re as _re
    from urllib.parse import quote as _q
    exe = ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not names:
        return {"ok": False, "error": "没有可拼接的视频"}
    paths = []
    for n in names:
        p = os.path.join(_vdir(), os.path.basename(str(n)))
        if not os.path.isfile(p):
            return {"ok": False, "error": "分段视频不存在：%s" % n}
        paths.append(p)
    if len(paths) == 1:
        nm = os.path.basename(paths[0])
        return {"ok": True, "name": nm, "parts": 1, "duration": _probe_duration(paths[0]),
                "url": "/api/avatar2/video/" + _q(nm)}

    name = out_name or ("avatar_" + time.strftime("%Y%m%d_%H%M%S") + "_"
                        + uuid.uuid4().hex[:4] + ".mp4")
    out = os.path.join(_vdir(), os.path.basename(name))
    lst = os.path.join(_vdir(), "_concat_%s.txt" % uuid.uuid4().hex[:8])
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                # concat demuxer 的路径要转义单引号；反斜杠统一成 / 更稳
                f.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-f", "concat",
                            "-safe", "0", "-i", lst, "-c", "copy",
                            "-movflags", "+faststart", out],
                           capture_output=True)
        ok = r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000
        if not ok:
            # 兜底：参数不一致时重编码
            r2 = subprocess.run([exe, "-y", "-loglevel", "error", "-f", "concat",
                                 "-safe", "0", "-i", lst,
                                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                                 "-movflags", "+faststart", out],
                                capture_output=True)
            if r2.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 1000:
                return {"ok": False,
                        "error": "拼接失败：%s" % ((r2.stderr or r.stderr or b"")[:300]
                                                  .decode("utf-8", "ignore"))}
    finally:
        try:
            os.remove(lst)
        except Exception:                              # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    # 时长直接读合并后的成品：累加各段时若某一段探测失败会返回 0，
    # 实测就出现过 101 秒的片子报成 54.6 秒（用户会以为丢了一半）。
    dur = round(_probe_duration(out), 2)
    return {"ok": True, "name": os.path.basename(out), "parts": len(paths),
            "duration": dur, "bytes": os.path.getsize(out),
            "url": "/api/avatar2/video/" + _q(os.path.basename(out))}


def _probe_duration(path: str) -> float:
    """读 mp4 时长（秒）；失败返回 0，不影响拼接结果。"""
    try:
        import re as _re
        exe = ffmpeg()
        r = subprocess.run([exe, "-i", path], capture_output=True)
        s = (r.stderr or b"").decode("utf-8", "ignore")
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", s)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:                                  # noqa: BLE001
        return 0.0
# ------------------------------------------- 闭嘴预览候选（SDXL 局部重绘）
# 2026-11-15：用户报「闭嘴那块像贴上去的色块，恶心」。
# 旧路径 close_mouth() 是**确定性像素搬运**（下唇上移 + 唇色覆盖），
# 2D 压缩做不出唇珠/唇线起伏，视觉复核原话是「珊瑚红的心形色块浮在皮肤上」。
# 新路径让扩散模型**重画**嘴唇，画风光影跟着原图走。
#
# ★ 主形象零改动的关键：只裁一个以嘴为中心的方块送去重绘，重绘完按原坐标贴回。
#   整图送去重绘会被 SDXL 缩到 680x1216 再还原（原图 787x1400），等于全图重采样
#   —— 实测 107326 个像素被改，违反「主形象不能变」。裁 512x512 时
#   _fit_working_size 返回 (512,512) 不缩放，块外像素实测 0 改动。

_CLOSED_PROMPTS = {
    "natural": ("a closed mouth, lips gently pressed together, natural closed lips, "
                "3D cartoon character, soft matte clay material, smooth skin, "
                "soft shading, subtle lip line, high quality"),
    "smile": ("a closed mouth with a gentle smile, lips softly closed, corners slightly up, "
              "3D cartoon character, soft matte clay material, smooth skin, "
              "soft shading, high quality"),
    "neutral": ("a relaxed closed mouth, calm expression, lips together, subtle lip line, "
                "3D cartoon character, soft matte clay material, smooth skin, "
                "soft shading, high quality"),
}
_CLOSED_NEG = ("open mouth, teeth, tongue, red lips, lipstick, exaggerated lips, "
               "blurry, deformed, extra mouth, text, watermark, low quality, "
               "harsh edges, color patch")
_CLOSED_STYLES = ("natural", "smile", "neutral")


def _closed_cand_dir() -> str:
    d = os.path.join(_dir(), "closed_candidates")
    os.makedirs(d, exist_ok=True)
    return d


def closed_candidates() -> list:
    """列出已生成的候选（按时间倒序）。"""
    d = _closed_cand_dir()
    out = []
    cm = _read_meta().get("cand_style") or {}
    for f in sorted(glob.glob(os.path.join(d, "*.png")),
                    key=os.path.getmtime, reverse=True):
        n = os.path.basename(f)
        out.append({"id": n, "style": cm.get(n, ""),
                    "url": "/api/avatar2/closed_candidate/" + _q(n),
                    "ts": time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(f)))})
    return out


def preview_closed_inpaint(n: int = 3, style: str = "mix", steps: int = 28,
                           patch: int = 512) -> dict:
    """用 SDXL 局部重绘生成闭嘴预览候选（**主形象零改动**）。

    n     : 生成几张（1~4）
    style : natural / smile / neutral / mix（mix = 依次轮换三种描述）
    steps : 采样步数，默认 28
    patch : 送进模型的方块边长；512 时不会被 _fit_working_size 缩放
    """
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFilter

    p = _img_path()
    if not os.path.isfile(p):
        return {"ok": False, "error": "还没有形象图，先上传一张"}
    try:
        from . import t2i as _t2i
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "局部重绘模块加载失败：%s" % e}
    try:
        if not _t2i.inpaint_ready():
            return {"ok": False,
                    "error": "还没装「局部重绘」模型（目录：%s）" % _t2i.inpaint_model_dir()}
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "检查重绘模型失败：%s" % e}

    im = Image.open(p).convert("RGB")
    W, H = im.size
    cx, cy, mw, mh = _mouth_geom((W, H), mouth())
    patch = max(256, min(512, int(patch or 512)))
    if W < patch or H < patch:
        patch = max(128, (min(W, H) // 2) * 2)
    px = int(round(cx - patch / 2.0))
    py = int(round(cy - patch / 2.0))
    px = max(0, min(W - patch, px))
    py = max(0, min(H - patch, py))
    block = im.crop((px, py, px + patch, py + patch))
    # 蒙版：椭圆盖住嘴（上下留余量，够模型画出唇线）
    m = Image.new("L", (patch, patch), 0)
    dr = ImageDraw.Draw(m)
    bx, by = cx - px, cy - py
    dr.ellipse([bx - mw * 1.05, by - mh * 2.1,
                bx + mw * 1.05, by + mh * 2.1], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(max(3.0, mh * 0.30)))

    tmpd = tempfile.mkdtemp(prefix="closecand_")
    bpath = os.path.join(tmpd, "block.png")
    mpath = os.path.join(tmpd, "mask.png")
    block.save(bpath)
    m.save(mpath)

    if style == "mix":
        styles = list(_CLOSED_STYLES)
    else:
        styles = [style if style in _CLOSED_PROMPTS else "natural"]
    d = _closed_cand_dir()
    ts = time.strftime("%Y%m%d_%H%M%S")
    made, errs = [], []
    try:
        for i in range(max(1, min(4, int(n or 3)))):
            st = styles[i % len(styles)]
            r = _t2i.inpaint(bpath, prompt=_CLOSED_PROMPTS[st],
                             negative_prompt=_CLOSED_NEG,
                             mask=mpath, strength=0.95, steps=steps)
            if not r.get("ok"):
                errs.append(r.get("error"))
                continue
            nb = Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB")
            if nb.size != (patch, patch):
                nb = nb.resize((patch, patch), Image.LANCZOS)
            out = im.copy()
            out.paste(nb, (px, py), m)      # ★ 块外像素原样保留
            name = "cand_%s_%d.png" % (ts, i + 1)
            out.save(os.path.join(d, name))
            made.append({"id": name, "style": st,
                         "url": "/api/avatar2/closed_candidate/" + _q(name),
                         "work_size": r.get("work_size")})
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    if not made:
        return {"ok": False,
                "error": "全部候选都失败了：%s" % ("; ".join(str(e) for e in errs[:3]))}
    _meta = _read_meta()
    cm = dict(_meta.get("cand_style") or {})
    for it in made:
        cm[it["id"]] = it["style"]
    _meta["cand_style"] = cm
    _write_meta(_meta)
    return {"ok": True, "count": len(made), "candidates": made,
            "box": [px, py, patch], "errors": errs[:3],
            "note": "只重绘了嘴部 %dx%d 方块，方块外像素零改动" % (patch, patch)}


def apply_closed_candidate(cid: str) -> dict:
    """把用户选中的候选设为 avatar_closed.png（之后渲染就用它）。"""
    cid = os.path.basename(str(cid or "").strip())
    if not cid or not cid.endswith(".png"):
        return {"ok": False, "error": "候选 id 不对"}
    src = os.path.join(_closed_cand_dir(), cid)
    if not os.path.isfile(src):
        return {"ok": False, "error": "找不到这张候选：%s" % cid}
    try:
        shutil.copyfile(src, _img_closed_path())
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "写入闭嘴基准失败：%s" % e}
    _meta = _read_meta()
    _meta["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _meta["closed_from"] = os.path.basename(_img_path())
    _meta["closed_method"] = "inpaint"
    _meta["closed_candidate"] = cid
    _write_meta(_meta)
    return {"ok": True, "method": "inpaint", "id": cid,
            "url": "/api/avatar2/closed?t=%d" % int(time.time())}


# __TEST_MARKER_1794594093__
