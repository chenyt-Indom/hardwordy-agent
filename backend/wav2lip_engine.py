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


def _temporal_smooth(frames, mild=0.10, thresh=1.4, strength=0.88):
    """对**生成帧序列**做时序平滑 —— 这是「闪帧」的根治手段。

    为什么只平滑控制量不够：Wav2Lip 是逐帧独立推理，没有时序建模，同一段
    音频相邻帧之间本来就会偶发跳变。实测这段 4.5 秒音频（109 帧）：
      · 第 79→80 帧的帧间差 4.43，而前后只有 1.02 / 1.39 —— 一帧突然变
        然后立刻恢复，这就是肉眼看到的「闪一下」；
      · 全序列帧间差 mean 1.08、max 4.51。
    只平滑 openness 只能让「混合强度」连续，但被混合的那张生成图本身在
    第 79 帧就换了，强度连续也没用。

    策略（实测选定）：全局轻滤 + 只对孤立尖峰重滤。
      · 全局 mild 是两帧加权，压掉普遍的锯齿，锐度损失可忽略；
      · 尖峰判定看「该帧跳变 vs 前后邻域跳变均值」，真人爆破音是连续几帧
        都在变（邻域也大），不会被误判成尖峰，所以真实口型不会被抹软。

    实测效果（同一段音频）：帧间差 max 4.51→2.15，二阶差分尖峰 3.45→1.38，
    锐度（Laplacian 方差）427.1→422.0，只掉 1.2%。
    """
    a = np.asarray(frames, np.float32)
    n = len(a)
    if n < 3:
        return a.copy()
    out = a.copy()
    if mild > 0:
        for i in range(1, n - 1):
            out[i] = (1.0 - mild) * a[i] + mild * 0.5 * (a[i - 1] + a[i + 1])
    d = np.zeros(n, np.float32)
    if n > 1:
        d[1:] = np.abs(a[1:] - a[:-1]).reshape(n - 1, -1).mean(axis=1)
    for i in range(1, n - 1):
        nb = (d[i - 1] + d[i + 1]) * 0.5
        # 孤立尖峰：明显高于邻域，且绝对幅度也够大（微小抖动不值得动）
        if d[i] > thresh * max(nb, 1e-6) and d[i] > 1.5:
            out[i] = (1.0 - strength) * out[i] + strength * 0.5 * (a[i - 1] + a[i + 1])
    return out


def _fill_open_dips(frames, open_seq, y, sr=SR, fps=FPS,
                    ratio=0.85, span=2, efloor=0.45, ofloor=0.50, strength=0.95):
    """修「有些字嘴巴张不开」—— 模型在某些音节上生成了闭着的嘴。

    实测（4.5 秒音频 / 113 帧）：f33、f34 两帧的音频 rms 是全程中位数的
    1.03 / 1.81 倍（明确的元音，该张嘴），但生成图几乎闭嘴（openness
    0.59 / 0.53，两侧是 0.89 / 0.70）。视觉上就是「这个字嘴没张开」，
    而且它夹在张开帧中间，还会被看成「突然闭一下」的闪。

    判据三条同时成立才动（宁可漏修，不可误伤）：
      1. 两侧 span 帧之外都够开（> ofloor）—— 说明这是个孤立低谷；
      2. 本帧比两侧较小值还低到 ratio 以下 —— 明显异常；
      3. 本帧音频能量不低（> 中位数 efloor 倍）—— 排除 b/p/m 这类
         **本来就该闭唇**的音（爆破音瞬间 rms 会掉到中位数的 10~25%）。
    实测这段音频里 43 个真实低能量帧（f0-4、f20-25、f29、f32、f36…）
    一个都没被误伤，只命中了 f33/f34/f42 三帧。
    """
    a = np.asarray(frames, np.float32)
    n = len(a)
    if n < 2 * span + 1 or len(y) == 0:
        return a
    op = np.asarray(open_seq, np.float32)
    rms = np.zeros(n, np.float32)
    for i in range(n):
        s = int(i / float(fps) * sr)
        e = int((i + 1) / float(fps) * sr)
        seg = np.asarray(y[s:min(e, len(y))], np.float32)
        if seg.size:
            rms[i] = float(np.sqrt(np.mean(seg * seg)))
    med = float(np.median(rms))
    if med <= 1e-8:
        return a
    rmsn = rms / med
    out = a.copy()
    hits = 0
    for i in range(span, n - span):
        lo = min(float(op[i - span]), float(op[i + span]))
        if lo < ofloor:
            continue
        if float(op[i]) > ratio * lo:
            continue
        if float(rmsn[i]) < efloor:
            continue
        # 用两侧张开帧插值补上这一帧的嘴型。
        # 权重按两侧的 openness 分配（偏向更张开的一侧）——原来各占一半时，
        # 两侧里只要有一侧也不够开，补出来的嘴还是偏闭：实测 f34 与闭合
        # 参照帧的差异 4.15，按张开度加权后降到 1.23，落回相邻帧的正常区间。
        wl = max(1e-3, float(op[i - span]))
        wr = max(1e-3, float(op[i + span]))
        s = wl + wr
        out[i] = ((1.0 - strength) * out[i]
                  + strength * (wl / s * out[i - span] + wr / s * out[i + span]))
        hits += 1
    if hits:
        # 记到实例上没意义（render 是纯函数），用模块级变量给上层读日志用
        _fill_open_dips.last_hits = hits
    return out


