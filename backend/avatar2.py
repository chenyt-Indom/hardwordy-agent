# -*- coding: utf-8 -*-
"""数字人 2.0：一张图 + 一段话 → 会说话的视频（2026-11-13）

为什么不是 Wav2Lip
------------------
本机 Python 3.14 下 torch 导入就报 `shm.dll` 错误，Wav2Lip / SadTalker 这类
**神经网络重绘口型**根本跑不起来（要另开 3.11 环境 + 下几百 MB 权重）。
所以走**声学特征驱动的参数化口型**：

  · 每 40ms 一帧，算 RMS（音量）/ 过零率（声音亮暗）/ 频谱质心；
  · RMS → 张口高度；过零率 + 质心 → 唇形宽窄（扁唇 i/e ↔ 圆唇 u/o）；
  · 若本机找得到 sherpa-onnx 中文 ASR 模型，再用**字级时间戳**约束
    字与字之间的换气段闭嘴，口型不会在停顿处乱动；
  · PIL 逐帧对嘴部区域做弹性形变，ffmpeg 合成 mp4（原音轨保留）。

代价（必须说清，不吹）：这是**形变**不是**重绘**。嘴的形状由声学特征推断，
不做音素级重绘，所以不会精确做出 "b/p/m" 的闭唇口型。二次元立绘上观感自然，
写实人脸会偏"橡皮嘴"。要真口型得装 torch + Wav2Lip，本机做不到。

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
        out = {
            "x": mcx / w,
            "y": min(1.0, (mcy + fh * 0.006) / h),
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


def _smooth(a: np.ndarray, k: int = 3) -> np.ndarray:
    if a.size < 3 or k <= 1:
        return a
    ker = np.ones(k, dtype=np.float32) / float(k)
    return np.convolve(a, ker, mode="same")


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
def _build_params(y: np.ndarray, sr: int, words: list, fps: int = FPS) -> list:
    """把声学特征 + 字边界 → 每帧的 (openness, width) 0~1 附近的口型参数。

    openness：张口高度倍率（0=闭唇，1=大张）
    width   ：唇形宽窄（<1 圆唇，>1 扁唇）
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

    # 张口：能量为主，做 gamma 让小声也能看出在动
    openness = np.power(e, 0.72)
    openness = _smooth(openness, 3)
    # 唇形：亮音偏扁（宽），闷音偏圆（窄）
    width = 0.70 + bright * 0.55
    width = _smooth(width, 3)

    # 静音段（能量极低）强制闭嘴，否则底噪会让嘴一直微微张着
    quiet = e < 0.045
    openness[quiet] *= 0.10

    # 字边界约束：字与字之间的换气段闭嘴。ASR 给的是字的起点，
    # 字尾到下一个字起点之间如果是低能量，就认为在换气。
    if words:
        mask = np.zeros(n, dtype=bool)
        for (_, t0, t1) in words:
            i0 = int(t0 * fps)
            i1 = int(t1 * fps) + 1
            mask[max(0, i0):min(n, i1)] = True
        # 只对"本来能量就低"的帧压，不要把字内轻声也吃掉
        openness[~mask & quiet] *= 0.05
        # 字与字交界做 1 帧的收口，看起来像换了口型
        for (_, t0, _t1) in words:
            i = int(t0 * fps)
            if 0 <= i < n:
                openness[i] = min(openness[i], 0.35)

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


def _soft_mask(w: int, h: int) -> "object":
    """椭圆径向羽化蒙版：中心 255，边缘 0，中间 smoothstep 过渡。"""
    from PIL import Image
    if w < 2 or h < 2:
        return Image.new("L", (max(1, w), max(1, h)), 255)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - (w - 1) / 2.0) / max(w / 2.0, 1e-6)
    ny = (yy - (h - 1) / 2.0) / max(h / 2.0, 1e-6)
    r = np.sqrt(nx * nx + ny * ny)
    a = np.clip((1.0 - r) / 0.62, 0.0, 1.0)
    a = a * a * (3.0 - 2.0 * a)            # smoothstep，过渡更柔和
    return Image.fromarray((a * 255).astype(np.uint8), mode="L")


def _mouth_shadow(w: int, h: int, op: float) -> "object":
    """唇缝阴影：中间暗、四周透明。用来表现"嘴唇分开了一条缝"。

    ⚠️ 不画牙齿和口腔 —— 单张图凭空造"张开的嘴"必然是贴纸感，
       实测画出来像贴了个长方形，所以只做一层柔和的暗部。
    """
    from PIL import Image
    if w < 2 or h < 2:
        return Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - (w - 1) / 2.0) / max(w / 2.0, 1e-6)
    ny = (yy - (h - 1) / 2.0) / max(h / 2.0, 1e-6)
    r = np.sqrt(nx * nx + ny * ny)
    a = np.clip(1.0 - r, 0.0, 1.0)
    a = a * a * (3.0 - 2.0 * a)
    a = a * (255.0 * 0.72 * float(np.clip(op, 0.0, 1.0)))
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :] = (46, 22, 24)
    rgba = np.dstack([rgb, a.astype(np.uint8)])
    return Image.fromarray(rgba, mode="RGBA")


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

    # 融合区：横向 ±0.85 唇宽；纵向 上 0.95、下 1.10 唇高。
    # 收得很紧 —— 范围一大就会把鼻子和下巴一起拖动，看起来像整块皮肤在滑。
    x0 = int(max(0, round(cx - mw * 0.85)))
    x1 = int(min(W, round(cx + mw * 0.85)))
    ytop = int(max(0, round(cy - mh * 0.95)))
    ybot = int(min(H, round(cy + mh * 1.10)))
    if x1 - x0 < 4:
        x1 = min(W, x0 + 4)
    if ybot - ytop < 4:
        ybot = min(H, ytop + 4)
    patch = base.crop((x0, ytop, x1, ybot))
    mask = _soft_mask(patch.width, patch.height)
    # 唇缝阴影层（比 patch 窄一点、扁一点，只覆盖唇部）
    sw = max(4, int(round(mw * 0.92)))
    sh = max(3, int(round(mh * 0.62)))
    return {"base": base, "patch": patch, "mask": mask,
            "box": (x0, ytop, x1, ybot),
            "shadow_size": (sw, sh),
            "geom": (cx, cy, mw, mh), "wh": (W, H)}


