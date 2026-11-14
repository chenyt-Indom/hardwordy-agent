"""Wav2Lip ONNX 口型同步引擎

纯 onnxruntime + numpy + cv2，不依赖 torch / librosa。

流程：
  音频 -> mel(80fps) -> 每视频帧取 16 帧 mel
  图像 -> SCRFD 检测 -> 裁脸 -> 6 通道输入(下半脸遮黑 + 原脸)
  -> ONNX -> 96x96 生成帧 -> 只替换嘴部区域 -> 颜色校正 + 细节回注 -> 贴回
"""
import os
import numpy as np
import cv2
import onnxruntime as ort

# ---- 音频参数（与 Wav2Lip hparams 一致）----
SR = 16000
N_FFT = 800
HOP = 200
WIN = 800
NUM_MELS = 80
FMIN = 55
FMAX = 7600
MIN_LEVEL_DB = -100.0
REF_LEVEL_DB = 20.0
MAX_ABS = 4.0
PREEMPH = 0.97
MEL_STEP = 16
FPS = 25

_MEL_BASIS = None


def _hann_periodic(n):
    return 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)


def _mel_basis(npy_path=None):
    global _MEL_BASIS
    if _MEL_BASIS is None:
        if npy_path and os.path.exists(npy_path):
            _MEL_BASIS = np.load(npy_path).astype(np.float64)
        else:
            _MEL_BASIS = _build_mel_basis()
    return _MEL_BASIS


def _hz_to_mel(f):
    """librosa hz_to_mel(htk=False) —— Slaney 公式"""
    f = np.asarray(f, dtype=np.float64)
    f_min = 0.0
    f_sp = 200.0 / 3.0
    mels = (f - f_min) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    if f.ndim == 0:
        if f >= min_log_hz:
            mels = min_log_mel + np.log(f / min_log_hz) / logstep
        return float(mels)
    mask = f >= min_log_hz
    mels = np.where(mask, min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, mels)
    return mels


def _mel_to_hz(m):
    """librosa mel_to_hz(htk=False) —— Slaney 公式"""
    m = np.asarray(m, dtype=np.float64)
    f_min = 0.0
    f_sp = 200.0 / 3.0
    freqs = f_min + f_sp * m
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    mask = m >= min_log_mel
    freqs = np.where(mask, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)
    return freqs