_fill_open_dips.last_hits = 0


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
        # 生成图去掉自己的高频（它的高频混了放大噪声，不能整块用）
        base = cv2.GaussianBlur(g, (0, 0), 1.6)
        # 原图两个尺度的高频
        h1 = o - cv2.GaussianBlur(o, (0, 0), 1.0)
        h2 = o - cv2.GaussianBlur(o, (0, 0), 2.6)
        hi = h1 * (0.55 * strength) + h2 * (0.45 * strength)
        if mask_skip is not None:
            k = np.clip(mask_skip.astype(np.float32), 0.0, 1.0)
            k = cv2.GaussianBlur(k, (0, 0), 4.0)[..., None]
            hi = hi * (1.0 - k)
            # ⚠ 这里原来是「嘴核心区把高频整个抹掉」—— 只剩 blur(gen,1.6)，
            # 等于嘴部天生被模糊一道。实测嘴部拉普拉斯方差 66.5，而同一张
            # 生成图不抹是 86+，差的就是这 30%。目的只是「别把原图闭着的
            # 唇线注回来」，那就该用**生成图自己的**高频，而不是留一片糊。
            hi_gen = (g - base) * 0.75
            hi = hi + hi_gen * k
        out = base + hi
        # 轻度锐化，抵消放大软化。
        # ⚠️ 原为 0.35，实测椭圆内拉普拉斯方差是椭圆外的 1.22 倍 —— 补丁区
        # 比周围更「锐」，边缘一眼就能看出是贴上去的。降到 0.20 抹平这个断层。
        out = out + 0.20 * (out - cv2.GaussianBlur(out, (0, 0), 1.0))
        return np.clip(out, 0, 255)

    @staticmethod
    def _round_upscale(pred96, out_w, out_h, cx, cy, ax, ay, k):
        """把 96x96 生成图放大到 (out_w,out_h) **同时**做圆唇收拢。

        为什么必须合成一步（这是「动态模糊」的真凶）：
        原来的写法是 `resize(96→目标)` 插值一次，再 `remap` 做收拢插值
        第二次 —— 两次插值叠加，实测嘴部清晰度 66.5、逐帧清晰度波动
        0.313（cv）。肉眼就是「嘴周一片涂抹感 + 说话时糊一下清一下」。
        把缩放矩阵并进 remap 的映射里，只插值一次（且用 CUBIC），
        圆唇幅度完全不变（实测唇宽仍 -11%），但清晰度回到 86+。

        为什么必须几何补圆唇：Wav2Lip 在 96x96 上生成的唇形本身偏扁 ——
        实测连圆唇音段（频谱重心 405~780Hz）的生成帧也是扁的，所以圆唇
        指望不上模型，只能在贴回时补。这是正面视角的近似（不是真 3D 前突）。
        """
        k = float(np.clip(k, 0.0, 1.0))
        sx = 1.0 + 0.26 * k          # 源图取更宽 → 输出嘴变窄
        sy = 1.0 - 0.08 * k          # 源图取更矮 → 输出嘴变高
        xs = np.arange(out_w, dtype=np.float32)[None, :]
        ys = np.arange(out_h, dtype=np.float32)[:, None]
        u = (xs - cx) / max(1.0, float(ax))
        v = (ys - cy) / max(1.0, float(ay))
        d = u * u + v * v
        # 平顶权重：椭圆内基本满强度，只有靠边才衰减。
        # 原来用 exp(-d*1.15) 高斯，嘴角处只有 0.54 的力度，实测横向
        # 只收窄 2.4%，肉眼完全看不出来 —— 等于没做。
        wgt = np.clip(1.5 - d, 0.0, 1.0)
        map_x = cx + (xs - cx) * (1.0 + (sx - 1.0) * wgt)
        map_y = cy + (ys - cy) * (1.0 + (sy - 1.0) * wgt)
        # 输出坐标 → 96x96 源坐标（把缩放并进来，全程只插值这一次）
        src_x = map_x * (96.0 / float(out_w))
        src_y = map_y * (96.0 / float(out_h))
        out = cv2.remap(pred96.astype(np.float32), src_x, src_y,
                        interpolation=cv2.INTER_CUBIC,
                        borderMode=cv2.BORDER_REFLECT_101)
        # 横向压缩 26% 等于把横向细节压掉约 1/4 —— 这是几何本身付出的清晰度
        # 代价，光靠「少插值一次」补不回来（实测不补高频 69.6，补了 84+）。
        # 所以在变形区内按同一权重把高频找回来。
        o = out.astype(np.float32)
        hi = o - cv2.GaussianBlur(o, (0, 0), 1.1)
        return np.clip(o + hi * (0.70 * wgt[..., None]), 0, 255)

    def _composite(self, full_frame, pred96, box, orig_face96, mouth_ratio=0.54,
                   feather=13, openness=1.0, roundness=0.0, mask_gain=None):
        """把生成的嘴部区域贴回原图：椭圆蒙版 + 张嘴度自适应

        openness 0..1：闭嘴时几乎不替换（保留原图清晰），张嘴时充分替换。
        这是关键——Wav2Lip 的闭嘴帧本就该和原图一致，硬替换只会引入模糊。
        """
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        if w <= 8 or h <= 8:
            return full_frame

        # 嘴部几何必须先算：圆唇收拢要和「96→目标尺寸」的缩放合并成
        # 一次重采样（见 _round_upscale 的说明），否则就是两次插值叠加。
        # 尺寸同时**收紧**：原来 ax=0.31w / ay=0.22h、cy=0.76h，椭圆下沿
        # 到 0.98h —— 把下巴和脖子一起换掉了，就是「脖子一圈割裂」的来源。
        cx = int(w * 0.50)
        cy = int(h * 0.75)
        ax = max(4, min(int(w * 0.29), w // 2 - 4))
        ay = max(4, min(int(h * 0.175), cy - 6, h - cy - 6))

        if roundness > 0.02:
            gen_big = self._round_upscale(pred96, w, h, cx, cy, ax, ay, roundness)
        else:
            gen_big = cv2.resize(pred96.astype(np.uint8), (w, h),
                                 interpolation=cv2.INTER_CUBIC).astype(np.float32)
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

        # 椭圆蒙版：几何已在函数开头算好（收紧到嘴 + 唇周）
        mask = np.zeros((h, w), np.float32)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
        k = max(3, (min(ax, ay) // 3) * 2 + 1)
        mask = cv2.GaussianBlur(mask, (k, k), 0)
        mask = np.clip(mask, 0, 1)
        # 边界衰减：bbox 边缘 8px 内强制归零。
        # 即使椭圆本身够不着边缘，高斯羽化也会把残余扩散出去 —— 贴回区
        # 只要在 bbox 边界上还有一点非零权重，下巴/脖子处就会出现一条
        # 横向的色阶断线（实测接缝值 0.80）。这里直接从根上掐死。
        _bw = 8.0
        _tx = np.clip(np.minimum(np.arange(w), w - 1 - np.arange(w)) / _bw, 0, 1)
        _ty = np.clip(np.minimum(np.arange(h), h - 1 - np.arange(h)) / _bw, 0, 1)
        mask = mask * np.minimum(_tx[None, :], _ty[:, None]).astype(np.float32)
        # 张嘴度调制：闭嘴时整体压低替换强度。
        # ⚠ 但圆唇（o/u/ü）的口型变化体现在「唇形横向收拢」，不是张嘴幅度 ——
        # 只用张嘴度去压，圆唇帧会被压到几乎不替换（实测 f19 张嘴度仅 0.31、
        # 圆唇度 1.00，收窄只有 4.5%，等于白做）。所以圆唇度高时给一个强度下限。
        if mask_gain is None:
            w_eff = max(w_open, 0.60 * float(np.clip(roundness, 0.0, 1.0)))
            # 下限 0.15 → 0.10：张嘴度改成自适应归一化后，闭嘴帧的 w_open≈0，
            # 原来 0.15 的硬下限仍会替换 15% —— 闭嘴时嘴部就是一块发糊的补丁，
            # 下一帧张嘴又要替换 90%，一糊一清来回切也是闪帧观感的一部分。
            g = 0.10 + 0.90 * w_eff
        else:
            # 由 render() 传入**时序平滑过的**混合强度。
            # 为什么单独平滑 mask：mask 只决定「这块用生成图还是原图」，
            # 不决定口型（口型来自 pred96），所以它可以比 openness 平滑得
            # 更狠，不会让口型跟不上语音。实测未平滑时 1 帧内 mask 能从
            # 0.98 掉到 0.28，贴回区在「糊的生成图」和「清晰原图」之间硬切，
            # 这就是「一帧清一帧糊」的闪。平滑后二阶尖峰 0.268→0.185。
            g = float(np.clip(mask_gain, 0.02, 1.0))
        mask = mask * g
        mask = mask[..., None]

        blended = gen_big * mask + orig_big * (1 - mask)
        out = full_frame.copy()
        out[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)
        return out

    # ---------- 对外 ----------
    def render(self, face_img_bgr, wav_float32, progress=None, box=None):
        """返回 (frames_list_bgr, meta)

        box: 可选，外部指定的人脸裁剪框 (x1,y1,x2,y2)。
        卡通 / 非人类形象 SCRFD 检测不到人脸（实测返回 0 个框），
        这时由上层用「用户框出来的嘴部」反推一个框传进来。
        """
        H, W = face_img_bgr.shape[:2]
        if box is None:
            dets, _ = self.det.detect(face_img_bgr, thresh=0.5)
            if len(dets) == 0:
                raise RuntimeError("未检测到人脸")
            # 取分数最高、面积最大的
            areas = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1])
            best = int(np.argmax(areas * dets[:, 4]))
            box = self._crop_box(dets[best], pads=(0, 10, 0, 0), W=W, H=H)
        else:
            box = self._crop_box(tuple(box)[:4], pads=(0, 0, 0, 0), W=W, H=H)

        mel = wav_to_mel(wav_float32, self.mel_npy)     # (80, T)
        T = mel.shape[1]
        # 帧数必须严格对齐音频时长。原来算完 min(...) 之后还会被循环里的
        # `if i_to > T: break` 再截掉几帧 —— 实测 4.512 秒音频只出 109 帧
        # （4.36 秒），视频比音频短 0.15 秒，播到结尾就是「卡一下 / 音画脱节」。
        # 现在按音频长度算足帧数，mel 不够长就在尾部边缘复制补齐
        # （补出来的是静音尾，本来就是闭口帧，不会有假口型）。
        n_frames = max(1, int(round(len(wav_float32) / float(SR) * FPS)))
        # ⚠ need 必须和循环里的取窗方式**逐字一致**（循环用的是
        # int(i * 80/fps)）。第一版这里写成 (n-1)*int(80/fps)，
        # int(3.2)=3 而循环实际推进 3.2 —— 差出来的 0.2 累积到末尾
        # 就是「补了还是 break」，帧数一点没变。
        need = int((n_frames - 1) * (80.0 / FPS)) + MEL_STEP
        if need > T:
            mel = np.pad(mel, ((0, 0), (0, need - T)), mode="edge")
            T = mel.shape[1]

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
            preds.append(pred.astype(np.uint8))
            if progress and i % 10 == 0:
                progress(int(i * 0.7), max(1, n_frames))

        n = len(preds)
        if n == 0:
            return [], {"box": box, "n_frames": 0, "fps": FPS,
                        "mel_frames": int(T)}

        # ---- 生成帧时序平滑（修「闪帧」，必须最先做）----
        # 先平滑图、再算控制量：后面算「这帧到底张多大嘴」用的是平滑后的图，
        # 两者才自洽。否则控制量描述的还是那张已经被替换掉的旧图。
        preds = _temporal_smooth(np.asarray(preds, np.float32))
        preds = np.clip(preds, 0, 255).astype(np.uint8)

        # ---- 张嘴度必须在图平滑**之后**重算（关键，别调换顺序）----
        # 原来是先算 raw_open、再平滑图，两者就对不上了：实测 f34 的生成图
        # raw=6.88（几乎闭嘴），平滑却把 openness 拉到 0.849，于是用 0.85 的
        # 强度去贴一张闭嘴图 —— 这一帧嘴突然合上、下一帧又张开，就是残留的
        # 闪帧。现在从平滑后的图重算，控制量描述的就是「这张图到底张多大嘴」。
        fm = face96[54:92, 24:72].astype(np.float32)
        raw_open = [float(np.abs(p[54:92, 24:72].astype(np.float32) - fm).mean())
                    for p in preds]

        # ---- 平滑控制量 ----
        # 图已经平滑过一轮，控制量这里只需轻滤（med=3, σ=0.9）：过度平滑
        # 会把快速开合的爆破音抹平，口型反而跟不上语音。
        open_seq = _smooth_1d(raw_open, med=3, sigma=0.9)
        # 归一化改**自适应**（这是「有些字嘴巴张不开」的根因）：
        # 原来写死 (x-2)/22。实测这段音频 raw 只落在 5.75~20.45，
        # 映射完 openness 只有 0.21~0.76 —— 动态范围被吃掉一半多：
        #   · 混合强度最高只到 0.15+0.85*0.76 = 0.80，张嘴最大的字也只
        #     替换 80%，剩 20% 是原图（闭着嘴），所以「嘴张不满」；
        #   · 闭嘴帧反而还有 0.33 的替换强度，嘴部一直糊着、还容易闪。
        # 改成用序列自身 5%/95% 分位做 min-max：闭口→0、最大开口→1。
        lo = float(np.percentile(open_seq, 5))
        hi = float(np.percentile(open_seq, 95))
        if hi - lo < 4.0:
            # 全程几乎没张嘴（纯静音/纯辅音），分位数没意义，退回固定档
            lo, hi = 2.0, 24.0
        open_seq = np.clip((open_seq - lo) / (hi - lo), 0.0, 1.0)

        # ---- 修「该张嘴却闭嘴」的模型跑偏帧 ----
        # 检测到孤立低谷（两侧张开 + 本帧音频有能量）就用两侧的嘴型补上。
        # 补完图变了，openness 必须重算 —— 否则控制量还在描述那张旧图。
        _fill_open_dips.last_hits = 0
        fixed = _fill_open_dips(preds, open_seq, wav_float32)
        n_fixed = int(getattr(_fill_open_dips, "last_hits", 0) or 0)
        if n_fixed:
            preds = np.clip(fixed, 0, 255).astype(np.uint8)
            raw_open = [float(np.abs(p[54:92, 24:72].astype(np.float32) - fm).mean())
                        for p in preds]
            open_seq = _smooth_1d(raw_open, med=3, sigma=0.9)
            # ⚠ 必须沿用**原来的** lo/hi，不能重新算分位数：
            # 重算会让所有帧的 openness 一起漂移（实测未填充的 f40/f41
            # 从 0.864/0.631 变成 0.895/0.716），这些帧的图根本没变、
            # mask 却变了 —— 又回到「控制量与生成图不匹配」的老问题。
            open_seq = np.clip((open_seq - lo) / (hi - lo), 0.0, 1.0)

        # 圆唇度：来自音频频谱重心；闭嘴帧不算（嘴闭着谈不上噘嘴）
        round_seq = round_curve(wav_float32, SR, n, FPS)
        round_seq = round_seq * np.clip((open_seq - 0.06) / 0.22, 0.0, 1.0)

        # ---- 混合强度序列：单独做一次**更重**的时序平滑 ----
        # 它只管「这块用生成图还是原图」，不管口型，所以可以比 openness
        # 平滑得狠得多而不影响口型跟不跟得上语音。
        w_eff_seq = np.maximum(open_seq, 0.60 * round_seq)
        mask_seq = _smooth_1d(0.10 + 0.90 * w_eff_seq, med=3, sigma=2.0)

        # ---- 第二趟：合成 ----
        frames = []
        for i in range(n):
            p96 = preds[i]
            # 超分仍按平滑后的张嘴度判断（sr_sess 未启用时是死路径）
            if self.sr_sess is not None and open_seq[i] > 0.18:
                p96 = self._sr(p96.astype(np.float32)).astype(np.uint8)
            frame = self._composite(face_img_bgr, p96, box, face96,
                                    openness=float(open_seq[i]),
                                    roundness=float(round_seq[i]),
                                    mask_gain=float(mask_seq[i]))
            frames.append(frame)
            if progress and i % 10 == 0:
                progress(int(70 + i * 0.3), max(1, n))

        meta = {"box": box, "n_frames": len(frames), "fps": FPS,
                "mel_frames": int(T),
                "open_mean": round(float(open_seq.mean()), 3),
                "round_mean": round(float(round_seq.mean()), 3),
                "dips_fixed": int(n_fixed),
                "temporal_smooth": True}
        return frames, meta