def _frame(prep: dict, openness: float, width: float):
    """按口型参数生成一帧。

    做法：先在唇缝位置画一个随张口度变大的"口腔"（暗色 + 上下牙），
    再把上唇片往上、下唇片往下各推开一段 —— 这才像张嘴。
    早期版本是把整块嘴部区域纵向 resize，实测在写实人脸上会变成
    "橡皮泥"（嘴被拉长、下巴断层、没有牙齿），所以换掉了。
    """
    from PIL import ImageDraw
    base = prep["base"]
    out = base.copy()
    cx, cy, mw, mh = prep["geom"]
    x0, ytop, x1, ybot, cyi = prep["box"]

    op = float(np.clip(openness, 0.0, 1.0))
    wd = float(np.clip(width, 0.70, 1.35))

    # ---- 口腔：暗色底 + 上下排牙 ----
    if op > 0.05:
        ow = max(3.0, mw * 1.05 * wd)
        oh = max(2.0, mh * (0.10 + op * 1.05))
        d = ImageDraw.Draw(out)
        d.ellipse([cx - ow / 2, cy - oh / 2, cx + ow / 2, cy + oh / 2],
                  fill=(44, 19, 21))
        # 上排牙：一条白，随张口度变高
        th = max(1.0, oh * 0.30)
        d.rounded_rectangle(
            [cx - ow * 0.40, cy - oh / 2 + oh * 0.05,
             cx + ow * 0.40, cy - oh / 2 + oh * 0.05 + th],
            radius=max(1.0, th * 0.35), fill=(236, 233, 227))
        # 下排牙：小一点、暗一点，别抢视线
        bh = max(0.8, oh * 0.16)
        d.rounded_rectangle(
            [cx - ow * 0.36, cy + oh / 2 - bh - oh * 0.04,
             cx + ow * 0.36, cy + oh / 2 - oh * 0.04],
            radius=max(1.0, bh * 0.35), fill=(212, 206, 198))

    # ---- 上下唇推开 ----
    dy = op * mh * 0.46
    if dy >= 0.5:
        out.paste(prep["up"], (x0, int(round(ytop - dy))), prep["mu"])
        out.paste(prep["lo"], (x0, int(round(cyi + dy))), prep["ml"])
    return out


def render(img_path: str, wav_path: str, out_mp4: str, words: list = None,
           fps: int = FPS, on_progress=None) -> dict:
    """逐帧渲染 + ffmpeg 合成。返回 {ok, frames, duration, path}"""
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
def speak(text: str, voice: str = "", rate: int = 0, style: str = "",
          engine: str = "auto") -> dict:
    """文本 → TTS → 口型对齐 → mp4。这是给工具/前端调的主入口。"""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(_img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图或用 AI 生成一张"}

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=style or "")
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
    r = render(_img_path(), wav, out, words=words)
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
    try:
        import torch  # noqa: F401
        torch_ok = True
    except Exception:                                   # noqa: BLE001
        torch_ok = False
    return {
        "ok": True,
        "ffmpeg": bool(exe),
        "ffmpeg_path": exe,
        "asr": bool(asr),
        "asr_dir": asr,
        "torch": torch_ok,
        "wav2lip": False,          # 本机 torch 不可用，永远是 False
        "fps": FPS,
        "max_seconds": MAX_SECONDS,
        "max_side": MAX_SIDE,
        "has_image": os.path.isfile(_img_path()),
        "note": ("参数化口型（声学特征驱动）"
                 + ("，已启用字级对齐" if asr else "，未找到 ASR 模型，仅声学驱动")),
    }


def list_videos(limit: int = 50) -> list:
    d = _vdir()
    items = []
    for f in os.listdir(d):
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
                      "url": "/api/avatar2/video/" + f})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def video_path(name: str) -> str:
    """安全取视频路径（挡目录穿越）。"""
    base = os.path.basename(name or "")
    if not base or not base.lower().endswith(".mp4"):
        return ""
    p = os.path.join(_vdir(), base)
    return p if os.path.isfile(p) else ""


def delete_video(name: str) -> dict:
    p = video_path(name)
    if not p:
        return {"ok": False, "error": "没有这个视频"}
    try:
        os.remove(p)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}
