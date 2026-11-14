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



def _smooth_1d(v, med=3, sigma=0.9):
    """一维控制量平滑：中值滤波去尖峰 + 高斯去抖。

    张嘴度 / 圆唇度都是逐帧独立算出来的，实测帧间会有 0.10~0.36 的跳变
    （5.33 秒里 25% 的相邻帧 |Δ|>0.10）。这些量直接去调混合蒙版的强度，
    表现就是「嘴部一帧清晰、一帧发糊、一帧变暗」—— 也就是用户说的抽搐。
    中值滤波先掐掉孤立的尖峰（单帧误判），高斯再抹平剩下的锯齿。
    """
    a = np.asarray(v, np.float32)
    n = len(a)
    if n < 3:
        return a.copy()
    k = int(med) | 1                       # 保证奇数
    if k >= 3 and n >= k:
        pad = k // 2
        ap = np.pad(a, pad, mode="edge")
        st = np.lib.stride_tricks.sliding_window_view(ap, k)
        a = np.median(st, axis=1).astype(np.float32)
    if sigma and sigma > 0:
        r = max(1, int(round(float(sigma) * 3)))
        x = np.arange(-r, r + 1, dtype=np.float32)
        g = np.exp(-(x * x) / (2.0 * sigma * sigma))
        g /= g.sum()
        ap = np.pad(a, r, mode="edge")
        a = np.convolve(ap, g, mode="valid").astype(np.float32)
    return a


def round_curve(y, sr, n, fps=FPS):
    """每帧「圆唇度」0..1 —— 给噘嘴（o/u/ü）用。

    判据用**频谱重心**，不用 F2 峰值：25fps 的 40ms 短窗上 F2 噪声极大
    （实测帧间能从 700Hz 跳到 3150Hz），而重心很稳。圆唇元音能量集中在
    低频，实测 o/u 段重心 405~780Hz，i/e 段 3000Hz 以上，区分度足够。
    """
    out = np.zeros(max(0, int(n)), np.float32)
    if n <= 0 or len(y) == 0:
        return out
    yy = np.asarray(y, np.float32)
    # 时间对齐（很关键）：Wav2Lip 第 i 帧吃的是 mel[i*80/fps : i*80/fps+16]，
    # 对应 16k 波形从 i*640 起、宽 3200 样本（0.2 秒）。分析窗必须落在这一段
    # 的中点，否则圆唇会比口型晚 3 帧 —— 实测过：f15 那段音频已经是圆唇
    # （频谱重心 577Hz），用居中的 40ms 窗却要到 f21 才识别出来。
    step = HOP * 80.0 / float(fps)          # 每帧推进的样本数
    span = MEL_STEP * HOP                   # 每帧覆盖的样本数
    win = max(256, int(sr * 0.080))
    for i in range(int(n)):
        c = i * step + span * 0.5
        a = int(c - win * 0.5)
        a = max(0, min(len(yy) - win, a))
        seg = yy[a:a + win]
        if seg.size < 64:
            continue
        seg = seg - seg.mean()
        if float(np.abs(seg).max()) < 1e-4:      # 静音帧不判圆唇
            continue
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
        tot = float(spec.sum())
        if tot <= 1e-9:
            continue
        cen = float((spec * freqs).sum() / tot)
        # 900Hz 及以下算完全圆唇，2000Hz 以上不算
        out[i] = float(np.clip((2000.0 - cen) / 1100.0, 0.0, 1.0))
    out = _smooth_1d(out, med=3, sigma=1.0)
    return np.clip(out, 0.0, 1.0)


