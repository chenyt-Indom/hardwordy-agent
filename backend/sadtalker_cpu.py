# -*- coding: utf-8 -*-
"""SadTalker CPU 移植：3DMM 驱动的生成式数字人（纯 CPU、自包含）。

## 为什么需要它
本机原有三条路都到不了「自然」：
  · 2D 形变 —— 源图闭嘴时没有「张开」的像素，只能拉宽唇缝，做不出上下唇分离
  · 3D 网格 —— 实测唇部锐度 12.1、开口动态范围 4.9（几乎不动）
  · Wav2Lip 96x96 —— 唇部锐度只有原图的 41%，96 像素是硬天花板
SadTalker 用 3DMM 系数驱动 face-vid2vid 神经渲染，**能生成真实张口与口腔结构**，
实测唇部锐度 57.2（高于 Wav2Lip 的 49.5）。代价是 CPU 上约 2.1 秒/帧。

## 六个网络（权重全部来自 SadTalker_V0.0.2_256.safetensors，逐个校验 100% 匹配）
  face_3drecon   332 tensor  ResNet50 + 7 分支 → 257 维
  audio2exp       93 tensor  mel → 64 维表情
  audio2pose     419 tensor  CVAE：mel → 6 维头姿
  generator      386 tensor  face-vid2vid 生成器
  kp_extractor    75 tensor  关键点提取
  mapping         18 tensor  3DMM → 15 kp（用 mapping_00109 截断 73→70 复用）

## 三个必须照做的细节（都踩过）
1. **source_image 必须是 RGB**：cv2.imread 给 BGR，直接喂会红蓝互换 → 肤色偏蓝紫。
2. **必须 5 点相似变换对齐**：用官方 similarity_Lm3D_all.mat 的 68 点模板，
   注意它的 y 轴向上为正、图像 y 轴向下为正，要翻转。不对齐误差 27.3px、
   脸被拉长且低头；对齐后 1.75px。
3. **mapping 输入是时间窗 (B,70,2r+1)** 且 r ≥ 12，否则 Conv1d(kernel=7) +
   3 层 encoder 的逐层 -6 会把长度压成负数。

## 分辨率
生成器原生只有 256x256，这是模型硬上限。默认 upscale=2 输出 512（整画面一致、
无接缝）。贴回原图（paste_back=True）实测更差：整脸 256→604 放大 2.4 倍后唇部
锐度从原图 120 掉到 9，且逆变换在鼻影处留重影接缝。
"""
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "_sadtalker_src")
_MODELS = os.path.join(_ROOT, "data", "models", "sadtalker")

FPS = 25
SEMANTIC_RADIUS = 12
GEN_SIZE = 256
SR = 16000
N_FFT = 800
HOP = 200
NUM_MELS = 80
FMIN = 55.0
FMAX = 7600.0

_CACHE = {}


# ---------------------------------------------------------------- 基础设施
def _ensure_path():
    for p in (os.path.join(_SRC, "_stubs"), _SRC):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def available() -> bool:
    need = ["SadTalker_V0.0.2_256.safetensors", "mapping_00109-model.pth.tar"]
    return all(os.path.isfile(os.path.join(_MODELS, n)) for n in need) and os.path.isdir(_SRC)


def _ffmpeg():
    from . import avatar2 as A2
    return A2.ffmpeg()


def _to_wav16k(src):
    """任意音频 → 16k 单声道 wav。"""
    if not src or not os.path.isfile(src):
        raise ValueError("音频文件不存在：%s" % src)
    if src.lower().endswith(".wav"):
        try:
            with wave.open(src, "rb") as w:
                if w.getframerate() == SR and w.getnchannels() == 1:
                    return src
        except Exception:                                   # noqa: BLE001
            pass
    tmp = tempfile.mkdtemp(prefix="st16k_")
    dst = os.path.join(tmp, "a.wav")
    r = subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", src,
                        "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", dst],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("音频转码失败：%s" % (r.stderr or "")[-300:])
    return dst


