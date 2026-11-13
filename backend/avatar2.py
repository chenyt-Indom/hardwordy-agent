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

import numpy as np

# 嘴部默认位置：脸的中下部。默认值不会对所有图都准，前端可以拖动校准。
DEFAULT_MOUTH = {"x": 0.50, "y": 0.735, "w": 0.155, "h": 0.062}

_IMG = "avatar.png"
_MOUTH = "mouth.json"
_META = "meta.json"

FPS = 25
MAX_SECONDS = 90          # 单次最长 90 秒，再长渲染太慢
MAX_SIDE = 720            # 输出长边上限，够看又不至于渲染几分钟


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


def ffmpeg() -> str:
    """拿 ffmpeg 可执行文件。优先 imageio_ffmpeg 自带的（项目其它模块也这么取）。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:                                   # noqa: BLE001
        pass
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
                pass
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
                pass
    _write_json(_MOUTH, cur)
    return mouth()


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
    # 存完立刻自动定位嘴：上传照片和 AI 头像的构图差别极大，靠默认值一定偏，
    # 检测到就覆盖 mouth.json；检测不到保留默认值，前端可手动拖动校准。
    auto = detect_mouth(_img_path())
    meta = {"source": source or "",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    if auto.get("ok"):
        set_mouth(auto["mouth"])
        meta["auto_mouth"] = auto["mouth"]
        meta["face_score"] = auto.get("score")
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
    p = _img_path()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return info()


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
    ]
    cands: list[str] = []
    for pat in pats:
        try:
            cands.extend(glob.glob(pat, recursive=True))
        except Exception:                               # noqa: BLE001
            pass
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

    # 唇形：亮音偏扁（宽），闷音偏圆（窄）。同样用低通去掉帧级毛刺。
    width = 0.70 + bright * 0.55
    width = _one_pole(width, 60, fps)

    # 字外低能量段轻压：ASR 字尾到下一个字起点之间是换气，嘴应该闭上。
    # 旧版这里是 *=0.05 的硬跳变，本身也贡献抖动；改成按能量平滑衰减。
    if words:
        mask = np.zeros(n, dtype=bool)
        for (_, t0, t1) in words:
            i0 = int(t0 * fps)
            i1 = int(t1 * fps) + 1
            mask[max(0, i0):min(n, i1)] = True
        outside = _smoothstep((0.045 - e) / 0.045) * (~mask)
        openness = openness * (1.0 - 0.85 * outside)

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
    return {"base": base, "patch": patch, "mask": mask,
            "box": (x0, ytop, x1, ybot),
            "shadow_size": (sw, sh),
            "geom": (cx, cy, mw, mh), "wh": (W, H)}


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
    patch = prep["patch"]
    mask = prep["mask"]
    out = base.copy()
    cx, cy, mw, mh = prep["geom"]
    x0, ytop, x1, ybot = prep["box"]

    op = float(np.clip(openness, 0.0, 1.0))
    wd = float(np.clip(width, 0.70, 1.35))
    # 闭嘴且唇形不变 → 直接用底图，省一次重绘
    if op < 0.03 and abs(wd - 1.0) < 0.03:
        return out

    pw, ph = patch.size
    parr = np.asarray(patch, dtype=np.float32)
    cyi = int(round(cy - ytop))
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
    # 横向：嘴角固定。唇形宽窄（wd）用横向 sigma 表达 ——
    # 原先是用整帧横向 resize 实现的，但 resize 目标宽度每帧都差 ±1px，
    # 等于每帧都重新采样一次，本身就是抖动源。改成调制 sigma 就完全不用重采样。
    sig_h = 0.80 * mw * 0.5 * (1.0 + 0.18 * (wd - 1.0))
    wh = np.exp(-((xx - (cx - x0)) / max(sig_h, 1.0)) ** 2)
    # 幅度：amp 只做「温和」的强度调节，主体增益在 _build_params 的曲线上。
    # 为什么不大幅调这里 —— 实测把系数拉到 0.72*1.4 + kmin 0.16 时，
    # 唇缝能到 41px，但视觉复核判定「嘴角拉扯、上唇边缘平、下巴僵硬」。
    # 所以这里只给 0.35 的斜率，kmin 下限停在 0.24（约 4.2x），再狠就拉丝。
    _a = float(np.clip(amp, 0.60, 2.00))
    coef = 0.72 * (1.0 + 0.35 * (_a - 1.0))
    kmin = max(0.24, 0.40 - 0.16 * (_a - 1.0))
    k = 1.0 - coef * op * wv * wh
    k = np.maximum(k, kmin)
    src = np.clip(cyi + d * k, 0, ph - 1)

    i0 = np.floor(src).astype(np.int32)
    i1 = np.minimum(i0 + 1, ph - 1)
    frac = (src - i0)[..., None]
    cols = np.arange(pw)[None, :]
    outp = parr[i0, cols] * (1.0 - frac) + parr[i1, cols] * frac

    # 唇缝中心轻微压暗：模拟口腔深度。只压 14%，再重就是一条黑带。
    dark = 1.0 - (0.14 + 0.05 * (_a - 1.0)) * op * np.exp(-(d / (0.26 * mh)) ** 2) * wh
    outp = outp * dark[..., None]

    img = Image.fromarray(np.clip(outp, 0, 255).astype(np.uint8), mode="RGB")

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
           fps: int = FPS, on_progress=None, expr: dict = None) -> dict:
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

    prep = _prep(img_path)
    params = _build_params(y, sr, words or [], fps)
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
            fr = _frame(prep, op, wd)
            if _ex_mod is not None:
                try:
                    # 张口度本身就是「此刻在不在说话」的代理指标
                    _ex_mod.apply(fr, i / float(fps), _expr,
                                  face=_face_norm,
                                  energy=min(1.0, float(op) * 1.6))
                except Exception:                       # noqa: BLE001
                    pass
            proc.stdin.write(fr.tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                       # noqa: BLE001
                    pass
    except Exception as e:                              # noqa: BLE001
        try:
            proc.kill()
        except Exception:                               # noqa: BLE001
            pass
        return {"ok": False, "error": "渲染中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
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
          engine: str = "auto", mood: str = "", expr: bool = True) -> dict:
    """文本 → TTS → 口型对齐 → mp4。这是给工具/前端调的主入口。

    engine  = TTS 引擎（auto/edge/sapi）
    mood    = 心情：auto/""（按语境自动判断）/ happy/calm/serious/gentle/
              sad/excited/surprise/worried/confident
    rate    = 语速 -50~50；None 表示用心情的建议值
    expr    = 是否叠加微表情（整头轻动 + 眉眼带轻形变）
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    # ---- 心情 + 语速解析（auto 时走规则，拿不准的标记交给调用方决定）
    from . import avatar2_expr as ex
    cfg = ex.resolve(mood, rate, t)
    final_rate = int(cfg.get("rate", 0))
    final_style = (style or "").strip() or str(cfg.get("style") or "")
    expr_cfg = cfg if expr else None

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=final_rate,
                      engine=engine or "auto", style=final_style)
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    audio = a.get("path") or ""
    try:
        wav = _to_wav16k(audio)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "音频转码失败：%s: %s" % (type(e).__name__, e)}

    words = _asr_words(wav)
    name = "avatar_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4] + ".mp4"
    out = os.path.join(_vdir(), name)
    r = render(_img_path(), wav, out, words=words, expr=expr_cfg)
    if not r.get("ok"):
        return r
    from urllib.parse import quote
    return {
        "ok": True,
        "name": name,
        "url": "/api/avatar2/video/" + quote(name),
        "path": out,
        "duration": r["duration"],
        "size": r["size"],
        "frames": r["frames"],
        "bytes": r["bytes"],
        "mood": cfg.get("key", "calm"),
        "mood_label": cfg.get("label", "平静"),
        "mood_auto": bool(cfg.get("auto")),
        "mood_confident": bool(cfg.get("auto_confident")),
        "mood_why": cfg.get("auto_why") or [],
        "rate": final_rate,
        "style": final_style,
        "expr": bool(expr),
        "lipsync_label": "声学形变口型",
        "audio_url": a.get("url") or "",
        "audio_engine": a.get("engine") or "",
        "align": ("字级对齐 %d 字" % len(words)) if words else "声学驱动（未找到 ASR 模型）",
        "text": t[:200],
    }


def status() -> dict:
    """能力自检：让前端如实告诉用户现在能做什么、缺什么。"""
    exe = ffmpeg()
    asr = _find_asr()
    torch_ok = False
    torch_ver = ""
    try:
        import torch  # noqa: F401
        torch_ok = True
        torch_ver = getattr(torch, "__version__", "")
    except Exception:                                   # noqa: BLE001
        torch_ok = False
    return {
        "ok": True,
        "ffmpeg": bool(exe),
        "ffmpeg_path": exe,
        "asr": bool(asr),
        "asr_dir": asr,
        "torch": torch_ok,
        "torch_version": torch_ver,
        "fps": FPS,
        "max_seconds": MAX_SECONDS,
        "max_side": MAX_SIDE,
        "has_image": os.path.isfile(_img_path()),
        "engines": ["param"],
        "note": "参数化口型（声学特征驱动）"
                + ("，已启用字级对齐" if asr else "，未找到 ASR 模型，仅声学驱动"),
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
            pass

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
            pass
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
# __TEST_MARKER_1794594093__