def _build_mel_basis():
    """librosa.filters.mel(slaney) 的等价实现"""
    fft_freqs = np.linspace(0, SR / 2.0, 1 + N_FFT // 2)
    mel_min = _hz_to_mel(FMIN)
    mel_max = _hz_to_mel(FMAX)
    mel_f = np.linspace(mel_min, mel_max, NUM_MELS + 2)
    hz = _mel_to_hz(mel_f)
    fd = np.diff(hz)
    ramps = hz[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fd[:-1, None]
    upper = ramps[2:] / fd[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    # Slaney 归一化
    enorm = 2.0 / (hz[2:NUM_MELS + 2] - hz[:NUM_MELS])
    weights *= enorm[:, None]
    return weights


def wav_to_mel(wav_float32, mel_npy=None):
    """16k float32 波形 -> (80, T) 归一化 mel"""
    w = wav_float32.astype(np.float64)
    # pre-emphasis: lfilter([1,-0.97],[1],w) 的向量化实现
    y = np.empty_like(w)
    y[0] = w[0]
    y[1:] = w[1:] - PREEMPH * w[:-1]
    # STFT (center=True, pad_mode=constant)
    pad = N_FFT // 2
    yp = np.pad(y, pad, mode="constant")
    win = _hann_periodic(WIN)
    n_frames = 1 + (len(yp) - N_FFT) // HOP
    if n_frames <= 0:
        return np.zeros((NUM_MELS, 0), np.float32)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = yp[idx] * win[None, :]
    spec = np.fft.rfft(frames, axis=1).T          # (401, T)
    mag = np.abs(spec)
    mel = _mel_basis(mel_npy) @ mag
    min_level = np.exp(MIN_LEVEL_DB / 20.0 * np.log(10.0))
    S = 20.0 * np.log10(np.maximum(min_level, mel)) - REF_LEVEL_DB
    M = np.clip((2 * MAX_ABS) * ((S - MIN_LEVEL_DB) / (-MIN_LEVEL_DB)) - MAX_ABS,
                -MAX_ABS, MAX_ABS)
    return M.astype(np.float32)


class Wav2LipEngine:
    def __init__(self, onnx_path, det, mel_npy=None, threads=4):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(onnx_path, so, providers=["CPUExecutionProvider"])
        self.in_names = [i.name for i in self.sess.get_inputs()]
        self.det = det
        self.mel_npy = mel_npy

    # ---------- 内部 ----------
    @staticmethod
    def _crop_box(bbox, pads=(0, 10, 0, 0), W=None, H=None):
        x1, y1, x2, y2 = bbox[:4]
        p_up, p_down, p_l, p_r = pads
        x1 -= p_l
        y1 -= p_up
        x2 += p_r
        y2 += p_down
        if W is not None:
            x1 = max(0, min(W - 2, x1))
            x2 = max(x1 + 2, min(W, x2))
        if H is not None:
            y1 = max(0, min(H - 2, y1))
            y2 = max(y1 + 2, min(H, y2))
        return int(x1), int(y1), int(x2), int(y2)

    def _prepare_face(self, img_bgr, box):
        x1, y1, x2, y2 = box
        face = img_bgr[y1:y2, x1:x2]
        face = cv2.resize(face, (96, 96), interpolation=cv2.INTER_LINEAR)
        masked = face.copy()
        masked[48:, :] = 0          # 遮住下半脸（实测该权重必须这样，遮右半会半脸发黑）
        inp = np.concatenate([masked, face], axis=2) / 255.0   # (96,96,6)
        return face, inp.astype(np.float32)

    @staticmethod
    def _match_color(src, ref):
        """把 src 的均值/方差对齐到 ref（逐通道）"""
        out = src.astype(np.float32)
        for c in range(3):
            s = out[..., c]
            r = ref[..., c].astype(np.float32)
            sm, ss = s.mean(), s.std() + 1e-6
            rm, rs = r.mean(), r.std() + 1e-6
            out[..., c] = (s - sm) / ss * rs + rm
        return np.clip(out, 0, 255)

    @staticmethod
    def _detail_inject(gen, orig, strength=0.6):
        """多尺度高频回注：把原图纹理注回生成图，缓解 96->大图放大的糊感"""
        g = gen.astype(np.float32)
        o = orig.astype(np.float32)
        # 生成图去掉自己的高频（它的高频是放大噪声）
        base = cv2.GaussianBlur(g, (0, 0), 1.6)
        # 原图两个尺度的高频
        h1 = o - cv2.GaussianBlur(o, (0, 0), 1.0)
        h2 = o - cv2.GaussianBlur(o, (0, 0), 2.6)
        out = base + h1 * (0.55 * strength) + h2 * (0.45 * strength)
        # 轻度锐化，抵消放大软化
        out = out + 0.35 * (out - cv2.GaussianBlur(out, (0, 0), 1.0))
        return np.clip(out, 0, 255)

    def _composite(self, full_frame, pred96, box, orig_face96, mouth_ratio=0.54,
                   feather=13, openness=1.0):
        """把生成的嘴部区域贴回原图：椭圆蒙版 + 张嘴度自适应

        openness 0..1：闭嘴时几乎不替换（保留原图清晰），张嘴时充分替换。
        这是关键——Wav2Lip 的闭嘴帧本就该和原图一致，硬替换只会引入模糊。
        """
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        if w <= 8 or h <= 8:
            return full_frame

        gen_big = cv2.resize(pred96.astype(np.uint8), (w, h), interpolation=cv2.INTER_CUBIC)
        orig_big = full_frame[y1:y2, x1:x2]

        gen_big = self._match_color(gen_big, orig_big)
        gen_big = self._detail_inject(gen_big, orig_big, strength=0.85)

        # 椭圆蒙版：中心在嘴部，轴长收在 bbox 内部，绝不触及边界
        cx = int(w * 0.50)
        cy = int(h * (1.0 - mouth_ratio * 0.55))
        ax = int(w * 0.38)
        ay = int(h * mouth_ratio * 0.62)
        ax = max(4, min(ax, w // 2 - 3))
        ay = max(4, min(ay, h // 2 - 3))
        mask = np.zeros((h, w), np.float32)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
        k = max(3, (min(ax, ay) // 3) * 2 + 1)
        mask = cv2.GaussianBlur(mask, (k, k), 0)
        mask = np.clip(mask, 0, 1)
        # 张嘴度调制：闭嘴时整体压低替换强度
        w_open = float(np.clip(openness, 0.0, 1.0))
        mask = mask * (0.15 + 0.85 * w_open)
        mask = mask[..., None]

        blended = gen_big * mask + orig_big * (1 - mask)
        out = full_frame.copy()
        out[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)
        return out

    # ---------- 对外 ----------
    def render(self, face_img_bgr, wav_float32, progress=None):
        """返回 (frames_list_bgr, meta)"""
        H, W = face_img_bgr.shape[:2]
        dets, _ = self.det.detect(face_img_bgr, thresh=0.5)
        if len(dets) == 0:
            raise RuntimeError("未检测到人脸")
        # 取分数最高、面积最大的
        areas = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1])
        best = int(np.argmax(areas * dets[:, 4]))
        box = self._crop_box(dets[best], pads=(0, 10, 0, 0), W=W, H=H)

        mel = wav_to_mel(wav_float32, self.mel_npy)     # (80, T)
        T = mel.shape[1]
        n_frames = max(1, int(len(wav_float32) / SR * FPS))
        n_frames = min(n_frames, max(1, (T - MEL_STEP) // 3 + 1))

        face96, face_inp = self._prepare_face(face_img_bgr, box)
        frames = []
        for i in range(n_frames):
            i_from = int(i * (80.0 / FPS))
            i_to = i_from + MEL_STEP
            if i_to > T:
                break
            chunk = mel[:, i_from:i_to][None, None, :, :].astype(np.float32)
            fin = np.transpose(face_inp, (2, 0, 1))[None].astype(np.float32)
            pred = self.sess.run(None, {self.in_names[0]: chunk,
                                        self.in_names[1]: fin})[0][0]
            pred = np.transpose(pred, (1, 2, 0))   # (96,96,3)
            pred = np.clip(pred, 0.0, 1.0) * 255.0
            frame = self._composite(face_img_bgr, pred, box, face96)
            frames.append(frame)
            if progress and i % 10 == 0:
                progress(i, n_frames)
        meta = {"box": box, "n_frames": len(frames), "fps": FPS,
                "mel_frames": int(T)}
        return frames, meta