def _read_wav(path):
    """读 16k 单声道 wav → (sr, float32 [-1,1])。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        y = y.reshape(-1, nch).mean(axis=1)
    return sr, y


def _hz_to_mel(f):
    f = np.asarray(f, np.float64)
    f_sp = 200.0 / 3.0
    mels = f / f_sp
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(f >= min_log_hz,
                    min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asarray(m, np.float64)
    f_sp = 200.0 / 3.0
    freqs = f_sp * m
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(m >= min_log_mel,
                    min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def _mel_basis():
    """80 阶 mel 滤波器组（与 Wav2Lip hparams 一致：htk=False, norm=slaney）。"""
    if "mel" in _CACHE:
        return _CACHE["mel"]
    n_freq = N_FFT // 2 + 1
    fftfreqs = np.linspace(0, SR / 2.0, n_freq)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(FMIN), _hz_to_mel(FMAX), NUM_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fftfreqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    # slaney 归一化
    enorm = 2.0 / (mel_f[2:NUM_MELS + 2] - mel_f[:NUM_MELS])
    weights *= enorm[:, None]
    _CACHE["mel"] = weights.astype(np.float32)
    return _CACHE["mel"]


def wav_to_mel(y):
    """波形 → (80, T) log-mel，参数对齐 Wav2Lip（n_fft800/hop200/win800）。"""
    y = np.asarray(y, np.float32)
    if y.ndim > 1:
        y = y.reshape(-1)
    # 预加重
    y = np.append(y[0], y[1:] - 0.97 * y[:-1])
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)
    pad = N_FFT // 2
    yp = np.pad(y, (pad, pad), mode="reflect")
    n = 1 + (len(yp) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = yp[idx] * win
    spec = np.abs(np.fft.rfft(frames, N_FFT, axis=1)) ** 2
    mel = spec @ _mel_basis().T
    mel = np.log10(np.maximum(1e-10, mel))
    mel = np.maximum(mel, mel.max() - 8.0)
    mel = (mel + 4.0) / 4.0
    return np.clip(mel, 0, 1).T.astype(np.float32)


# ---------------------------------------------------------------- 网络
def _load_nets():
    if "nets" in _CACHE:
        return _CACHE["nets"]
    _ensure_path()
    import torch
    import yaml
    from safetensors.torch import load_file
    from yacs.config import CfgNode as CN
    from src.facerender.modules.generator import OcclusionAwareSPADEGenerator
    from src.facerender.modules.keypoint_detector import KPDetector
    from src.facerender.modules.mapping import MappingNet
    from src.audio2exp_models.networks import SimpleWrapperV2
    from src.audio2pose_models.audio2pose import Audio2Pose
    from src.face3d.models import networks as f3net

    ck = load_file(os.path.join(_MODELS, "SadTalker_V0.0.2_256.safetensors"))
    mp = yaml.safe_load(open(os.path.join(_SRC, "src/config/facerender.yaml"),
                             encoding="utf-8"))["model_params"]
    gen = OcclusionAwareSPADEGenerator(**mp["generator_params"], **mp["common_params"])
    gen.load_state_dict({k[len("generator."):]: v for k, v in ck.items()
                         if k.startswith("generator.")})
    kp = KPDetector(**mp["kp_detector_params"], **mp["common_params"])
    kp.load_state_dict({k[len("kp_extractor."):]: v for k, v in ck.items()
                        if k.startswith("kp_extractor.")})
    mpn = MappingNet(**mp["mapping_params"])
    mt = dict(torch.load(os.path.join(_MODELS, "mapping_00109-model.pth.tar"),
                         map_location="cpu", weights_only=False)["mapping"])
    mt["first.0.weight"] = mt["first.0.weight"][:, :70, :].contiguous()
    mpn.load_state_dict(mt)
    ne = SimpleWrapperV2()
    ne.load_state_dict({k[len("audio2exp."):]: v for k, v in ck.items()
                        if k.startswith("audio2exp.")})
    cfg = CN.load_cfg(open(os.path.join(_SRC, "src/config/auido2pose.yaml"),
                           encoding="utf-8"))
    a2p = Audio2Pose(cfg, None, device="cpu")
    a2p.load_state_dict({k[len("audio2pose."):]: v for k, v in ck.items()
                         if k.startswith("audio2pose.")})
    recon = f3net.define_net_recon("resnet50", use_last_fc=False, init_path=None)
    recon.load_state_dict({k[len("face_3drecon."):]: v for k, v in ck.items()
                           if k.startswith("face_3drecon.")})
    for m in (gen, kp, mpn, ne, a2p, recon):
        m.eval()
    _CACHE["nets"] = (gen, kp, mpn, ne, a2p, recon)
    return _CACHE["nets"]


def _std5_px(size=224):
    from scipy.io import loadmat
    lm = loadmat(os.path.join(_SRC, "src/config/similarity_Lm3D_all.mat"))["lm"]
    std = np.array([lm[36:42, :2].mean(0), lm[42:48, :2].mean(0),
                    lm[30, :2], lm[48, :2], lm[54, :2]], np.float32)
    sc = size / 2.0
    out = np.empty_like(std)
    out[:, 0] = std[:, 0] * sc + sc
    out[:, 1] = -std[:, 1] * sc + sc        # lm3d 的 y 向上为正，图像向下为正
    return out


def align_face(img, size=224):
    """5 点相似变换对齐到 3DMM 标准正面。返回 (对齐图, 变换矩阵) 或 (None, None)。"""
    from .face_detect_scrfd import SCRFD
    det = SCRFD(os.path.join(_ROOT, "data", "models", "wav2lip",
                             "scrfd_2.5g_bnkps.onnx"))
    dets, kps = det.detect(img, thresh=0.5)
    if dets is None or len(dets) == 0 or kps is None or len(kps) == 0:
        return None, None
    k5 = np.array(kps[0], np.float32)[:5]
    M, _ = cv2.estimateAffinePartial2D(k5, _std5_px(size), method=cv2.LMEDS)
    if M is None:
        return None, None
    return cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REFLECT), M


def _coeff70(nets, face_rgb):
    import torch
    recon = nets[5]
    x = torch.tensor(face_rgb / 255.0, dtype=torch.float32).permute(2, 0, 1)[None]
    with torch.no_grad():
        out = recon(x)[0].numpy()
    _, exp, _, angle, _, txy, tz = np.split(out, [80, 144, 224, 227, 254, 256])
    return np.concatenate([exp, angle, txy, tz]).astype(np.float32)


def _indiv_mels(y, n_frames):
    mel = wav_to_mel(y)
    out = np.zeros((n_frames, NUM_MELS, 16), np.float32)
    for i in range(n_frames):
        idx = int(i * 80.0 / FPS)
        seg = mel[:, idx:idx + 16]
        if seg.shape[1] < 16:
            seg = np.pad(seg, ((0, 0), (0, 16 - seg.shape[1])), mode="edge")
        out[i] = seg
    return out


def _target_coeff(nets, indiv, src70, n_frames):
    import torch
    from scipy.signal import savgol_filter
    ne, a2p = nets[3], nets[4]
    mel_t = torch.FloatTensor(indiv).unsqueeze(1).unsqueeze(0)
    with torch.no_grad():
        exp_pred = ne(mel_t.view(-1, 1, NUM_MELS, 16),
                      torch.FloatTensor(np.repeat(src70[None, :64], n_frames, 0))[None],
                      torch.zeros(1, n_frames))
        mi = torch.cat([mel_t, mel_t[:, -1:]], 1)
        pose = a2p.test({"ref": torch.FloatTensor(src70)[None, None],
                         "class": torch.LongTensor([0]),
                         "indiv_mels": mi,
                         "num_frames": torch.LongTensor([n_frames])})["pose_pred"]
    pl = pose.shape[1]
    pl2 = int((pl - 1) / 2) * 2 + 1 if pl < 13 else 13
    pose_s = torch.FloatTensor(savgol_filter(pose[0].numpy(), pl2, 2, axis=0))[None]
    return torch.cat([exp_pred, pose_s], -1)[0].numpy()


def _sem_win(coeffs, i, r=SEMANTIC_RADIUS):
    N = coeffs.shape[0]
    idx = [min(max(i + k, 0), N - 1) for k in range(-r, r + 1)]
    return coeffs[idx, :].transpose(1, 0)


# ---------------------------------------------------------------- 主流程
def render(img_path, wav16_path, out_mp4, amp=1.0, upscale=2, paste_back=False,
           on_progress=None, threads=0, limit_sec=0.0):
    """图 + 16k wav → 说话视频。

    amp        口型幅度（0.6~2.0），只放大「相对 source 的偏移」
    upscale    输出放大倍数（默认 2 → 512）。生成器原生 256，这是硬上限
    paste_back True 时贴回原图尺寸（实测更糊且有接缝，仅在要 1024 画布时开）
    """
    import torch
    if threads <= 0:
        threads = min(8, os.cpu_count() or 4)
    torch.set_num_threads(threads)
    nets = _load_nets()

    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("形象图读不出来：%s" % img_path)
    H, W = img.shape[:2]

    face224, M224 = align_face(img, 224)
    if face224 is None:
        raise ValueError("没检测到人脸，无法做 3DMM 对齐")
    M256 = M224.copy()
    M256[:, :2] *= GEN_SIZE / 224.0
    M256[:, 2] *= GEN_SIZE / 224.0
    src256_bgr = cv2.warpAffine(img, M256, (GEN_SIZE, GEN_SIZE),
                                flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    # ⚠ 必须 RGB
    src70 = _coeff70(nets, cv2.cvtColor(face224, cv2.COLOR_BGR2RGB))

    sr, y = _read_wav(wav16_path)
    if y is None or len(y) == 0:
        raise ValueError("音频读不出来")
    n = int(round(len(y) / float(sr) * FPS))
    if limit_sec and limit_sec > 0:
        n = min(n, int(limit_sec * FPS))
    if n <= 0:
        raise ValueError("音频太短")

    indiv = _indiv_mels(y, n)
    coeffs = _target_coeff(nets, indiv, src70, n)

    amp = float(np.clip(amp, 0.6, 2.0))
    if abs(amp - 1.0) > 1e-3:
        ref = src70[None, :]
        coeffs = ref + (coeffs - ref) * amp

    src_sem = torch.FloatTensor(np.stack([src70] * (2 * SEMANTIC_RADIUS + 1), 1)[None])
    tgt_sem = torch.FloatTensor(np.stack([_sem_win(coeffs, i) for i in range(n)], 0)[None])

    from src.facerender.modules.make_animation import make_animation
    src_t = torch.FloatTensor(cv2.cvtColor(src256_bgr, cv2.COLOR_BGR2RGB)
                              .transpose(2, 0, 1)[None] / 255.0)
    with torch.no_grad():
        vid = make_animation(src_t, src_sem, tgt_sem, nets[0], nets[1], None, nets[2],
                             use_exp=True)
    frames = vid[0].cpu().numpy()

    tmpdir = tempfile.mkdtemp(prefix="stcpu_")
    fpat = os.path.join(tmpdir, "f%05d.png")
    k = max(1, int(upscale or 1))
    for i in range(frames.shape[0]):
        f = np.clip(frames[i].transpose(1, 2, 0) * 255.0, 0, 255).astype(np.uint8)
        f = cv2.cvtColor(f, cv2.COLOR_RGB2BGR)
        if paste_back:
            f = _paste_back(f, img, M256, W, H)
        elif k > 1:
            f = cv2.resize(f, (GEN_SIZE * k, GEN_SIZE * k),
                           interpolation=cv2.INTER_LANCZOS4)
        cv2.imencode(".png", f)[1].tofile(fpat % i)
        if on_progress and (i % 5 == 0 or i == frames.shape[0] - 1):
            on_progress(i + 1, frames.shape[0])

    cmd = [_ffmpeg(), "-y", "-loglevel", "error",
           "-framerate", str(FPS), "-i", fpat, "-i", wav16_path,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 合成失败：%s" % (r.stderr or "")[-400:])
    try:
        for fn in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, fn))
        os.rmdir(tmpdir)
    except OSError:
        logger.debug("清理临时目录失败", exc_info=True)

    return {"ok": True, "path": out_mp4, "frames": int(frames.shape[0]),
            "duration": round(frames.shape[0] / float(FPS), 2),
            "size": [W, H] if paste_back else [GEN_SIZE * k, GEN_SIZE * k],
            "bytes": os.path.getsize(out_mp4), "engine": "sadtalker-cpu",
            "paste_back": bool(paste_back)}


def _paste_back(face_bgr, img_orig, M256, W, H):
    inv = cv2.invertAffineTransform(M256)
    warped = cv2.warpAffine(face_bgr, inv, (W, H), flags=cv2.INTER_CUBIC,
                            borderMode=cv2.BORDER_REFLECT)
    m = np.zeros((GEN_SIZE, GEN_SIZE), np.uint8)
    cv2.ellipse(m, (GEN_SIZE // 2, int(GEN_SIZE * 0.52)),
                (int(GEN_SIZE * 0.40), int(GEN_SIZE * 0.46)),
                0, 0, 360, 255, -1, lineType=cv2.LINE_AA)
    m = cv2.GaussianBlur(m, (0, 0), GEN_SIZE * 0.02)
    mask = cv2.warpAffine(m, inv, (W, H), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = (mask.astype(np.float32) / 255.0)[..., None]
    return np.clip(warped.astype(np.float32) * a + img_orig.astype(np.float32) * (1 - a),
                   0, 255).astype(np.uint8)


def speak(text, voice="", rate=None, style="", engine="auto",
          amp=1.0, upscale=2, paste_back=False, on_progress=None):
    """文本 → TTS → SadTalker → mp4。"""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not available():
        return {"ok": False, "error": "SadTalker 权重不完整（需要 "
                                      "SadTalker_V0.0.2_256.safetensors 与 "
                                      "mapping_00109-model.pth.tar）"}
    from . import avatar2 as A2
    if not os.path.isfile(A2._img_path()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图"}
    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=(style or "").strip())
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    try:
        wav = _to_wav16k(a.get("path") or "")
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "音频转码失败：%s" % e}
    name = "avatar_st_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4"
    out = os.path.join(A2._vdir(), name)
    try:
        r = render(A2._img_path(), wav, out, amp=amp, upscale=upscale,
                   paste_back=paste_back, on_progress=on_progress)
    except Exception as e:                                   # noqa: BLE001
        logger.exception("sadtalker 渲染失败")
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    from urllib.parse import quote
    r.update({"name": name, "url": "/api/avatar2/video/" + quote(name),
              "audio_url": a.get("url") or "", "audio_engine": a.get("engine") or "",
              "lipsync_label": "SadTalker 3DMM 生成式口型", "text": t[:200]})
    return r


def status():
    ok = available()
    return {"available": ok, "engine": "sadtalker-cpu", "models_dir": _MODELS,
            "src_dir": _SRC if os.path.isdir(_SRC) else "", "gen_size": GEN_SIZE,
            "fps": FPS,
            "note": ("3DMM 驱动神经渲染，能生成真实张口与口腔；CPU 约 2.1 秒/帧"
                     if ok else "权重缺失")}
