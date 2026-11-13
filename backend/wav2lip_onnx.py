"""Wav2Lip ONNX 推理 —— 真·语音驱动口型。

与 avatar2 的形变方案不同，这里是真的神经网络重绘嘴部：
音频 mel 谱 + 下半脸遮罩 → 模型生成嘴型 → 贴回原图。

不需要 torch（用 onnxruntime），因此能在主环境 Python 3.14 上跑。
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import numpy as np

# ---------------------------------------------------------------- 路径
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def model_path() -> str:
    """ONNX 权重路径；找不到返回空串。"""
    cands = [
        os.path.join(_root(), "data", "models", "wav2lip", "wav2lip_gan.onnx"),
        os.path.join(_root(), "data", "models", "wav2lip", "wav2lip.onnx"),
    ]
    for p in cands:
        if os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024:
            return p
    return ""


# ---------------------------------------------------------------- mel
# Wav2Lip 训练时的 hparams（必须严格一致，否则口型对不上）
SR = 16000
N_FFT = 800
HOP = 200
WIN = 800
N_MELS = 80
FMIN = 55
FMAX = 7600
PREEMPH = 0.97
REF_LEVEL_DB = 20.0
MIN_LEVEL_DB = -100.0
MAX_ABS = 4.0

_mel_basis_cache: dict = {}


def _hz_to_mel(f):
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(f >= min_log_hz,
                    min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
                    mels)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(m >= min_log_mel,
                    min_log_hz * np.exp(logstep * (m - min_log_mel)),
                    freqs)


def mel_basis(sr: int = SR, n_fft: int = N_FFT, n_mels: int = N_MELS,
              fmin: float = FMIN, fmax: float = FMAX):
    """librosa.filters.mel(htk=False) 的 numpy 复刻（Slaney 面积归一化）。"""
    key = (sr, n_fft, n_mels, fmin, fmax)
    if key in _mel_basis_cache:
        return _mel_basis_cache[key]
    fftfreqs = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fftfreqs[None, :]
    w = np.zeros((n_mels, len(fftfreqs)), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        w[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels])
    w *= enorm[:, None]
    _mel_basis_cache[key] = w
    return w


def _stft(y: np.ndarray, n_fft: int = N_FFT, hop: int = HOP, win: int = WIN):
    """librosa.stft 的等价实现。

    librosa 默认 center=True + pad_mode="constant"（两侧补零，不是反射），
    window 用周期性 hann（fftbins=True），这两点必须一致，否则边界帧会偏。
    """
    pad = n_fft // 2
    y = np.pad(y, (pad, pad), mode="constant")
    # 周期性 hann：0.5 - 0.5*cos(2*pi*n/win)，等价 scipy get_window('hann', win, fftbins=True)
    n = np.arange(win)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * n / win)).astype(np.float64)
    if win < n_fft:
        window = np.pad(window, (0, n_fft - win))
    n_frames = 1 + (len(y) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n_frames, n_fft),
        strides=(y.strides[0] * hop, y.strides[0]), writeable=False)
    spec = np.fft.rfft(frames * window, n=n_fft, axis=1)
    return spec.T  # (freq, time)


def _preemphasis(y: np.ndarray, k: float = PREEMPH):
    return np.append(y[0], y[1:] - k * y[:-1])


def _normalize(S: np.ndarray):
    if MAX_ABS is not None:
        return np.clip((2 * MAX_ABS) * ((S - MIN_LEVEL_DB) / (-MIN_LEVEL_DB)) - MAX_ABS,
                       -MAX_ABS, MAX_ABS)
    return np.clip((S - MIN_LEVEL_DB) / (-MIN_LEVEL_DB), 0, 1)


def mel_spectrogram(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """返回 (80, T) 的 mel 谱，值域 [-4, 4]，与 Wav2Lip 训练时一致。"""
    y = np.asarray(y, dtype=np.float64)
    if sr != SR:
        raise ValueError("需要 16k 采样率，收到 %d" % sr)
    # rescale（Wav2Lip 训练前做了 rescale=True, rescaling_max=0.9）
    m = np.max(np.abs(y))
    if m > 0:
        y = y / m * 0.9
    y = _preemphasis(y)
    D = _stft(y)
    S = np.abs(D)
    mel = np.dot(mel_basis(), S)
    mel = 20 * np.log10(np.maximum(mel, 1e-5)) - REF_LEVEL_DB
    return _normalize(mel).astype(np.float32)


# ---------------------------------------------------------------- 音频读取
def read_wav_16k(path: str):
    """读 wav，返回 (sr, float32 单声道)。非 16k 或非单声道会转。"""
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError("不支持的位深 %d" % sw)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        # 简单线性重采样
        n = int(round(len(a) * SR / sr))
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
        sr = SR
    return sr, a


# ---------------------------------------------------------------- 模型
_session = None
_model_path_cache = ""


def session():
    """懒加载 onnxruntime 会话。"""
    global _session, _model_path_cache
    p = model_path()
    if not p:
        return None
    if _session is not None and _model_path_cache == p:
        return _session
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = min(8, os.cpu_count() or 4)
    _session = ort.InferenceSession(p, so, providers=["CPUExecutionProvider"])
    _model_path_cache = p
    return _session


def model_info() -> dict:
    s = session()
    if s is None:
        return {"ok": False, "error": "没有 Wav2Lip 权重"}
    return {
        "ok": True,
        "path": _model_path_cache,
        "size_mb": round(os.path.getsize(_model_path_cache) / 2 ** 20, 1),
        "inputs": [{"name": i.name, "shape": i.shape, "type": i.type} for i in s.get_inputs()],
        "outputs": [{"name": o.name, "shape": o.shape, "type": o.type} for o in s.get_outputs()],
    }


def available() -> bool:
    try:
        return session() is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 人脸裁剪
MEL_STEP = 16


def _imread(path: str):
    """读图，兼容中文路径。返回 BGR uint8。"""
    import cv2
    img = cv2.imread(path)
    if img is None:
        from PIL import Image
        with Image.open(path) as im:
            arr = np.array(im.convert("RGB"))
        img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return img


def _avatar2():
    """拿 avatar2 模块；兼容「包内导入」和「独立文件加载」两种情况。"""
    try:
        from . import avatar2 as m
        return m
    except Exception:  # noqa: BLE001
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar2.py")
        spec = importlib.util.spec_from_file_location("_av2_standalone", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m


def _yunet_path() -> str:
    return _avatar2()._find_yunet()


def detect_face_box(img) -> list | None:
    """返回人脸框 [x1, y1, x2, y2]（像素）。失败返回 None。"""
    import cv2
    p = _yunet_path()
    if not p:
        return None
    h, w = img.shape[:2]
    try:
        det = cv2.FaceDetectorYN.create(p, "", (w, h), score_threshold=0.6,
                                        nms_threshold=0.3, top_k=5000)
        _n, faces = det.detect(img)
    except Exception:  # noqa: BLE001
        return None
    if faces is None or len(faces) == 0:
        return None
    faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
    f = faces[0]
    x, y, fw, fh = [float(v) for v in f[:4]]
    return [x, y, x + fw, y + fh]


def crop_rect(box, w: int, h: int):
    """按 Wav2Lip 官方的扩展比例算出裁剪矩形。"""
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    fh = y2 - y1
    fw = x2 - x1
    y1 = max(0, y1 - fh // 4)
    y2 = min(h, y2 + fh // 4)
    x1 = max(0, x1 - fw // 8)
    x2 = min(w, x2 + fw // 8)
    return x1, y1, x2, y2


def mel_chunks(mel: np.ndarray, fps: int, n_frames: int | None = None):
    """按 fps 把 mel 切成 (80,16) 的片段。"""
    mult = 80.0 / fps
    out = []
    i = 0
    T = mel.shape[1]
    while True:
        s = int(i * mult)
        if s + MEL_STEP > T:
            out.append(mel[:, max(0, T - MEL_STEP):])
            break
        out.append(mel[:, s:s + MEL_STEP])
        i += 1
        if n_frames and len(out) >= n_frames:
            break
    return out


def _mouth_mask(h: int, w: int, my_rel: float, rx: float = 0.36,
                up: float = 0.06, dn: float = 0.16, soft: float = 0.35):
    """嘴部**椭圆**融合蒙版（相对裁剪框尺寸）。

    2026-11-14 第三次修：原来用整行宽的矩形带，左右一直铺到裁剪框边缘，
    两颊和下巴的皮肤也被换成 96x96 的模糊输出，用户看到的是「整块嘴被切下来
    往下拽」的贴图感。实测四种蒙版对比，椭圆 rx=0.36 的两颊过渡最自然、
    无明显接缝。

    形状是上下不对称的椭圆：上边 up（人中不能动），下边 dn（要覆盖张开的
    口腔）。soft 是边缘渐变比例（相对椭圆半径）。
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cyv = my_rel * h
    cxv = w * 0.5
    dy = yy - cyv
    hh = np.where(dy < 0, up * h, dn * h)
    d = np.sqrt(((xx - cxv) / max(1e-6, rx * w)) ** 2
                + (dy / np.maximum(hh, 1e-3)) ** 2)
    m = np.clip((1.0 - d) / max(1e-6, soft), 0.0, 1.0)
    return m[:, :, None].astype(np.float32)