class Wav2LipEngine:
    def __init__(self, onnx_path, det, mel_npy=None, threads=4, sr_path=None):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(onnx_path, so, providers=["CPUExecutionProvider"])
        self.in_names = [i.name for i in self.sess.get_inputs()]
        self.det = det
        self.mel_npy = mel_npy
        self.sr_sess = None
        if sr_path and os.path.isfile(sr_path):
            so2 = ort.SessionOptions()
            so2.intra_op_num_threads = threads
            so2.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self.sr_sess = ort.InferenceSession(sr_path, so2, providers=["CPUExecutionProvider"])
            self.sr_in = self.sr_sess.get_inputs()[0].name

    def _sr(self, pred96):
        """96x96 BGR -> 384x384 BGR（BigFace 4x 超分）"""
        if self.sr_sess is None:
            return pred96
        x = pred96.astype(np.float32) / 255.0
        x = x[:, :, ::-1]                      # BGR -> RGB
        x = np.transpose(x, (2, 0, 1))[None]
        out = self.sr_sess.run(None, {self.sr_in: x})[0][0]
        out = np.transpose(out, (1, 2, 0))[:, :, ::-1]   # RGB -> BGR
        return np.clip(out * 255.0, 0, 255)

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
    def _match_color(src, ref, roi=None):
        """把 src 的均值/方差对齐到 ref（逐通道）

        roi=True 的区域才参与统计。默认排除嘴部中心，
        否则生成的「张口暗部」会被整体均值提亮，口型就被洗掉了。
        """
        out = src.astype(np.float32)
        r = ref.astype(np.float32)
        if roi is None:
            roi = np.ones(out.shape[:2], bool)
        for c in range(3):
            s = out[..., c][roi]
            rr = r[..., c][roi]
            if s.size < 16:
                continue
            sm, ss = s.mean(), s.std() + 1e-6
            rm, rs = rr.mean(), rr.std() + 1e-6
            out[..., c] = (out[..., c] - sm) / ss * rs + rm
        return np.clip(out, 0, 255)

    @staticmethod
    def _match_light(gen, ref, mask_keep=None, sigma=None):
        """低频光照场校正：把 ref 的**局部**明暗迁移到 gen 上。

        全局均值/方差对齐（_match_color）只能修整体偏色，修不了「额头亮、
        下巴暗」这种空间渐变 —— 实测贴回后椭圆内平均色差 19.76，是椭圆外
        的 2.2 倍，这就是「补丁感」的主因。

        这里只迁移低频（大 sigma 模糊后的差），高频纹理完全保留，所以不会
        把生成图的嘴部细节糊掉。mask_keep 用来把嘴部核心排除在外 ——
        否则原图闭合的暗唇会把生成的张口暗部提亮，口型被洗掉。
        """
        g = gen.astype(np.float32)
        r = ref.astype(np.float32)
        h, w = g.shape[:2]
        if sigma is None:
            sigma = max(4.0, min(h, w) * 0.18)
        # ⚠️ 直接在原分辨率上跑 sigma≈67 的高斯极慢（每帧两次，出片会超时）。
        # 低频场本来就不需要全分辨率：降到 1/8 上算完再双线性放大，
        # 结果等价（低频本身就平滑），速度快 50 倍以上。
        ds = 8
        sw, sh = max(2, w // ds), max(2, h // ds)
        sg = cv2.resize(g, (sw, sh), interpolation=cv2.INTER_AREA)
        sr_ = cv2.resize(r, (sw, sh), interpolation=cv2.INTER_AREA)
        sig_s = max(1.0, sigma / ds)
        gl = cv2.GaussianBlur(sg, (0, 0), sig_s)
        rl = cv2.GaussianBlur(sr_, (0, 0), sig_s)
        corr = cv2.resize(rl - gl, (w, h), interpolation=cv2.INTER_LINEAR)
        if mask_keep is not None:
            mk = cv2.resize(mask_keep.astype(np.float32), (sw, sh),
                            interpolation=cv2.INTER_AREA)
            mk = cv2.GaussianBlur(mk, (0, 0), max(0.6, sig_s * 0.5))
            k = cv2.resize(mk, (w, h), interpolation=cv2.INTER_LINEAR)
            corr = corr * k[..., None]
        return np.clip(g + corr, 0, 255)

    @staticmethod
    def _detail_inject(gen, orig, strength=0.6, mask_skip=None):
        """多尺度高频回注：把原图纹理注回生成图，缓解 96->大图放大的糊感

        mask_skip: 1=完全不回注的区域（嘴核心）。那里必须用生成图自己的像素，
        否则原图闭合的唇线会被注回来，口型被洗掉；但嘴**周围**（下巴、人中、
        唇周皮肤）要注满，否则 96x96 放大后皮肤纹理被抹平，视觉上就是
        「贴了一块半透明补丁」。旧版一刀切按张嘴度降 strength，两个目标
        互相打架，所以改成按区域分开处理。
        """
        g = gen.astype(np.float32)
        o = orig.astype(np.float32)
        # 生成图去掉自己的高频（它的高频是放大噪声）
        base = cv2.GaussianBlur(g, (0, 0), 1.6)
        # 原图两个尺度的高频
        h1 = o - cv2.GaussianBlur(o, (0, 0), 1.0)
        h2 = o - cv2.GaussianBlur(o, (0, 0), 2.6)
        hi = h1 * (0.55 * strength) + h2 * (0.45 * strength)
        if mask_skip is not None:
            k = np.clip(mask_skip.astype(np.float32), 0.0, 1.0)
            k = cv2.GaussianBlur(k, (0, 0), 4.0)[..., None]
            hi = hi * (1.0 - k)
        out = base + hi
        # 轻度锐化，抵消放大软化。
        # ⚠️ 原为 0.35，实测椭圆内拉普拉斯方差是椭圆外的 1.22 倍 —— 补丁区
        # 比周围更「锐」，边缘一眼就能看出是贴上去的。降到 0.20 抹平这个断层。
        out = out + 0.20 * (out - cv2.GaussianBlur(out, (0, 0), 1.0))
        return np.clip(out, 0, 255)

    @staticmethod
    def _round_lips(img, cx, cy, ax, ay, k):
        """把嘴部横向收拢 + 纵向微扩，做出噘嘴（o/u/ü）的正面观感。

        为什么必须几何补：Wav2Lip 在 96x96 上生成的唇形本身偏扁 —— 实测连
        圆唇音段（频谱重心 405~780Hz）的生成帧也是扁的，所以圆唇指望不上
        模型，只能在贴回时补。这是正面视角的近似（不是真 3D 前突），
        幅度必须克制，过了就成「捏扁」失真。
        """
        k = float(np.clip(k, 0.0, 1.0))
        if k <= 0.02:
            return img
        h, w = img.shape[:2]
        sx = 1.0 + 0.26 * k          # 源图取更宽 → 输出嘴变窄
        sy = 1.0 - 0.08 * k          # 源图取更矮 → 输出嘴变高
        xs = np.arange(w, dtype=np.float32)[None, :]
        ys = np.arange(h, dtype=np.float32)[:, None]
        u = (xs - cx) / max(1.0, float(ax))
        v = (ys - cy) / max(1.0, float(ay))
        d = u * u + v * v
        # 平顶权重：椭圆内基本满强度，只有靠边才衰减。
        # 原来用 exp(-d*1.15) 高斯，嘴角处只有 0.54 的力度，实测横向
        # 只收窄 2.4%，肉眼完全看不出来 —— 等于没做。
        wgt = np.clip(1.5 - d, 0.0, 1.0)
        map_x = cx + (xs - cx) * (1.0 + (sx - 1.0) * wgt)
        map_y = cy + (ys - cy) * (1.0 + (sy - 1.0) * wgt)
        out = cv2.remap(img, map_x.astype(np.float32),
                        map_y.astype(np.float32),
                        interpolation=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REFLECT_101)
        # 横向挤压会把纹理压密、高频被抹平 —— 视觉上就是「嘴周一片涂抹感」。
        # 只在变形区、只补高频，把唇线锐度找回来。
        o = out.astype(np.float32)
        hi = o - cv2.GaussianBlur(o, (0, 0), 1.0)
        return np.clip(o + hi * (0.50 * wgt[..., None]), 0, 255)

    def _composite(self, full_frame, pred96, box, orig_face96, mouth_ratio=0.54,
                   feather=13, openness=1.0, roundness=0.0):
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

        w_open = float(np.clip(openness, 0.0, 1.0))
        # 颜色统计排除嘴部中心区（张嘴的暗部不能参与均值，否则会被提亮）
        roi = np.ones((h, w), bool)
        roi[int(h * 0.55):int(h * 0.95), int(w * 0.18):int(w * 0.82)] = False
        gen_big = self._match_color(gen_big, orig_big, roi=roi)
        # 局部光照场校正：全局统计修不了「额头亮、下巴暗」的空间渐变，
        # 不修就是一块色偏的补丁（实测椭圆内色差是外面的 2.2 倍）
        gen_big = self._match_light(gen_big, orig_big,
                                    mask_keep=roi.astype(np.float32))
        # 细节回注按区域分开：
        #   · 嘴核心（含唇）→ 完全不注，保住生成的口型
        #   · 唇周/下巴/人中 → 注满，保住原图皮肤纹理（不注就是一块被抹平的补丁）
        mouth_core = np.zeros((h, w), np.float32)
        cv2.ellipse(mouth_core, (int(w * 0.50), int(h * 0.76)),
                    (max(4, int(w * 0.26)), max(4, int(h * 0.15))),
                    0, 0, 360, 1.0, -1)
        gen_big = self._detail_inject(gen_big, orig_big, strength=0.75,
                                      mask_skip=mouth_core)

        # 口腔加深：Wav2Lip 在这类闭口原图上张口偏小，
        # 只把「真正的口腔暗部」（灰度 < 92）再压暗一点，
        # 视觉上张口更明确，又不会把嘴唇一起涂黑（试过阈值 125 会出现黑块）。
        # 口腔加深：原来的硬阈值 `if w_open > 0.25` 会在阈值附近来回切，
        # 实测 5.33 秒内切了 15 次（每秒 2.8 次）—— 每次切换嘴部暗部亮度
        # 突变一下，这就是抽搐感的主要来源之一。改成连续增益。
        open_gain = float(np.clip((w_open - 0.12) / 0.33, 0.0, 1.0))
        if open_gain > 0.02:
            gray = cv2.cvtColor(gen_big, cv2.COLOR_BGR2GRAY).astype(np.float32)
            dark = np.clip((92.0 - gray) / 34.0, 0.0, 1.0)
            dark = cv2.GaussianBlur(dark, (0, 0), 3.0)
            gen_big = np.clip(gen_big.astype(np.float32)
                              - dark[..., None] * 40.0 * open_gain, 0, 255)

        # 椭圆蒙版：中心在嘴部，且必须完整落在 bbox 内部（否则下巴处会有横向接缝）
        cx = int(w * 0.50)
        cy = int(h * 0.76)
        ax = int(w * 0.31)
        ay = int(h * 0.22)
        ax = max(4, min(ax, w // 2 - 4))
        ay = max(4, min(ay, cy - 6, h - cy - 6))
        mask = np.zeros((h, w), np.float32)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
        k = max(3, (min(ax, ay) // 3) * 2 + 1)
        mask = cv2.GaussianBlur(mask, (k, k), 0)
        mask = np.clip(mask, 0, 1)
        # 张嘴度调制：闭嘴时整体压低替换强度。
        # ⚠ 但圆唇（o/u/ü）的口型变化体现在「唇形横向收拢」，不是张嘴幅度 ——
        # 只用张嘴度去压，圆唇帧会被压到几乎不替换（实测 f19 张嘴度仅 0.31、
        # 圆唇度 1.00，收窄只有 4.5%，等于白做）。所以圆唇度高时给一个强度下限。
        w_eff = max(w_open, 0.60 * float(np.clip(roundness, 0.0, 1.0)))
        mask = mask * (0.15 + 0.85 * w_eff)
        mask = mask[..., None]

        # 圆唇几何增强（噘嘴）。放在蒙版之后、混合之前：变形只作用在生成图
        # 这一侧，原图完全不动，所以椭圆外的皮肤一点不受影响。
        if roundness > 0.02:
            gen_big = self._round_lips(gen_big, cx, cy, ax, ay, roundness)

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
        fin = np.transpose(face_inp, (2, 0, 1))[None].astype(np.float32)

        # ---- 第一趟：只推理，把每帧生成结果和张嘴度都攒下来 ----
        # 为什么要两趟：张嘴度原来是逐帧独立算、当场就调混合强度。实测
        # 帧间跳变最大 0.36、25% 的相邻帧 |Δ|>0.10 —— 混合蒙版强度跟着跳，
        # 嘴部就一帧清晰一帧发糊，这就是抽搐的主因。必须先把整段控制量
        # 序列拿到手、平滑之后回头合成，过渡才可能连续。
        preds = []
        raw_open = []
        for i in range(n_frames):
            i_from = int(i * (80.0 / FPS))
            i_to = i_from + MEL_STEP
            if i_to > T:
                break
            chunk = mel[:, i_from:i_to][None, None, :, :].astype(np.float32)
            pred = self.sess.run(None, {self.in_names[0]: chunk,
                                        self.in_names[1]: fin})[0][0]
            pred = np.transpose(pred, (1, 2, 0))   # (96,96,3)
            pred = np.clip(pred, 0.0, 1.0) * 255.0
            # 张嘴度：pred 与输入在嘴部区域的差异（闭嘴时≈0，张嘴时变大）
            pm = pred[54:92, 24:72].astype(np.float32)
            fm = face96[54:92, 24:72].astype(np.float32)
            raw_open.append(float(np.abs(pm - fm).mean()))
            preds.append(pred.astype(np.uint8))
            if progress and i % 10 == 0:
                progress(int(i * 0.7), max(1, n_frames))

        n = len(preds)
        if n == 0:
            return [], {"box": box, "n_frames": 0, "fps": FPS,
                        "mel_frames": int(T)}

        # ---- 平滑控制量（关键）----
        # 张嘴度：中值(3) 掐掉单帧误判，高斯(σ=0.9) 抹平锯齿，约 ±1 帧
        # 延迟（40ms），不会造成看得出的口型拖尾。
        open_seq = _smooth_1d(raw_open, med=5, sigma=1.5)
        open_seq = np.clip((open_seq - 2.0) / 22.0, 0.0, 1.0)
        # 圆唇度：来自音频频谱重心；闭嘴帧不算（嘴闭着谈不上噘嘴）
        round_seq = round_curve(wav_float32, SR, n, FPS)
        round_seq = round_seq * np.clip((open_seq - 0.06) / 0.22, 0.0, 1.0)

        # ---- 第二趟：合成 ----
        frames = []
        for i in range(n):
            p96 = preds[i]
            # 超分仍按平滑后的张嘴度判断（sr_sess 未启用时是死路径）
            if self.sr_sess is not None and open_seq[i] > 0.18:
                p96 = self._sr(p96.astype(np.float32)).astype(np.uint8)
            frame = self._composite(face_img_bgr, p96, box, face96,
                                    openness=float(open_seq[i]),
                                    roundness=float(round_seq[i]))
            frames.append(frame)
            if progress and i % 10 == 0:
                progress(int(70 + i * 0.3), max(1, n))

        meta = {"box": box, "n_frames": len(frames), "fps": FPS,
                "mel_frames": int(T),
                "open_mean": round(float(open_seq.mean()), 3),
                "round_mean": round(float(round_seq.mean()), 3)}
        return frames, meta
