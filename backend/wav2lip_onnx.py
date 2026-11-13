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


def _feather_mask(h: int, w: int, top: int = 0):
    """下半脸的融合蒙版：上部渐入，避免贴回时出现硬边。"""
    m = np.ones((h, w), dtype=np.float32)
    band = max(2, int(h * 0.18))
    for i in range(band):
        v = (i + 1) / float(band + 1)
        yy = top + i
        if 0 <= yy < h:
            m[yy, :] = v
    return m[:, :, None]


# ---------------------------------------------------------------- 推理
def render(img_path: str, wav_path: str, out_mp4: str, fps: int = 25,
           on_progress=None) -> dict:
    """图片 + 音频 → 真口型视频。"""
    sess = session()
    if sess is None:
        return {"ok": False, "error": "没有 Wav2Lip 权重（data/models/wav2lip/wav2lip_gan.onnx）"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}
    if not os.path.isfile(wav_path):
        return {"ok": False, "error": "没有音频文件"}

    from . import avatar2
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

    mel = mel_spectrogram(y, sr)
    n_frames = int(round(dur * fps))
    chunks = mel_chunks(mel, fps, n_frames=n_frames)

    in_names = [i.name for i in sess.get_inputs()]
    out_name = sess.get_outputs()[0].name

    # 预处理：原图裁剪一次，后面每帧复用（静态图，人脸不动）
    face0 = img[y1:y2, x1:x2]
    face96 = cv2.resize(face0, (96, 96), interpolation=cv2.INTER_LINEAR)
    masked = face96.copy()
    masked[96 // 2:, :] = 0          # 下半脸遮住，让模型生成
    both = np.concatenate([masked, face96], axis=2).astype(np.float32) / 255.0
    both = both.transpose(2, 0, 1)[None]  # (1,6,96,96)

    # 融合蒙版（只贴下半部分，带羽化）
    cw, ch = x2 - x1, y2 - y1
    msk = _feather_mask(ch, cw, top=ch // 2 - 2)

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
    try:
        for i, ch_mel in enumerate(chunks):
            m = ch_mel[None, None, :, :].astype(np.float32)  # (1,1,80,16)
            feeds = {}
            for nm in in_names:
                low = nm.lower()
                if "mel" in low or "audio" in low:
                    feeds[nm] = m
                else:
                    feeds[nm] = both
            pred = sess.run([out_name], feeds)[0]            # (1,3,96,96)
            pred = pred[0].transpose(1, 2, 0)                # (96,96,3)
            pred = np.clip(pred * 255.0, 0, 255).astype(np.uint8)
            pred = cv2.cvtColor(pred, cv2.COLOR_RGB2BGR)
            patch = cv2.resize(pred, (cw, ch), interpolation=cv2.INTER_LINEAR)

            frame = img.copy()
            roi = frame[y1:y2, x1:x2].astype(np.float32)
            blend = patch.astype(np.float32) * msk + roi * (1 - msk)
            frame[y1:y2, x1:x2] = np.clip(blend, 0, 255).astype(np.uint8)

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