# ---------------------------------------------------------------- 推理
# 2026-11-14 清理：`_feather_mask`（矩形带状蒙版，已被 _mouth_mask 椭圆取代）、
# `_gap96` / `_lip_openness`（给已删除的幅度增强用）、`_boost_patch`（纵向拉伸）
# 全部删除 —— 都已无调用方，留着只会让下一个人以为还能用。


def render(img_path: str, wav_path: str, out_mp4: str, fps: int = 25,
           on_progress=None, boost: float = 1.0, expr: dict = None) -> dict:
    """图片 + 音频 → 真口型视频。

    boost: 口型幅度倍率（1.0=原始输出；1.2~1.6 明显放大；>1.8 会出涂抹感）
    expr : 表情层配置（avatar2_expr.resolve() 的返回值）。为 None 则不做表情。
           表情叠加在**嘴部合成之后**的整帧上，所以不会破坏 Wav2Lip 的口型。
    """
    sess = session()
    if sess is None:
        return {"ok": False, "error": "没有 Wav2Lip 权重（data/models/wav2lip/wav2lip_gan.onnx）"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}
    if not os.path.isfile(wav_path):
        return {"ok": False, "error": "没有音频文件"}

    avatar2 = _avatar2()
    exe = avatar2.ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}

    import cv2

    sr, y = read_wav_16k(wav_path)
    dur = len(y) / float(sr)
    if dur < 0.2:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}
    if dur > 120:
        return {"ok": False, "error": "音频 %.0f 秒，太长（Wav2Lip 建议 120 秒内）" % dur}

    img = _imread(img_path)
    if img is None:
        return {"ok": False, "error": "形象图读不出来"}
    H, W = img.shape[:2]
    box = detect_face_box(img)
    if box is None:
        return {"ok": False, "error": "没检测到人脸，无法做真口型（请换一张正面清晰的人像）"}
    x1, y1, x2, y2 = crop_rect(box, W, H)
    if x2 - x1 < 24 or y2 - y1 < 24:
        return {"ok": False, "error": "人脸太小，无法做口型"}

    # 表情层：用**未扩展**的人脸框定位眉眼带（crop_rect 会把框上下各扩 1/4，
    # 拿它定位会把眉眼带算到额头上）。
    _expr = expr or None
    _face_norm = None
    _ex_mod = None
    if _expr:
        try:
            from . import avatar2_expr as _ex_mod          # noqa: F811
            bx1, by1, bx2, by2 = box
            _face_norm = [bx1 / W, by1 / H, (bx2 - bx1) / W, (by2 - by1) / H]
        except Exception:                                  # noqa: BLE001
            _ex_mod = None

    mel = mel_spectrogram(y, sr)
    n_frames = int(round(dur * fps))
    chunks = mel_chunks(mel, fps, n_frames=n_frames)

    in_names = [i.name for i in sess.get_inputs()]
    out_name = sess.get_outputs()[0].name

    # 预处理：原图裁剪一次，后面每帧复用（静态图，人脸不动）
    face0 = img[y1:y2, x1:x2]
    face96 = cv2.resize(face0, (96, 96), interpolation=cv2.INTER_LINEAR)
    # 嘴在**裁剪框内**的相对位置。boost 遮罩、融合带、张口测量必须共用这一套
    # 坐标。2026-11-14 修：这里原来写 int(avatar2.mouth()["y"] * 96)，用的是
    # 整图归一化 y，和融合带的裁剪框坐标差 6px/96（0.583 vs 0.648）。结果模型
    # 把嘴生成在偏上位置、融合带却贴在偏下，用户看到「嘴位严重往下偏、下巴有
    # 模糊带」。
    try:
        _m0 = avatar2.mouth()
        my_rel = (float(_m0["y"]) * H - y1) / float(max(1, y2 - y1))
    except Exception:  # noqa: BLE001
        my_rel = 0.66
    my_rel = float(min(0.95, max(0.05, my_rel)))
    _mr = int(round(my_rel * 96))

    # ------------------------------------------------ 口型幅度：双遮罩混合
    # 2026-11-14 实测（同一张图、同一段音频、测 1024 图上的开口像素，
    # 人脸高 499px，真实说话张口约 15% ≈ 75px）：
    #
    #   遮罩方式              最大开口    占脸高   观感
    #   整下半脸(旧 boost1.0)   27px      5.4%   嘴几乎不动，用户反馈"没开口"
    #   窄遮罩(boost1.2)        79px     15.8%   幅度对了，但口腔糊成黑块、下颌重影
    #   中间档？                —— 没有。boost 1.1 反而更小(23px)，非单调
    #
    # 所以 boost 单独调不出"幅度够但不变形"的档位。改成**同一帧跑两套遮罩，
    # 再把两份模型输出按权重混合**：两个都是模型真实生成的嘴形，平均出来
    # 是可信的中间态，而不是靠拉伸硬造。
    #
    # 实测混合比 → 最大开口：w=0 →27, 0.3→31, 0.5→39, 0.7→70, 1.0→79
    # 视觉复核：w=0.5(39px) 最像正常说话，无重影无双唇线；w=1.0 明显重影。
    # 因此 boost=1.0 映射到 w=0.5（默认最佳），boost 越大越接近窄遮罩的大幅张嘴。
    try:
        _b = float(boost or 1.0)
    except Exception:  # noqa: BLE001
        _b = 1.0
    _b = float(min(1.4, max(1.0, _b)))
    _w_hi = float(min(1.0, max(0.5, 0.5 + 1.25 * (_b - 1.0))))  # 1.0→0.5, 1.4→1.0

    def _both_for(b: float):
        """按 boost 口径构造 (1,6,96,96) 的模型输入。"""
        m2 = face96.copy()
        if b <= 1.05:
            m2[96 // 2:, :] = 0
        else:
            k = min(1.0, (b - 1.0) / 0.8)
            up = int(round(14 + 10 * k))
            dn = int(round(16 + 12 * k))
            m2[max(0, _mr - up):min(96, _mr + dn), :] = 0
        a = np.concatenate([m2, face96], axis=2).astype(np.float32) / 255.0
        return a.transpose(2, 0, 1)[None]

    both_lo = _both_for(1.0)     # 宽遮罩：嘴几乎不动，但绝不变形
    both_hi = _both_for(1.2)     # 窄遮罩：张得大，口腔会糊

    # ------------------------------------------------ 融合蒙版（演进记录）
    # 这块参数前后改了三轮，每一轮都被用户看出问题，记下来免得再走回头路：
    #
    # ① 0.60~0.88 整行宽矩形带：把下巴一起换成 96x96 的模糊输出，用户反馈
    #    "嘴太吓人"（大片灰雾 + 黑色口腔空洞）。
    # ② 对称 ±0.045：下巴干净了，但只覆盖上下唇的**闭口**高度。实测 96x96
    #    输出里嘴是**向下**张开的（开口从唇缝 y≈62.7 压到 y≈75~78），对称带
    #    只到 y=67，张开的下半截被整段裁掉 → 用户反馈"位置对了但不开口"。
    # ③ 现在：非对称椭圆。上 0.06（人中不动）、下 0.16（覆盖整个开口）、
    #    横向 rx=0.36（只包嘴周，两颊保原图）。
    #
    # 实测依据：
    #   · 下边 0.13 → 开口只有 16~19px，等于没张开；0.16 → 16~27px，明显开合
    #   · 下边 0.18 或旧 0.88 → 下巴被换掉，重影脏块
    #   · 矩形 vs 椭圆：椭圆两颊过渡自然，矩形有明显水平接缝（视觉复核结论）
    #   · 音频驱动强度（mel 缩放 1.0/0.9/0.8/0.7）对开口幅度几乎无影响，
    #     所以调幅度只能动蒙版，不能动 mel
    cw, ch = x2 - x1, y2 - y1
    msk = _mouth_mask(ch, cw, my_rel, rx=0.36, up=0.06, dn=0.16, soft=0.35)

    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-",
           "-i", wav_path,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-shortest", "-movflags", "+faststart", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    n = len(chunks)
    _base_gap = None
    _boost_on = bool(boost and boost > 1.001)
    try:
        def _infer(binput, mel_in):
            """跑一次模型，返回 (96,96,3) 的 float32（0~255，BGR）。"""
            feeds = {}
            for nm in in_names:
                low = nm.lower()
                feeds[nm] = mel_in if ("mel" in low or "audio" in low) else binput
            p = sess.run([out_name], feeds)[0][0].transpose(1, 2, 0)
            return p

        for i, ch_mel in enumerate(chunks):
            m = ch_mel[None, None, :, :].astype(np.float32)  # (1,1,80,16)
            # 两套遮罩各跑一次，再按权重混合（见上面 _w_hi 的说明）。
            # w=1.0 时只跑窄遮罩，省一半时间。
            if _w_hi >= 0.999:
                pred = _infer(both_hi, m)
            elif _w_hi <= 0.001:
                pred = _infer(both_lo, m)
            else:
                pred = (_infer(both_lo, m) * (1.0 - _w_hi)
                        + _infer(both_hi, m) * _w_hi)
            pred = np.clip(pred * 255.0, 0, 255).astype(np.uint8)
            # 模型输入输出都是 BGR（官方用 cv2 读图，未做通道转换），
            # 这里绝不能再 cvtColor，否则嘴唇会发紫。
            patch = cv2.resize(pred, (cw, ch), interpolation=cv2.INTER_CUBIC)
            # 幅度由上面的遮罩范围控制（模型真实生成），不再叠加拉伸：
            # 实测叠加会让牙齿/口腔出现涂抹和断层，且增益只有 2px。
            # 96x96 放大到原尺寸必然发糊，补一层**很轻**的 USM。
            # 2026-11-14 实测四档（0 / 0.12 / 0.28 / 0.45）：0.28 和 0.45 会把
            # 口腔内部那块本就模糊的暗区锐成一坨色块和光晕，视觉上"牙齿不干净"；
            # 0.12 能提一点唇缘清晰度又不放大脏污，是平衡点。
            # （0.28 是旧值，用户反馈"嘴部糊成色块"跟它有关。）
            if cw > 160 or ch > 160:
                _b = cv2.GaussianBlur(patch, (0, 0), 1.0)
                patch = cv2.addWeighted(patch, 1.12, _b, -0.12, 0)
            # 注：曾试过用上半脸均值差做整体色调平移，实测把本就偏暗的下半脸
            # 提亮，下巴出现明显白斑，比不做更差，已回退。

            frame = img.copy()
            roi = frame[y1:y2, x1:x2].astype(np.float32)
            blend = patch.astype(np.float32) * msk + roi * (1 - msk)
            frame[y1:y2, x1:x2] = np.clip(blend, 0, 255).astype(np.uint8)

            # 微表情放在**嘴部合成之后**：口型已由 Wav2Lip 生成好，
            # 整帧再做轻量仿射不会破坏它（反过来先动再贴会错位）。
            if _ex_mod is not None:
                try:
                    # 说话强度取本帧 mel 均值：静音段 mel≈-4，有声段更高，
                    # 用它对表情幅度做门控，避免不说话时还在挤眉弄眼。
                    _en = float(np.clip((float(ch_mel.mean()) + 4.0) / 8.0, 0.0, 1.0))
                    _ex_mod.apply(frame, i / float(fps), _expr,
                                  face=_face_norm, energy=_en)
                except Exception:                          # noqa: BLE001
                    pass

            proc.stdin.write(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "渲染中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    err = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s"
                % (err or b"")[:400].decode("utf-8", "ignore")}
    return {"ok": True, "frames": n, "duration": round(dur, 2),
            "size": [W, H], "path": out_mp4, "engine": "wav2lip",
            "bytes": os.path.getsize(out_mp4),
            "face_box": [x1, y1, x2, y2]}
