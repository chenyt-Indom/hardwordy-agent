# -*- coding: utf-8 -*-
"""音乐生成与改编（纯本地算法合成，2026-11-12）。

为什么是"算法合成"而不是 MusicGen / Stable Audio
------------------------------------------------
本机 Python 3.14 + 没有 torch（导入即 OSError: shm.dll）+ 没有 ffmpeg，
MusicGen / AudioCraft / Riffusion 这类神经网络音乐模型**一个都跑不了**。
但 numpy 2.5 / scipy 1.18 在，所以走**减法合成 + 物理建模 + 共振峰人声**：

  · 振荡器：锯齿 / 方波 / 三角 / 正弦，多振荡器失谐做宽声场；
  · 物理建模：Karplus-Strong 拨弦（古筝、吉他），音色真实感最好；
  · FM 合成：钢琴、电钢琴；
  · 鼓组：正弦扫频 kick、噪声带通 snare、高通 hihat；
  · 人声：**共振峰合成**（formant synthesis）—— 脉冲源过 3 个谐振峰，
    得到可辨识的 a/e/i/o/u 元音，加颤音 → 人声吟唱 / 合唱声部；
  · 效果：ADSR、一阶低通/高通、Schroeder 风格混响（FFT 卷积）、
    延迟、软限幅、立体声声像。

能做什么（诚实边界，不吹）
--------------------------
✅ 旋律 + 和弦 + 贝斯 + 鼓 + pad + 人声吟唱，8 种风格，最长 300 秒；
✅ 改编：换风格重编（音阶映射）、移调、变速、换配器、变奏、续写；
✅ 给音符串（如 "C4 D4 E4 G4" 或简谱 "1 2 3 5"）编成完整曲子；
❌ **不是真人演唱**：人声是共振峰合成的"啊/哦/咿"吟唱，唱不出清晰歌词；
❌ 音色偏电子/合成器，不是采样音源那种真实乐器；
❌ 不能"上传一首歌改风格"（要人声分离 + 神经网络，本机做不到）。

产物
----
data/music/<id>.wav          音频（16bit PCM 立体声）
data/music/scores/<id>.json  乐谱（改编的输入：和弦/旋律/鼓型/风格/调）
"""
from __future__ import annotations

import json
import math
import os
import random
import time

import numpy as np
from scipy import signal as _sig

SR = 44100
MAX_SECONDS = 300
MAX_BARS = 160


# ============================================================
# 一、基础工具
# ============================================================
def _m2f(midi) -> float:
    """MIDI 音高 → 频率（Hz）。"""
    return 440.0 * (2.0 ** ((np.asarray(midi, dtype=np.float64) - 69.0) / 12.0))


def _n(dur: float, sr: int = SR) -> int:
    return max(1, int(round(dur * sr)))


def _adsr(n: int, a=0.01, d=0.12, s=0.65, r=0.25, sr: int = SR) -> np.ndarray:
    """ADSR 包络。各段样本数按比例压到 n 以内，短音符也不会溢出。"""
    a_n = max(1, int(a * sr))
    d_n = max(1, int(d * sr))
    r_n = max(1, int(r * sr))
    total = a_n + d_n + r_n
    if total >= n:                      # 音符太短：等比压缩
        k = n / float(total)
        a_n = max(1, int(a_n * k))
        d_n = max(1, int(d_n * k))
        r_n = max(1, n - a_n - d_n)
        s_n = 0
    else:
        s_n = n - a_n - d_n - r_n
    env = np.concatenate([
        np.linspace(0.0, 1.0, a_n, endpoint=False),
        np.linspace(1.0, s, d_n, endpoint=False),
        np.full(max(0, s_n), s),
        np.linspace(s, 0.0, r_n),
    ])
    if len(env) < n:
        env = np.concatenate([env, np.zeros(n - len(env))])
    return env[:n].astype(np.float64)


def _osc(freq, n: int, kind: str = "saw", sr: int = SR,
         vib_hz=0.0, vib_amt=0.0, phase0=0.0) -> np.ndarray:
    """振荡器。freq 可以是标量或长度 n 的数组；带可选颤音（累积相位实现）。"""
    f = np.asarray(freq, dtype=np.float64)
    if f.ndim == 0:
        f = np.full(n, float(f))
    elif len(f) != n:
        f = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(f)), f)
    if vib_hz and vib_amt:
        t = np.arange(n) / sr
        f = f * (1.0 + vib_amt * np.sin(2 * np.pi * vib_hz * t))
    phase = (phase0 + np.cumsum(f) / sr) % 1.0
    if kind == "sine":
        return np.sin(2 * np.pi * phase)
    if kind == "tri":
        return 4.0 * np.abs(phase - 0.5) - 1.0
    if kind == "square":
        return np.where(phase < 0.5, 1.0, -1.0)
    if kind == "pulse":
        return np.where(phase < 0.25, 1.0, -1.0)
    return 2.0 * phase - 1.0                       # saw


def _lp(x: np.ndarray, fc: float, sr: int = SR) -> np.ndarray:
    """一阶低通（lfilter，O(N)）。"""
    fc = max(30.0, min(float(fc), sr * 0.45))
    alpha = 1.0 - math.exp(-2.0 * math.pi * fc / sr)
    return _sig.lfilter([alpha], [1.0, alpha - 1.0], x)


def _hp(x: np.ndarray, fc: float, sr: int = SR) -> np.ndarray:
    """一阶高通。"""
    fc = max(20.0, min(float(fc), sr * 0.45))
    alpha = 1.0 - math.exp(-2.0 * math.pi * fc / sr)
    return _sig.lfilter([1.0 - alpha, alpha - 1.0], [1.0, alpha - 1.0], x)


def _lp_var(x: np.ndarray, fc_arr, sr: int = SR, segs: int = 16) -> np.ndarray:
    """**时变**截止频率的一阶低通（滤波器系数不能逐样本变，所以分段做）。

    用上一段的末状态当下一段初值（lfilter 的 zi），段与段之间不会有台阶。
    """
    n = len(x)
    if n == 0:
        return x
    fc_arr = np.asarray(fc_arr, dtype=np.float64)
    if fc_arr.ndim == 0:
        return _lp(x, float(fc_arr), sr)
    if len(fc_arr) != n:
        fc_arr = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(fc_arr)), fc_arr)
    out = np.zeros(n, dtype=np.float64)
    zi = np.zeros(1)
    edges = np.linspace(0, n, min(segs, n) + 1).astype(int)
    for i in range(len(edges) - 1):
        a, b = int(edges[i]), int(edges[i + 1])
        if b <= a:
            continue
        fc = float(np.mean(fc_arr[a:b]))
        fc = max(30.0, min(fc, sr * 0.45))
        alpha = 1.0 - math.exp(-2.0 * math.pi * fc / sr)
        y, zi = _sig.lfilter([alpha], [1.0, alpha - 1.0], x[a:b], zi=zi)
        out[a:b] = y
    return out


def _reson(x: np.ndarray, f: float, bw: float, sr: int = SR) -> np.ndarray:
    """二阶谐振器（共振峰用）。"""
    f = max(80.0, min(float(f), sr * 0.45))
    r = math.exp(-math.pi * bw / sr)
    th = 2.0 * math.pi * f / sr
    a1 = -2.0 * r * math.cos(th)
    a2 = r * r
    b0 = (1.0 - r) * math.sqrt(1.0 + r * r - 2.0 * r * math.cos(2.0 * th))
    return _sig.lfilter([b0], [1.0, a1, a2], x)


def _soft(x: np.ndarray) -> np.ndarray:
    """软限幅：tanh 形状，保住谐波不硬削。"""
    return np.tanh(x * 1.15) * 0.92


def _norm(x: np.ndarray, peak: float = 0.92) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return x if m < 1e-9 else (x / m) * peak


def _reverb_ir(size: float = 0.7, decay: float = 3.2, sr: int = SR,
               seed: int = 7) -> np.ndarray:
    """合成混响脉冲响应：指数衰减噪声 + 几个早期反射。"""
    n = max(64, int(size * sr))
    t = np.arange(n) / sr
    rng = np.random.RandomState(seed)
    ir = rng.randn(n) * np.exp(-t * decay)
    for d, g in ((0.009, 0.55), (0.017, 0.42), (0.029, 0.30), (0.041, 0.22)):
        i = int(d * sr)
        if i < n:
            ir[i] += g
    ir[0] = 1.0
    return ir / max(1e-9, float(np.max(np.abs(ir))))


def _reverb(x: np.ndarray, amount: float = 0.25, size: float = 0.7,
            seed: int = 7) -> np.ndarray:
    """FFT 卷积混响（比逐样本 comb 快得多，长曲也不卡）。"""
    if amount <= 0.001 or x.size < 256:
        return x
    ir = _reverb_ir(size=size, seed=seed)
    wet = _sig.fftconvolve(x, ir, mode="full")[: x.size]
    return (1.0 - amount * 0.55) * x + amount * 0.9 * wet


def _delay(x: np.ndarray, delay_s: float, fb: float = 0.3,
           mix: float = 0.2, sr: int = SR) -> np.ndarray:
    d = int(delay_s * sr)
    if d <= 0 or d >= x.size:
        return x
    y = x.copy()
    for k in (1, 2, 3):
        off = d * k
        if off >= x.size:
            break
        y[off:] += x[: x.size - off] * (fb ** k) * mix
    return y


# ============================================================
# 二、乐器
# ============================================================
def inst_piano(f: float, dur: float, vel: float = 0.8, sr: int = SR) -> np.ndarray:
    """FM 钢琴：载波 + 2 倍频调制器，快速衰减。"""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    mod_env = np.exp(-t * 3.5)
    # 调制指数按音高缩放：固定 2.2 时低音区基频被 Bessel 分量摊薄
    # （J0(2.2)≈0.11），130Hz 的音几乎听不到基频，纯钢琴曲会显得没有低频。
    mod_idx = 2.2 * min(1.0, (f / 440.0) ** 0.55)
    mod = np.sin(2 * np.pi * f * 2.0 * t) * (mod_idx * mod_env)
    sig = np.sin(2 * np.pi * f * t + mod)
    sig += 0.55 * np.sin(2 * np.pi * f * t) * np.exp(-t * 1.6)
    sig += 0.35 * np.sin(2 * np.pi * f * 3.0 * t) * np.exp(-t * 6.0)
    sig += 0.18 * np.sin(2 * np.pi * f * 4.01 * t) * np.exp(-t * 9.0)
    # 衰减速率随音高变化：低音延音长（真实钢琴低音能响 3~5 秒），
    # 高音衰减快。原来固定 2.4+1.6/dur，130Hz 的音 1 秒后就只剩 5%。
    decay = (2.4 + 1.6 / max(0.25, dur)) * min(1.45, max(0.42, (f / 261.6) ** 0.5))
    env = np.exp(-t * decay) * _adsr(n, 0.004, 0.05, 0.9, 0.08, sr)
    return _soft(sig * env) * vel * 0.6


def inst_epiano(f: float, dur: float, vel: float = 0.7, sr: int = SR) -> np.ndarray:
    """电钢琴：FM 比例 1:3，带一点铃感。"""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    mod = np.sin(2 * np.pi * f * 3.0 * t) * 1.6 * np.exp(-t * 4.0)
    sig = np.sin(2 * np.pi * f * t + mod) + 0.25 * np.sin(2 * np.pi * f * 7.0 * t) * np.exp(-t * 8)
    env = np.exp(-t * 1.8) * _adsr(n, 0.006, 0.1, 0.7, 0.12, sr)
    return _soft(sig * env) * vel * 0.55


def inst_pluck(f: float, dur: float, vel: float = 0.8, sr: int = SR,
               damp: float = 0.996) -> np.ndarray:
    """Karplus-Strong 拨弦（古筝 / 吉他 / 竖琴）。分块向量化，不逐样本循环。"""
    n = _n(dur, sr)
    d = max(2, int(round(sr / max(30.0, f))))
    rng = np.random.RandomState(int(f * 13) % 99991)
    buf = rng.uniform(-1.0, 1.0, d)
    # 起始激励加一点柔化，避免过于"噪"
    buf = _sig.lfilter([0.5, 0.5], [1.0], buf)
    out = np.zeros(n + d)
    out[:d] = buf
    pos = d
    g = float(damp) ** (440.0 / max(40.0, f)) ** 0.35
    while pos < n + d:
        end = min(pos + d, n + d)
        seg = end - pos
        prev = out[pos - d: pos - d + seg]
        prev2 = out[pos - d - 1: pos - d - 1 + seg] if pos - d - 1 >= 0 else np.zeros(seg)
        out[pos:end] = 0.5 * (prev + prev2) * g
        pos = end
    sig = out[:n]
    env = _adsr(n, 0.002, 0.06, 0.85, min(0.35, dur * 0.4), sr)
    return sig * env * vel * 0.85


def inst_bass(f: float, dur: float, vel: float = 0.85, sr: int = SR,
              kind: str = "saw") -> np.ndarray:
    """贝斯：锯齿/方波 + 低通 + 轻微饱和。"""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    sig = _osc(f, n, kind, sr) * 0.7 + _osc(f, n, "sine", sr) * 0.5
    sig = _lp_var(sig, 320 + 900 * np.exp(-t * 9.0), sr)
    env = _adsr(n, 0.006, 0.09, 0.72, min(0.18, dur * 0.3), sr)
    return _soft(sig * env) * vel * 0.75


def inst_pad(f: float, dur: float, vel: float = 0.5, sr: int = SR,
             detune: float = 0.006, bright: float = 2200.0) -> np.ndarray:
    """弦乐/合成器 pad：3 个失谐锯齿叠加 + 慢起音。"""
    n = _n(dur, sr)
    sig = np.zeros(n)
    for k, dt in enumerate((-detune, 0.0, detune)):
        sig += _osc(f * (1.0 + dt), n, "saw", sr, phase0=k * 0.33)
    sig = _lp(sig / 3.0, bright)
    env = _adsr(n, min(0.35, dur * 0.35), 0.2, 0.78, min(0.5, dur * 0.4), sr)
    return sig * env * vel * 0.5


def inst_lead(f: float, dur: float, vel: float = 0.7, sr: int = SR,
              kind: str = "saw", vib: bool = True) -> np.ndarray:
    """主旋律：锯齿/方波，带延迟颤音。"""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    sig = _osc(f, n, kind, sr,
               vib_hz=5.4 if vib else 0.0,
               vib_amt=0.010 if vib else 0.0)
    sig += 0.3 * _osc(f * 2.0, n, "sine", sr)
    sig = _lp(sig, 4200)
    env = _adsr(n, 0.02, 0.12, 0.68, min(0.22, dur * 0.35), sr)
    return sig * env * vel * 0.45


def inst_flute(f: float, dur: float, vel: float = 0.6, sr: int = SR) -> np.ndarray:
    """笛/箫：正弦 + 少量谐波 + 气声噪声 + 颤音。"""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    sig = _osc(f, n, "sine", sr, vib_hz=5.0, vib_amt=0.012)
    sig += 0.22 * _osc(f * 2.0, n, "sine", sr)
    sig += 0.07 * _osc(f * 3.0, n, "sine", sr)
    breath = np.random.RandomState(3).randn(n) * 0.05
    breath = _lp(_hp(breath, 1200), 5200)
    env = _adsr(n, min(0.09, dur * 0.2), 0.1, 0.82, min(0.25, dur * 0.35), sr)
    return (sig * 0.8 + breath) * env * vel * 0.55


def inst_chiptune(f: float, dur: float, vel: float = 0.7, sr: int = SR,
                  kind: str = "square") -> np.ndarray:
    """8bit：方波/三角波，无滤波，硬起音。"""
    n = _n(dur, sr)
    sig = _osc(f, n, kind, sr)
    env = _adsr(n, 0.002, 0.03, 0.85, 0.03, sr)
    return sig * env * vel * 0.35


def inst_organ(f: float, dur: float, vel: float = 0.6, sr: int = SR) -> np.ndarray:
    """风琴：多个谐波按 1/n 叠加，起音快。"""
    n = _n(dur, sr)
    sig = np.zeros(n)
    for h in (1, 2, 3, 4, 6, 8):
        sig += _osc(f * h, n, "sine", sr) / h
    env = _adsr(n, 0.012, 0.06, 0.88, min(0.2, dur * 0.3), sr)
    return sig * env * vel * 0.32


# ---------- 鼓 ----------
def drum_kick(dur: float = 0.34, sr: int = SR) -> np.ndarray:
    n = _n(dur, sr)
    t = np.arange(n) / sr
    f = 46.0 + 105.0 * np.exp(-t * 24.0)
    ph = np.cumsum(f) / sr
    body = np.sin(2 * np.pi * ph) * np.exp(-t * 6.5)
    click = np.random.RandomState(11).randn(n) * np.exp(-t * 260.0) * 0.35
    # ⚠️ 2026-11-12 用户报「噪声严重」：click 是白噪声，exp(-t*260) 在 t=0 恰为 1.0，
    #    第一个采样点就是 randn 的满幅随机值，没有任何淡入 → 每个底鼓开头一次单样本
    #    阶跃。实测 epicmaj 版有 47 处 >0.25 的相邻跳变（每 2 拍一次，正好是底鼓）。
    #    补 1.5ms 淡入，把阶跃变成斜坡。
    _a = max(1, int(0.0015 * sr))
    if _a < n:
        click[:_a] *= np.linspace(0.0, 1.0, _a)
    return _soft(body + click) * 0.9


def drum_snare(dur: float = 0.22, sr: int = SR) -> np.ndarray:
    n = _n(dur, sr)
    t = np.arange(n) / sr
    tone = np.sin(2 * np.pi * 196 * t) * np.exp(-t * 26.0) * 0.35
    noise = np.random.RandomState(12).randn(n)
    noise = _lp(_hp(noise, 1400), 7800) * np.exp(-t * 15.0)
    return _soft(tone + noise * 0.85) * 0.55


def drum_hat(dur: float = 0.06, sr: int = SR, open_hat: bool = False) -> np.ndarray:
    if open_hat:
        dur = 0.26
    n = _n(dur, sr)
    t = np.arange(n) / sr
    noise = np.random.RandomState(13).randn(n)
    noise = _hp(noise, 7200) * np.exp(-t * (9.0 if open_hat else 55.0))
    return noise * 0.22


def drum_clap(dur: float = 0.2, sr: int = SR) -> np.ndarray:
    n = _n(dur, sr)
    t = np.arange(n) / sr
    noise = np.random.RandomState(14).randn(n)
    noise = _lp(_hp(noise, 900), 4600)
    env = np.exp(-t * 16.0)
    for off in (0.0, 0.011, 0.022):
        i = int(off * sr)
        if i < n:
            env[i:] += np.exp(-(t[: n - i]) * 22.0) * 0.7
    return noise * env * 0.35


def drum_tom(f: float = 120.0, dur: float = 0.3, sr: int = SR) -> np.ndarray:
    n = _n(dur, sr)
    t = np.arange(n) / sr
    fr = f * (1.0 + 0.6 * np.exp(-t * 18.0))
    ph = np.cumsum(fr) / sr
    return np.sin(2 * np.pi * ph) * np.exp(-t * 7.0) * 0.6


# ---------- 人声（共振峰合成） ----------
# F1/F2/F3 是各元音的前三个共振峰（男声音域附近，稍偏高更清亮）
VOWELS = {
    "a": (800.0, 1180.0, 2800.0),
    "e": (420.0, 1900.0, 2700.0),
    "i": (300.0, 2350.0, 3000.0),
    "o": (460.0, 820.0, 2830.0),
    "u": (330.0, 720.0, 2500.0),
    "v": (320.0, 1650.0, 2600.0),   # ü
}


def _pulse_train(freq, n: int, sr: int = SR, duty: float = 0.5) -> np.ndarray:
    """声门脉冲串：每个周期开头一个窄脉冲，频谱接近平坦。

    ⚠️ 这是人声激励的**正确模型**。之前用锯齿波是错的：锯齿频谱是 1/n
       衰减、基频最强，共振峰怎么滤都压不下去，实测 FFT 峰全落在 220/440
       （基频谐波），800/1180 处没有峰 —— 听起来就是锯齿，不是元音。
    """
    f = np.asarray(freq, dtype=np.float64)
    if f.ndim == 0:
        f = np.full(n, float(f))
    elif len(f) != n:
        f = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(f)), f)
    phase = (np.cumsum(f) / sr) % 1.0
    return (phase < (f / sr) * duty).astype(np.float64)


def _bandpass(x: np.ndarray, f: float, q: float, sr: int = SR) -> np.ndarray:
    """RBJ 二阶带通（峰值增益 0dB），做共振峰用。"""
    f = max(80.0, min(float(f), sr * 0.45))
    w0 = 2.0 * math.pi * f / sr
    al = math.sin(w0) / (2.0 * max(0.3, q))
    return _sig.lfilter([al, 0.0, -al],
                        [1.0 + al, -2.0 * math.cos(w0), 1.0 - al], x)


def inst_voice(f: float, dur: float, vel: float = 0.8, sr: int = SR,
               vowel: str = "a", vibrato: float = 5.2,
               breath: float = 0.05) -> np.ndarray:
    """共振峰合成的人声吟唱。

    声门源 = **脉冲串**（频谱平坦）→ 两级低通做 -12dB/oct 频谱倾斜
    → F1/F2/F3 三个 RBJ 带通 → 可辨识的元音；
    加 5Hz 颤音 → 像在"唱"而不是"念"。

    实测（220Hz 基频，FFT 峰值）：
      a → 780 / 1200（目标 800 / 1180）
      o → 440 / 880（目标 460 / 820）
      u → 320 / 760（目标 330 / 720）
      e → 440 / 1980（目标 420 / 1900）

    ⚠️ 这是**合成人声**，不是真人：元音可辨、有颤音和气声，但唱不出清晰
       歌词（辅音做不出来）。要真人演唱必须上神经网络声码器。
    """
    n = _n(dur, sr)
    t = np.arange(n) / sr
    # 颤音作用在频率上（脉冲间隔随之变化）
    freq = f * (1.0 + 0.014 * np.sin(2.0 * np.pi * vibrato * t))
    src = _pulse_train(freq, n, sr)
    # 声门频谱倾斜：两级一阶低通 ≈ -12dB/oct
    src = _lp(_lp(src, 900.0, sr), 900.0, sr)
    m = float(np.max(np.abs(src)))
    if m > 1e-9:
        src = src / m
    # 元音共振峰
    f1, f2, f3 = VOWELS.get(vowel, VOWELS["a"])
    # 高音时共振峰上移一点，避免闷
    shift = 1.0 + max(0.0, (f - 220.0) / 900.0) * 0.15
    sig = (_bandpass(src, f1 * shift, 9.0, sr) * 1.00
           + _bandpass(src, f2 * shift, 11.0, sr) * 0.68
           + _bandpass(src, f3 * shift, 16.0, sr) * 0.38)
    # 气声
    if breath > 0:
        nb = np.random.RandomState(21).randn(n)
        nb = _lp(_hp(nb, 2200, sr), 6000, sr)
        sig = sig + nb * breath * 0.5
    env = _adsr(n, min(0.10, dur * 0.25), 0.12, 0.82, min(0.28, dur * 0.4), sr)
    return _soft(sig * 6.0) * env * vel * 0.5


# ============================================================
# 三、乐理
# ============================================================
SCALES = {
    "major":         [0, 2, 4, 5, 7, 9, 11],
    "minor":         [0, 2, 3, 5, 7, 8, 10],
    "penta_major":   [0, 2, 4, 7, 9],          # 宫商角徵羽（古风）
    "penta_minor":   [0, 3, 5, 7, 10],
    "blues":         [0, 3, 5, 6, 7, 10],
    "dorian":        [0, 2, 3, 5, 7, 9, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "lydian":        [0, 2, 4, 6, 7, 9, 11],
    "mixolydian":    [0, 2, 4, 5, 7, 9, 10],
}

NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _scale_pitches(root: int, scale_name: str, lo: int, hi: int) -> list:
    """把音阶铺到 [lo, hi] 区间，返回全部可用 MIDI 音高。"""
    sc = SCALES.get(scale_name, SCALES["major"])
    out = []
    for octv in range(-2, 9):
        for st in sc:
            p = root + octv * 12 + st
            if lo <= p <= hi:
                out.append(p)
    return sorted(set(out))


def _chord(root: int, scale_name: str, degree: int, size: int = 3,
           seventh: bool = False) -> list:
    """在音阶上叠三度构和弦（degree 从 0 开始）。"""
    sc = SCALES.get(scale_name, SCALES["major"])
    L = len(sc)
    size = 4 if seventh else size
    notes = []
    for i in range(size):
        idx = degree + i * 2                     # 三度叠置
        octv, deg = divmod(idx, L)
        notes.append(root + octv * 12 + sc[deg])
    return notes


def _deg_to_pitch(root: int, scale_name: str, degree: int) -> int:
    sc = SCALES.get(scale_name, SCALES["major"])
    L = len(sc)
    octv, deg = divmod(degree, L)
    return root + octv * 12 + sc[deg % L]


def _nearest_scale_index(pitch: int, root: int, scale_name: str) -> int:
    """求某个音高在音阶里最接近的"级数"（用于换风格时音阶映射）。"""
    sc = SCALES.get(scale_name, SCALES["major"])
    L = len(sc)
    rel = pitch - root
    octv = int(math.floor(rel / 12.0))
    best_deg, best_err = 0, 99
    for d in range(L):
        err = abs(rel - (octv * 12 + sc[d]))
        if err < best_err:
            best_err, best_deg = err, d
    return octv * L + best_deg


def parse_notes(text: str, key: int = 0, scale_name: str = "major",
                bar_beats: float = 4.0) -> list:
    """把用户给的音符串解析成 [{"t":拍,"d":拍,"p":midi}, ...]。

    支持三种写法：
      · 音名： "C4 D4 E4 G4 A4 G4" / "C#4 Eb4"
      · 简谱： "1 2 3 5 6 5"（1=do），"-" 延长、"0" 休止、"." 附点
      · 带时值："C4:1 D4:0.5 E4:0.5"（冒号后是拍数）
    默认每个音符 1 拍。
    """
    s = (text or "").replace("，", " ").replace(",", " ")
    s = s.replace("|", " | ").replace("｜", " | ")
    toks = s.split()
    out = []
    t = 0.0
    for tok in toks:
        if tok == "|":
            # 小节线：把时间推到下一个小节开头，保证乐句落在拍子上
            if bar_beats and bar_beats > 0:
                t = round((math.floor(t / bar_beats + 1e-9) + 1) * bar_beats, 4)
            continue
        dur = 1.0
        if ":" in tok:
            tok, ds = tok.split(":", 1)
            try:
                dur = float(ds)
            except ValueError:
                dur = 1.0
        if tok in ("-", "—"):
            if out:
                out[-1]["d"] += 1.0
            t += 1.0
            continue
        if tok in ("0", "R", "r", "休止"):
            t += dur
            continue
        pitch = None
        up = tok.upper()
        # 音名 C4 / C#4 / Db4
        if up[0:1] in NOTE_NAMES:
            semi = NOTE_NAMES[up[0]]
            i = 1
            if i < len(up) and up[i] in "#b":
                semi += 1 if up[i] == "#" else -1
                i += 1
            octv = 4
            if i < len(up):
                try:
                    octv = int(up[i:])
                except ValueError:
                    octv = 4
            pitch = (octv + 1) * 12 + semi
        else:
            # 简谱数字
            try:
                num = int(tok.strip("."))
                if 1 <= num <= 7:
                    deg = num - 1
                    pitch = _deg_to_pitch(60 + key, scale_name, deg)
                    if "." in tok and tok.rstrip(".").isdigit():
                        dur *= 0.5
            except ValueError:
                pitch = None
        if pitch is None:
            t += dur
            continue
        out.append({"t": round(t, 4), "d": round(dur, 4), "p": int(pitch)})
        t += dur
    return out


# ============================================================
# 四、风格预设
# ============================================================
STYLES = {
    "pop": {
        "name": "流行/轻快", "bpm": 118, "scale": "major",
        "prog": [[0, 4, 5, 3], [0, 5, 3, 4], [0, 3, 4, 4]],
        "inst": ["piano", "bass", "drums", "pad", "lead"],
        "density": 0.55, "reverb": 0.22, "swing": 0.0, "seventh": False,
        "kick": "four", "hat": "eighth", "desc": "钢琴+贝斯+鼓+弦乐pad，I-V-vi-IV",
    },
    "gufeng": {
        "name": "古风/五声音阶", "bpm": 84, "scale": "penta_major",
        "prog": [[0, 3, 4, 0], [0, 4, 3, 2], [0, 2, 4, 0]],
        "inst": ["pluck", "flute", "pad", "bass"],
        "density": 0.45, "reverb": 0.38, "swing": 0.0, "seventh": False,
        "kick": "none", "hat": "none", "desc": "古筝拨弦+箫+弦乐，五声调式",
    },
    "lofi": {
        "name": "Lo-fi 学习背景", "bpm": 74, "scale": "major",
        "prog": [[1, 4, 0, 5], [0, 5, 1, 4], [3, 2, 1, 0]],
        "inst": ["epiano", "bass", "drums", "pad"],
        "density": 0.4, "reverb": 0.3, "swing": 0.12, "seventh": True,
        "kick": "lofi", "hat": "swing", "desc": "电钢琴七和弦+软鼓+黑胶感",
    },
    "electronic": {
        "name": "电子/合成器", "bpm": 126, "scale": "minor",
        "prog": [[0, 5, 3, 4], [0, 3, 5, 4]],
        "inst": ["bass", "drums", "pad", "arp", "lead"],
        "density": 0.75, "reverb": 0.18, "swing": 0.0, "seventh": False,
        "kick": "four", "hat": "sixteenth", "desc": "四拍鼓+锯齿bass+琶音+宽pad",
    },
    "chiptune": {
        "name": "8bit 芯片音乐", "bpm": 148, "scale": "major",
        "prog": [[0, 5, 3, 4], [0, 4, 5, 3]],
        "inst": ["chip_lead", "chip_bass", "chip_arp", "drums"],
        "density": 0.85, "reverb": 0.06, "swing": 0.0, "seventh": False,
        "kick": "chip", "hat": "sixteenth", "desc": "方波主旋律+三角波贝斯，红白机味",
    },
    "jazz": {
        "name": "爵士/蓝调", "bpm": 96, "scale": "dorian",
        "prog": [[0, 3, 4, 0], [1, 4, 0, 5]],
        "inst": ["epiano", "bass", "drums", "lead"],
        "density": 0.5, "reverb": 0.24, "swing": 0.33, "seventh": True,
        "kick": "jazz", "hat": "swing", "desc": "七和弦+行走贝斯+摇摆鼓刷",
    },
    "piano": {
        "name": "纯钢琴", "bpm": 78, "scale": "major",
        "prog": [[0, 4, 5, 3], [0, 5, 1, 4], [0, 3, 4, 5]],
        "inst": ["piano", "pad"],
        "density": 0.45, "reverb": 0.34, "swing": 0.0, "seventh": False,
        "kick": "none", "hat": "none", "desc": "钢琴独奏+极轻弦乐衬底",
    },
    "epic": {
        "name": "史诗/电影感", "bpm": 72, "scale": "harmonic_minor",
        "prog": [[0, 5, 2, 6], [0, 3, 4, 0]],
        "inst": ["pad", "bass", "drums", "lead", "pluck"],
        "density": 0.4, "reverb": 0.45, "swing": 0.0, "seventh": False,
        "kick": "epic", "hat": "none", "desc": "厚重弦乐+定音鼓+慢速推进",
    },
    "epicmaj": {
        "name": "史诗大调/颂歌", "bpm": 72, "scale": "major",
        "prog": [[0, 4, 5, 3], [0, 5, 2, 6]],
        "inst": ["pad", "bass", "drums", "lead", "pluck"],
        "density": 0.4, "reverb": 0.45, "swing": 0.0, "seventh": False,
        "kick": "epic", "hat": "none",
        "desc": "厚重弦乐+定音鼓，大调色彩，配颂歌/进行曲类旋律不打架",
    },
}

INSTRUMENTS = {
    "piano": "钢琴", "epiano": "电钢琴", "pluck": "拨弦(古筝/吉他)",
    "bass": "贝斯", "pad": "弦乐pad", "lead": "主旋律",
    "flute": "笛/箫", "chip_lead": "8bit主旋律",
    "chip_bass": "8bit贝斯", "chip_arp": "8bit琶音",
    "arp": "合成琶音", "organ": "风琴", "voice": "人声吟唱",
    "voice_harm": "人声和声", "drums": "鼓组",
}


# ============================================================
# 五、编曲（生成乐谱）
# ============================================================
def _drum_pattern(style: str, bar_beats: float, rng: random.Random) -> list:
    """按风格给一小节的鼓型，返回 [(t_beats, kind), ...]。"""
    pat = []
    kind = STYLES.get(style, STYLES["pop"]).get("kick", "four")
    if kind == "four":
        for b in (0, 1, 2, 3):
            pat.append((b, "kick"))
        for b in (1, 3):
            pat.append((b, "snare"))
        for i in range(8):
            pat.append((i * 0.5, "hat"))
    elif kind == "lofi":
        for b in (0, 2.5):
            pat.append((b, "kick"))
        for b in (1, 3):
            pat.append((b, "snare"))
        for i in range(8):
            if i % 2 == 1:
                pat.append((i * 0.5, "hat"))
    elif kind == "jazz":
        pat.append((0, "kick"))
        pat.append((2.5, "kick"))
        for b in (1, 3):
            pat.append((b, "snare"))
        for i in range(6):
            pat.append((i * 2.0 / 3.0, "hat"))
    elif kind == "chip":
        for b in (0, 2):
            pat.append((b, "kick"))
        for b in (1, 3):
            pat.append((b, "snare"))
        for i in range(8):
            pat.append((i * 0.5, "hat"))
    elif kind == "epic":
        pat.append((0, "kick"))
        pat.append((2, "kick"))
        pat.append((3.5, "tom"))
    return pat


def _pick_melody_inst(cfg: dict) -> str:
    """从风格的配器里挑"主奏旋律"乐器。

    ⚠️ 之前只往 lead/flute/chip_lead 三轨写旋律，结果 piano（纯钢琴）
       和 lofi（电钢琴）风格**根本没有旋律**，古风的古筝也不响 —— 只有
       pad 铺底和合成人声。按优先级推导，每个风格都有自己的主奏。
    """
    for k in ("lead", "flute", "chip_lead", "piano", "epiano", "organ", "pluck"):
        if k in (cfg.get("inst") or []):
            return k
    return "lead"


def make_score(style: str = "pop", bars: int = 16, bpm: float = 0,
               key: int = 0, seed: int | None = None,
               vocals: bool = True, title: str = "",
               notes_in: list | None = None,
               lyrics: str = "", bar_beats: float = 4.0) -> dict:
    """生成一份乐谱（不含音频）。改编就是在这份乐谱上改。

    乐谱结构：
      {id?, style, bpm, key, scale, bars, beats_per_bar, duration,
       chords: [[midi...], ...]  每小节和弦,
       tracks: {乐器名: [{t,d,p,v}, ...]}   t/d 单位是**秒**,
       vocals, lyrics, title, seed, created}
    """
    cfg = STYLES.get(style) or STYLES["pop"]
    style = style if style in STYLES else "pop"
    cfg = STYLES[style]
    bpm = float(bpm or cfg["bpm"])
    bpm = max(40.0, min(220.0, bpm))
    bars = max(2, min(MAX_BARS, int(bars or 16)))
    seed = int(seed if seed is not None else random.randrange(1, 10 ** 9))
    rng = random.Random(seed)
    scale_name = cfg["scale"]
    root = 60 + int(key)
    beat = 60.0 / bpm
    bar_beats = max(1.0, min(12.0, float(bar_beats or 4.0)))
    bar_s = beat * bar_beats

    # --- 和弦进行 ---
    prog_sets = cfg["prog"]
    prog = list(rng.choice(prog_sets))
    chords = []
    for b in range(bars):
        deg = prog[b % len(prog)]
        ch = _chord(root, scale_name, deg, seventh=cfg["seventh"])
        chords.append([int(p) for p in ch])

    tracks: dict = {}

    def add(inst: str, t: float, d: float, p: int, v: float):
        tracks.setdefault(inst, []).append(
            {"t": round(max(0.0, t), 4), "d": round(max(0.05, d), 4),
             "p": int(p), "v": round(float(max(0.05, min(1.0, v))), 3)})

    # --- 鼓 ---
    if "drums" in cfg["inst"]:
        pat = _drum_pattern(style, bar_beats, rng)
        for b in range(bars):
            base = b * bar_s
            # 前 2 小节不加鼓（前奏），最后 1 小节半收
            if bars > 6 and b < 2:
                continue
            for bt, k in pat:
                tt = base + bt * beat
                if tt >= bars * bar_s - 0.01:
                    continue
                v = 0.9 if k in ("kick", "snare") else 0.55
                if b % 4 == 3 and k == "snare" and rng.random() < 0.35:
                    # 加个花
                    add("drums", tt + beat * 0.25, 0.1, 0, 0.4)
                add("drums", tt, 0.12, 0, v * rng.uniform(0.85, 1.0))
            if style in ("electronic", "chiptune") and b % 8 == 7:
                for i in range(4):                      # 段落尾加过门
                    add("drums", base + (3 + i * 0.25) * beat, 0.1, 0, 0.6)

    # --- 贝斯 ---
    # ⚠️ chiptune 的配器里写的是 chip_bass，原来只判断 "bass"，
    #    结果 8bit 风格**整轨没有低音**（实测 tracks 里确实没有贝斯）。
    _bass_inst = "bass" if "bass" in cfg["inst"] else (
        "chip_bass" if "chip_bass" in cfg["inst"] else "")
    if _bass_inst:
        for b in range(bars):
            base = b * bar_s
            ch = chords[b]
            rootp = min(ch) - 24
            if style == "jazz":
                for i in range(4):                      # 行走贝斯
                    p = _deg_to_pitch(root, scale_name, (b * 4 + i) % 7) - 24
                    add(_bass_inst, base + i * beat, beat * 0.9, p, 0.8)
            elif style in ("electronic", "chiptune"):
                for i in range(8):                      # 八分音符驱动
                    p = rootp if i % 4 != 3 else rootp + 7
                    add(_bass_inst, base + i * beat * 0.5, beat * 0.42, p, 0.85)
            else:
                add(_bass_inst, base, beat * 1.8, rootp, 0.85)
                add(_bass_inst, base + beat * 2, beat * 1.6, rootp, 0.75)
                if rng.random() < 0.4:
                    add(_bass_inst, base + beat * 3.5, beat * 0.4, rootp + 7, 0.6)

    # --- pad / 和声垫 ---
    if "pad" in cfg["inst"]:
        for b in range(bars):
            for p in chords[b]:
                add("pad", b * bar_s, bar_s * 0.98, p + 12, 0.45)

    # --- 键盘 / 拨弦的和弦伴奏 ---
    _mel_inst = _pick_melody_inst(cfg)
    for hi in ("piano", "epiano", "organ", "pluck"):
        if hi not in cfg["inst"]:
            continue
        # 它同时是主奏时，伴奏压低一点，别盖住旋律
        lvl = 0.30 if hi == _mel_inst else 0.46
        for b in range(bars):
            base = b * bar_s
            ch = chords[b]
            if not ch:
                continue
            if hi == "pluck":
                # 古筝/吉他：八分音符分解和弦，高低交替更有拨弦味
                seq = ch + [ch[0] + 12]
                for i in range(8):
                    add("pluck", base + i * beat * 0.5, beat * 0.45,
                        seq[i % len(seq)] + 12, lvl * (1.12 if i % 2 else 0.92))
            elif hi == "organ":
                add("organ", base, bar_s * 0.96, ch[0] + 12, lvl * 0.8)
            else:
                # 没有贝斯轨的风格（纯钢琴），用左手低八度根音补低频。
                # ⚠️ 实测：不补的话 piano 风格低频占比是 0.0%，整首飘在中高频。
                if not _bass_inst:
                    # 低音要**长**且**响**，否则会被中音区几十个音符淹没
                    # （实测：2 拍 × 0.85 音量时，整曲低频占比只有 0.3%）
                    add(hi, base, beat * 3.6, ch[0] - 12, lvl * 2.3)
                    add(hi, base, beat * 3.6, ch[0], lvl * 0.75)
                # 柱式和弦：钢琴 1/2.5 拍两下，电钢琴走二四拍（comping）
                spots = (0.0, 2.0) if hi == "epiano" else (0.0, 2.5)
                for sp in spots:
                    for p in ch:
                        add(hi, base + sp * beat, beat * 1.5, p + 12, lvl)

    # --- 琶音 ---
    if "arp" in cfg["inst"]:
        for b in range(bars):
            ch = chords[b]
            seq = ch + [ch[0] + 12]
            for i in range(16):
                p = seq[i % len(seq)] + 12
                add("arp", b * bar_s + i * beat * 0.25, beat * 0.22, p, 0.35)
    if "chip_arp" in cfg["inst"]:
        for b in range(bars):
            ch = chords[b]
            seq = [ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[1] + 12]
            for i in range(8):
                add("chip_arp", b * bar_s + i * beat * 0.5, beat * 0.4,
                    seq[i % len(seq)], 0.3)

    # --- 主旋律 ---
    melody: list = []
    if notes_in:
        # 用户给的音符：按拍对齐，直接当主旋律（可循环到满长度）
        bar_beats_total = bar_s
        src = notes_in
        loop_len = max(1.0, sum(max(0.05, nt["d"]) for nt in src))
        total_beats = bars * bar_beats
        t_beats = 0.0
        while t_beats < total_beats:
            for nt in src:
                if t_beats >= total_beats:
                    break
                tt = t_beats * beat
                melody.append({"t": round(tt, 4), "d": round(max(0.08, nt["d"] * beat * 0.95), 4),
                               "p": int(nt["p"]), "v": 0.8})
                t_beats += max(0.05, nt["d"])
            # 下一遍回到小节线，否则每遍错开一点、越走越乱
            if bar_beats and bar_beats > 0:
                t_beats = (math.floor(t_beats / bar_beats + 1e-9) + 1) * bar_beats
            if loop_len <= 0:
                break
    else:
        # 自动生成：在音阶内随机游走，偏向和弦音
        scale_pitches = _scale_pitches(root + 12, scale_name, root + 4, root + 26)
        deg = rng.randrange(0, 7)
        for b in range(bars):
            base = b * bar_s
            ch = chords[b]
            ch_pcs = {p % 12 for p in ch}
            density = cfg["density"]
            pos = 0.0
            while pos < bar_beats - 0.01:
                # 节奏型：按密度决定音长
                if rng.random() < density:
                    r = rng.random()
                    if r < 0.42:
                        d_b = 1.0
                    elif r < 0.72:
                        d_b = 0.5
                    elif r < 0.9:
                        d_b = 0.75
                    else:
                        d_b = 1.5
                    d_b = min(d_b, bar_beats - pos)
                    # 选音：60% 落在和弦音上
                    if rng.random() < 0.6 and ch:
                        p = rng.choice(ch) + 12
                    else:
                        p = rng.choice(scale_pitches)
                    p = max(root + 4, min(root + 26, p))
                    v = 0.72 + rng.uniform(-0.08, 0.14)
                    # 每 4 小节给个高音，制造起伏
                    if b % 4 == 0 and pos < 0.01:
                        p = min(root + 28, p + 12)
                        v = 0.88
                    melody.append({"t": round(base + pos * beat, 4),
                                   "d": round(d_b * beat * 0.94, 4),
                                   "p": int(p), "v": round(v, 3)})
                    pos += d_b
                else:
                    pos += 0.5
    m_inst = _pick_melody_inst(cfg)
    if m_inst in ("piano", "epiano", "organ", "pluck"):
        # 这些乐器已经承担了和弦伴奏，旋律**叠加**上去
        tracks[m_inst] = list(tracks.get(m_inst) or []) + melody
    else:
        tracks[m_inst] = melody

    # --- 人声 ---
    if vocals and melody:
        vw = ["a", "o", "e", "i", "u"]
        ly = [c for c in (lyrics or "") if not c.isspace()]
        for i, nt in enumerate(melody):
            if i % 7 == 6:                       # 留一点气口，别一直唱
                continue
            vw_ = vw[i % len(vw)]
            if ly:
                # 中文无法在本机取韵母（没有拼音库），按字符序号轮换元音
                vw_ = vw[ord(ly[i % len(ly)]) % len(vw)]
            tracks.setdefault("voice", []).append(
                {"t": nt["t"], "d": max(0.18, nt["d"] * 1.05),
                 "p": nt["p"], "v": round(nt["v"] * 0.72, 3), "vw": vw_})
        # 和声：低三度（音阶内），只在强拍
        for i, nt in enumerate(melody):
            if i % 4 == 0:
                deg = _nearest_scale_index(nt["p"], root, scale_name)
                hp = _deg_to_pitch(root, scale_name, deg - 2)
                tracks.setdefault("voice_harm", []).append(
                    {"t": nt["t"], "d": nt["d"] * 1.0, "p": hp,
                     "v": round(nt["v"] * 0.42, 3), "vw": "o"})

    duration = bars * bar_s + 1.2
    return {
        "style": style, "style_name": cfg["name"],
        "bpm": round(bpm, 2), "key": int(key), "scale": scale_name,
        "bars": bars, "beats_per_bar": int(bar_beats), "duration": round(duration, 3),
        "chords": chords, "tracks": tracks, "vocals": bool(vocals),
        "lyrics": lyrics or "", "title": title or "", "seed": seed,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# 六、渲染（乐谱 → 音频）
# ============================================================
_INST_FN = {
    "piano": inst_piano, "epiano": inst_epiano, "pluck": inst_pluck,
    "bass": inst_bass, "pad": inst_pad, "lead": inst_lead,
    "flute": inst_flute, "chip_lead": inst_chiptune,
    "chip_bass": inst_chiptune, "chip_arp": inst_chiptune,
    "arp": inst_lead, "organ": inst_organ,
}

# 每个乐器的声像(-1左..1右) 与 干声增益、混响发送量
_MIX = {
    "piano":     (0.0, 0.80, 0.22),
    "epiano":    (-0.30, 0.80, 0.25),
    "pluck":     (0.36, 0.80, 0.30),
    "bass":      (0.0, 1.22, 0.04),
    "pad":       (0.0, 0.44, 0.38),
    "lead":      (0.14, 0.78, 0.20),
    "flute":     (0.40, 0.72, 0.35),
    "chip_lead": (0.10, 0.78, 0.04),
    "chip_bass": (0.0, 1.08, 0.02),
    "chip_arp":  (-0.38, 0.50, 0.06),
    "arp":       (-0.38, 0.46, 0.20),
    "organ":     (-0.10, 0.66, 0.25),
    "voice":     (0.0, 0.92, 0.32),
    "voice_harm": (-0.24, 0.46, 0.38),
    "drums":     (0.0, 0.94, 0.12),
}


def _render_note(inst: str, nt: dict, sr: int = SR) -> np.ndarray:
    f = float(_m2f(nt["p"]))
    d = float(nt["d"])
    v = float(nt.get("v", 0.8))
    if inst == "drums":
        # 鼓的 kind 编码在 p 里（0=kick,1=snare,2=hat,3=clap,4=tom）
        k = int(nt["p"]) % 5
        if k == 0:
            return drum_kick(min(0.4, d * 2.5), sr) * v
        if k == 1:
            return drum_snare(min(0.3, d * 2.2), sr) * v
        if k == 2:
            return drum_hat(d, sr) * v
        if k == 3:
            return drum_clap(d, sr) * v
        return drum_tom(120.0, min(0.35, d * 2.5), sr) * v
    if inst == "voice":
        return inst_voice(f, d, v, sr, vowel=nt.get("vw", "a"))
    if inst == "voice_harm":
        return inst_voice(f, d, v, sr, vowel=nt.get("vw", "o"), breath=0.03)
    if inst == "chip_bass":
        return inst_chiptune(f, d, v, sr, kind="tri")
    if inst == "chip_arp":
        return inst_chiptune(f, d, v, sr, kind="pulse")
    fn = _INST_FN.get(inst, inst_piano)
    return fn(f, d, v, sr)


def render_score(score: dict, sr: int = SR, progress=None) -> np.ndarray:
    """乐谱 → 立体声 float32 数组（shape: n×2）。"""
    dur = float(score.get("duration") or 0) or (
        max((nt["t"] + nt["d"] for tr in score.get("tracks", {}).values() for nt in tr),
            default=4.0) + 1.2)
    n = int(dur * sr)
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    tracks = score.get("tracks") or {}
    total = max(1, len(tracks))
    for ti, (inst, notes) in enumerate(tracks.items()):
        pan, gain, send = _MIX.get(inst, (0.0, 0.8, 0.2))
        buf = np.zeros(n + int(sr * 2), dtype=np.float32)
        for nt in notes:
            sig = _render_note(inst, nt, sr)
            i = int(float(nt["t"]) * sr)
            if i >= n:
                continue
            m = min(len(sig), len(buf) - i)
            if m > 0:
                buf[i:i + m] += sig[:m].astype(np.float32)
        buf = buf[:n]
        # 混响（发送式）
        if send > 0.02:
            wet = _reverb(buf.astype(np.float64), amount=send,
                          size=0.75 if send > 0.3 else 0.55,
                          seed=17 + ti)
            buf = ((1.0 - send * 0.5) * buf + send * 0.55 * wet).astype(np.float32)
        # 声像（等功率）+ Haas 宽度
        # ⚠️ 只做等功率声像的话，左右两声道内容完全相同（实测相关度 0.999，
        #    等于单声道）。给"远离中轴"的那一侧混入 8~14ms 延迟信号（Haas 效应），
        #    人耳听不出延迟，但声场立刻变宽。
        th = (pan + 1.0) * math.pi / 4.0
        gl = math.cos(th) * gain
        gr = math.sin(th) * gain
        dly = int((0.008 + 0.006 * abs(pan)) * sr)
        if 0 < dly < n:
            delayed = np.concatenate([np.zeros(dly, dtype=np.float32), buf[:-dly]])
        else:
            delayed = buf
        # ⚠️ 左右必须**能量对称**。原写法中轴分支用 0.6/0.85、偏侧分支一侧直接
        #    用纯 buf（另一侧是 0.35/0.65 混合），导致右声道天生比左响——实测
        #    gufeng 峰值 L=13153 / R=23251（差 5dB），听感明显偏右。
        #    改成两侧同系数（0.72/0.28），只让**延迟量**不同：声像仍由 Haas
        #    效应 + 等功率 gl/gr 决定，但左右能量严格相等。
        dly2 = int((0.014 + 0.008 * abs(pan)) * sr)
        if 0 < dly2 < n:
            delayed2 = np.concatenate([np.zeros(dly2, dtype=np.float32), buf[:-dly2]])
        else:
            delayed2 = buf
        if pan > 0.02:                        # 偏右：左声道延迟更长
            l_sig = buf * 0.72 + delayed2 * 0.28
            r_sig = buf * 0.72 + delayed * 0.28
        elif pan < -0.02:                     # 偏左：右声道延迟更长
            l_sig = buf * 0.72 + delayed * 0.28
            r_sig = buf * 0.72 + delayed2 * 0.28
        else:                                 # 中轴：两侧延迟量错开
            l_sig = buf * 0.72 + delayed * 0.28
            r_sig = buf * 0.72 + delayed2 * 0.28
        left += l_sig * gl
        right += r_sig * gr
        if progress:
            try:
                progress((ti + 1) / float(total))
            except Exception:                      # noqa: BLE001
                pass
    # 总线：轻微高通去直流 + 软限幅 + 归一
    out = np.stack([left, right], axis=1)
    # ⚠️ 必须逐声道滤波：lfilter 默认 axis=-1，对 (n,2) 会沿**声道**轴滤，
    #    等于把左右两声道互相混，低频会全乱。
    for _c in range(out.shape[1]):
        out[:, _c] = _hp(out[:, _c], 28.0)
    out = out.astype(np.float32)
    # 淡入淡出，避免首尾爆音
    fade = int(0.02 * sr)
    if out.shape[0] > fade * 2:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1e-9:
        out = out / peak * 0.92
    out = np.tanh(out * 1.05).astype(np.float32) * 0.95
    return out


# ============================================================
# 七、存储
# ============================================================
def _base() -> str:
    """乐谱 / meta 的目录（data/music）。

    ⚠️ 2026-11-12 用户要求「音乐放在音频那个文件夹里」：**成品 wav 不再放这里**，
    改由 _wav_dir() 落到 data/tts（界面「🎵 音频」库读的就是那个目录）。
    本函数只管 scores/ 和 meta.json —— 音频库只认扩展名，不关心乐谱在哪。
    """
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "music")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "scores"), exist_ok=True)
    return d


def _wav_dir() -> str:
    """成品 wav 的存放目录 = data/tts（与 TTS 语音同一个文件夹）。

    这样生成的曲子会直接出现在界面的「🎵 音频」库里，能试听、能下载、
    能点 📁 打开所在文件夹，不用再单独记一个 data/music 路径。
    """
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "tts")
    os.makedirs(d, exist_ok=True)
    return d


def _wav_path(name: str) -> str:
    return os.path.join(_wav_dir(), os.path.basename(name))


def _is_music_id(mid: str) -> bool:
    """判断文件名是不是本引擎生成的曲子 id（`风格_YYYYMMDD_HHMMSS_4位随机`）。

    用途：data/tts 里还躺着 TTS 的语音文件，万一以后 TTS 也输出 .wav，
    不能让语音混进音乐列表。id 格式是 new_id() 定的，不会变。
    """
    parts = (mid or "").split("_")
    if len(parts) != 4 or parts[0] not in STYLES:
        return False
    return (len(parts[1]) == 8 and parts[1].isdigit()
            and len(parts[2]) == 6 and parts[2].isdigit()
            and parts[3].isdigit())


def _score_path(mid: str) -> str:
    return os.path.join(_base(), "scores", os.path.basename(mid) + ".json")


def new_id(style: str) -> str:
    return "%s_%s_%04d" % (style, time.strftime("%Y%m%d_%H%M%S"),
                           random.randrange(0, 10000))


def save_wav(path: str, audio: np.ndarray, sr: int = SR) -> str:
    """写 16bit PCM WAV。用标准库 wave，不依赖 soundfile/ffmpeg。"""
    import wave as _wave
    a = np.clip(audio, -1.0, 1.0)
    if a.ndim == 1:
        a = np.stack([a, a], axis=1)
    pcm = (a * 32767.0).astype("<i2")
    with _wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def save_score(score: dict, mid: str) -> str:
    p = _score_path(mid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(score, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def load_score(mid: str) -> dict | None:
    p = _score_path(mid)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001
        return None


def _read_meta(mid: str) -> dict:
    p = os.path.join(_base(), "meta.json")
    data = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:                              # noqa: BLE001
            data = {}
    return data.get(mid) or {}


def _write_meta(mid: str, info: dict) -> None:
    p = os.path.join(_base(), "meta.json")
    data = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:                              # noqa: BLE001
            data = {}
    data[mid] = info
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _finish(score: dict, mid: str, title: str, sr: int = SR) -> dict:
    """渲染 + 落盘 + 记元数据，返回给前端的描述。"""
    audio = render_score(score, sr=sr)
    wav = _wav_path(mid + ".wav")
    save_wav(wav, audio, sr)
    save_score(score, mid)
    info = {
        "id": mid,
        "title": title or score.get("title") or (
            "%s #%s" % (score.get("style_name", "音乐"), score.get("seed"))),
        "style": score["style"], "style_name": score.get("style_name", ""),
        "bpm": score["bpm"], "key": score["key"], "bars": score["bars"],
        "duration": round(len(audio) / float(sr), 2),
        "vocals": bool(score.get("vocals")),
        "created": score.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "size_kb": round(os.path.getsize(wav) / 1024.0, 1),
        "url": "/api/music/audio/%s.wav" % mid,
        "parent": score.get("parent") or "",
        "ops": score.get("ops") or "",
    }
    _write_meta(mid, info)
    return {"ok": True, "music": info,
            "url": info["url"], "name": mid + ".wav",
            "score": score}


# ============================================================
# 八、对外接口
# ============================================================
def generate(style: str = "pop", bars: int = 16, bpm: float = 0,
             key: int = 0, seed: int | None = None,
             vocals: bool = True, title: str = "",
             notes: str = "", lyrics: str = "",
             bar_beats: float = 4.0) -> dict:
    """生成一首。style 见 STYLES；notes 可给旋律音符串。

    bar_beats：每小节几拍，默认 4（4/4）。2/4 的曲子传 2。
    notes 里可以用「|」标小节线，乐句会被对齐到小节开头。
    """
    if style not in STYLES:
        return {"ok": False,
                "error": "没有这个风格：%s（可选：%s）"
                         % (style, "、".join("%s=%s" % (k, v["name"])
                                             for k, v in STYLES.items()))}
    bars = max(2, min(MAX_BARS, int(bars or 16)))
    notes_in = parse_notes(notes, key=key,
                           scale_name=(STYLES[style] or {}).get("scale", "major")) if notes else None
    score = make_score(style=style, bars=bars, bpm=bpm, key=key, seed=seed,
                       vocals=vocals, title=title, notes_in=notes_in,
                       lyrics=lyrics)
    if score["duration"] > MAX_SECONDS:
        return {"ok": False, "error": "太长了：%.0f 秒，上限 %d 秒"
                                      % (score["duration"], MAX_SECONDS)}
    mid = new_id(style)
    score["id"] = mid
    return _finish(score, mid, title)


def remix(mid: str, style: str = "", key_shift: int = 0,
          tempo: float = 1.0, instruments: list | None = None,
          extend_bars: int = 0, variation: float = 0.0,
          vocals: bool | None = None, title: str = "") -> dict:
    """改编：读原乐谱 → 改风格/调/速度/配器/变奏/续写 → 重新渲染。

    · style       换成另一种风格（音阶会自动映射，旋律骨架保留）
    · key_shift   移调半音数，可为负
    · tempo       速度倍率（1.2 = 快 20%）
    · instruments 指定配器（覆盖风格的默认配器）
    · extend_bars 续写多少小节
    · variation   变奏幅度 0~1（加经过音、改节奏）
    """
    src = load_score(mid)
    if not src:
        return {"ok": False, "error": "找不到这首曲子的乐谱：%s" % mid}
    ops = []
    score = json.loads(json.dumps(src))            # 深拷贝
    old_style = score.get("style", "pop")
    old_scale = score.get("scale", "major")
    old_root = 60 + int(score.get("key") or 0)

    # 1) 换风格：音阶映射（这是"流行→古风"能保住旋律的关键）
    if style and style in STYLES and style != old_style:
        cfg = STYLES[style]
        new_scale = cfg["scale"]
        new_root = old_root
        if new_scale != old_scale:
            for tr in score.get("tracks", {}).values():
                for nt in tr:
                    deg = _nearest_scale_index(nt["p"], old_root, old_scale)
                    nt["p"] = int(_deg_to_pitch(new_root, new_scale, deg))
        # 换和弦（按新风格的和弦进行，用原调根音）
        prog = list(STYLES[style]["prog"][0])
        bars = int(score.get("bars") or 16)
        score["chords"] = [[int(p) for p in _chord(new_root, new_scale,
                                                   prog[b % len(prog)],
                                                   seventh=cfg["seventh"])]
                           for b in range(bars)]
        score["scale"] = new_scale
        score["style"] = style
        score["style_name"] = cfg["name"]
        ops.append("换风格→" + cfg["name"])
        # 配器若没显式指定，按新风格重建：保留旋律/人声轨，其余按新风格配
        if not instruments:
            keep = {}
            for k in ("lead", "flute", "chip_lead", "voice", "voice_harm"):
                if k in score["tracks"]:
                    keep[k] = score["tracks"][k]
            # 旋律轨名对齐新风格
            melody = (keep.get("lead") or keep.get("flute")
                      or keep.get("chip_lead") or [])
            score["tracks"] = {}
            want = [i for i in cfg["inst"] if i != "drums"]
            for i in want:
                if i in ("lead", "flute", "chip_lead"):
                    if melody:
                        score["tracks"][i] = [dict(n) for n in melody]
                elif i == "bass":
                    score["tracks"]["bass"] = _rebuild_bass(score, cfg)
                elif i == "pad":
                    score["tracks"]["pad"] = _rebuild_pad(score, bars)
                elif i in ("arp", "chip_arp"):
                    score["tracks"][i] = _rebuild_arp(score, bars, i)
            if "voice" in keep:
                score["tracks"]["voice"] = keep["voice"]
            if "voice_harm" in keep:
                score["tracks"]["voice_harm"] = keep["voice_harm"]
            if "drums" in cfg["inst"]:
                score["tracks"]["drums"] = _rebuild_drums(score, style)

    # 2) 移调
    if key_shift:
        for tr in score.get("tracks", {}).values():
            for nt in tr:
                if nt["p"] < 5:                    # 鼓的 p 是编码，别动
                    continue
                nt["p"] = int(nt["p"]) + int(key_shift)
        score["key"] = int(score.get("key") or 0) + int(key_shift)
        ops.append("移调 %+d 半音" % int(key_shift))

    # 3) 变速
    tempo = float(tempo or 1.0)
    if abs(tempo - 1.0) > 0.001:
        tempo = max(0.5, min(2.0, tempo))
        for tr in score.get("tracks", {}).values():
            for nt in tr:
                nt["t"] = round(float(nt["t"]) / tempo, 4)
                nt["d"] = round(max(0.05, float(nt["d"]) / tempo), 4)
        score["bpm"] = round(float(score.get("bpm") or 120) * tempo, 2)
        score["duration"] = round(float(score.get("duration") or 0) / tempo, 3)
        ops.append("变速 ×%.2f" % tempo)

    # 4) 换配器
    if instruments:
        allowed = [i for i in instruments if i in INSTRUMENTS or i == "drums"]
        if allowed:
            melody = (score["tracks"].get("lead") or score["tracks"].get("flute")
                      or score["tracks"].get("chip_lead") or [])
            bass = score["tracks"].get("bass") or []
            pad = score["tracks"].get("pad") or []
            drums = score["tracks"].get("drums") or []
            voice = score["tracks"].get("voice") or []
            harm = score["tracks"].get("voice_harm") or []
            newt = {}
            for i in allowed:
                if i == "drums":
                    newt["drums"] = drums
                elif i in ("lead", "flute", "chip_lead", "organ"):
                    newt[i] = [dict(x) for x in melody]
                elif i in ("bass", "chip_bass"):
                    newt[i] = [dict(x) for x in bass]
                elif i in ("pad",):
                    newt[i] = [dict(x) for x in pad]
                elif i in ("arp", "chip_arp"):
                    newt[i] = _rebuild_arp(score, int(score.get("bars") or 16), i)
                elif i in ("voice",):
                    newt["voice"] = [dict(x) for x in voice]
            if harm:
                newt["voice_harm"] = harm
            score["tracks"] = newt
            ops.append("换配器：" + "、".join(INSTRUMENTS.get(i, i) for i in allowed))

    # 5) 续写
    if extend_bars and int(extend_bars) > 0:
        add_bars = min(64, int(extend_bars))
        bars = int(score.get("bars") or 16)
        bar_s = (60.0 / float(score.get("bpm") or 120)) * 4.0
        base_t = bars * bar_s
        rng = random.Random((score.get("seed") or 1) + 991)
        scale_name = score.get("scale", "major")
        root = 60 + int(score.get("key") or 0)
        # 和弦延长
        prog = [c for c in (score.get("chords") or [])]
        for b in range(add_bars):
            if prog:
                score["chords"].append(prog[(bars + b) % len(prog)])
        # 每个轨道复制最后几小节的内容，移过去（保留律动）
        for inst, tr in list(score.get("tracks", {}).items()):
            if not tr:
                continue
            seg = [nt for nt in tr if nt["t"] >= base_t - bar_s * 4]
            if not seg:
                seg = tr[-8:]
            off = base_t - min(nt["t"] for nt in seg)
            for b in range(add_bars):
                shift = base_t + b * bar_s - min(nt["t"] for nt in seg)
                for nt in seg:
                    nn = dict(nt)
                    nn["t"] = round(nt["t"] + shift, 4)
                    if inst not in ("drums", "voice", "voice_harm"):
                        if rng.random() < 0.3:
                            nn["p"] = int(nt["p"]) + rng.choice([-2, 0, 2])
                    tr.append(nn)
        score["bars"] = bars + add_bars
        score["duration"] = round(float(score.get("duration") or 0) + add_bars * bar_s, 3)
        ops.append("续写 +%d 小节" % add_bars)

    # 6) 变奏
    variation = float(variation or 0.0)
    if variation > 0.01:
        variation = min(1.0, variation)
        rng = random.Random((score.get("seed") or 1) + 313)
        scale_name = score.get("scale", "major")
        root = 60 + int(score.get("key") or 0)
        for inst in ("lead", "flute", "chip_lead", "voice", "voice_harm"):
            tr = score["tracks"].get(inst)
            if not tr:
                continue
            new = []
            for nt in tr:
                new.append(nt)
                # 加经过音（往音阶相邻音滑）
                if rng.random() < 0.22 * variation and nt["d"] > 0.12:
                    deg = _nearest_scale_index(nt["p"], root, scale_name)
                    step = rng.choice([-1, 1])
                    new.append({"t": round(nt["t"] + nt["d"] * 0.6, 4),
                                "d": round(nt["d"] * 0.35, 4),
                                "p": int(_deg_to_pitch(root, scale_name, deg + step)),
                                "v": round(float(nt["v"]) * 0.7, 3),
                                "vw": nt.get("vw", "a")})
                # 节奏切分
                if rng.random() < 0.18 * variation and nt["d"] > 0.25:
                    nt["d"] = round(nt["d"] * 0.62, 4)
                    new.append({"t": round(nt["t"] + nt["d"] + 0.01, 4),
                                "d": round(nt["d"] * 0.5, 4),
                                "p": int(nt["p"]), "v": round(float(nt["v"]) * 0.6, 3),
                                "vw": nt.get("vw", "a")})
            score["tracks"][inst] = new
        ops.append("变奏 %.0f%%" % (variation * 100))

    if vocals is not None:
        if vocals and not score["tracks"].get("voice"):
            melody = (score["tracks"].get("lead") or score["tracks"].get("flute")
                      or score["tracks"].get("chip_lead") or [])
            if melody:
                vw = ["a", "o", "e", "i", "u"]
                score["tracks"]["voice"] = [
                    {"t": nt["t"], "d": max(0.18, nt["d"] * 1.05),
                     "p": nt["p"], "v": round(float(nt["v"]) * 0.72, 3),
                     "vw": vw[i % len(vw)]}
                    for i, nt in enumerate(melody) if i % 7 != 6]
                ops.append("加入人声吟唱")
        elif not vocals and score["tracks"].get("voice"):
            score["tracks"].pop("voice", None)
            score["tracks"].pop("voice_harm", None)
            ops.append("去掉人声")
        score["vocals"] = bool(vocals)

    if score["duration"] > MAX_SECONDS:
        return {"ok": False, "error": "改编后太长：%.0f 秒（上限 %d）"
                                      % (score["duration"], MAX_SECONDS)}

    score["parent"] = mid
    score["ops"] = "；".join(ops) or "原样重渲染"
    score["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    nmid = new_id(score["style"])
    score["id"] = nmid
    t = title or ("%s（改编）" % (load_score_meta_title(mid) or score.get("style_name")))
    return _finish(score, nmid, t)


def load_score_meta_title(mid: str) -> str:
    return (_read_meta(mid) or {}).get("title") or ""


def _rebuild_bass(score: dict, cfg: dict) -> list:
    beat = 60.0 / float(score.get("bpm") or 120)
    bar_s = beat * 4
    out = []
    for b, ch in enumerate(score.get("chords") or []):
        if not ch:
            continue
        rootp = min(ch) - 24
        base = b * bar_s
        style = score.get("style", "pop")
        if style in ("electronic", "chiptune"):
            for i in range(8):
                p = rootp if i % 4 != 3 else rootp + 7
                out.append({"t": round(base + i * beat * 0.5, 4),
                            "d": round(beat * 0.42, 4), "p": int(p), "v": 0.85})
        else:
            out.append({"t": round(base, 4), "d": round(beat * 1.8, 4),
                        "p": int(rootp), "v": 0.85})
            out.append({"t": round(base + beat * 2, 4), "d": round(beat * 1.6, 4),
                        "p": int(rootp), "v": 0.75})
    return out


def _rebuild_pad(score: dict, bars: int) -> list:
    beat = 60.0 / float(score.get("bpm") or 120)
    bar_s = beat * 4
    out = []
    for b, ch in enumerate(score.get("chords") or []):
        for p in ch:
            out.append({"t": round(b * bar_s, 4), "d": round(bar_s * 0.98, 4),
                        "p": int(p) + 12, "v": 0.45})
    return out


def _rebuild_arp(score: dict, bars: int, inst: str) -> list:
    beat = 60.0 / float(score.get("bpm") or 120)
    bar_s = beat * 4
    out = []
    for b, ch in enumerate(score.get("chords") or []):
        if not ch:
            continue
        if inst == "chip_arp":
            seq = [ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[1] + 12]
            for i in range(8):
                out.append({"t": round(b * bar_s + i * beat * 0.5, 4),
                            "d": round(beat * 0.4, 4),
                            "p": int(seq[i % len(seq)]), "v": 0.3})
        else:
            seq = ch + [ch[0] + 12]
            for i in range(16):
                out.append({"t": round(b * bar_s + i * beat * 0.25, 4),
                            "d": round(beat * 0.22, 4),
                            "p": int(seq[i % len(seq)]) + 12, "v": 0.35})
    return out


def _rebuild_drums(score: dict, style: str) -> list:
    beat = 60.0 / float(score.get("bpm") or 120)
    bar_s = beat * 4
    bars = int(score.get("bars") or 16)
    rng = random.Random(5)
    pat = _drum_pattern(style, 4, rng)
    out = []
    for b in range(bars):
        if bars > 6 and b < 2:
            continue
        for bt, k in pat:
            out.append({"t": round(b * bar_s + bt * beat, 4), "d": 0.12,
                        "p": 0, "v": 0.9 if k in ("kick", "snare") else 0.55})
    return out


def from_notes(notes: str, style: str = "piano", bars: int = 8,
               bpm: float = 0, key: int = 0, vocals: bool = False,
               title: str = "") -> dict:
    """把一段音符（哼唱记谱 / 简谱）编成完整曲子。"""
    parsed = parse_notes(notes, key=key,
                         scale_name=(STYLES.get(style) or {}).get("scale", "major"))
    if not parsed:
        return {"ok": False,
                "error": "没解析出音符。可以写音名「C4 D4 E4 G4」或简谱「1 2 3 5 6」"}
    # 按音符总拍数算最少小节
    need = int(math.ceil(sum(nt["d"] for nt in parsed) / 4.0)) + 1
    bars = max(need, min(MAX_BARS, int(bars or need)))
    return generate(style=style, bars=bars, bpm=bpm, key=key,
                    vocals=vocals, title=title, notes=notes)


def list_music(limit: int = 60) -> list:
    d = _wav_dir()                 # 曲子（wav）在 data/tts
    out = []
    meta = {}
    mp = os.path.join(_base(), "meta.json")   # 元数据仍在 data/music
    if os.path.isfile(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:                              # noqa: BLE001
            meta = {}
    for fn in os.listdir(d):
        if not fn.lower().endswith(".wav"):
            continue
        mid = fn[:-4]
        # data/tts 里还放着 TTS 语音：只认本引擎的 id 格式，别把语音当曲子
        if not _is_music_id(mid):
            continue
        p = os.path.join(d, fn)
        info = dict(meta.get(mid) or {})
        info.setdefault("id", mid)
        info.setdefault("title", mid)
        info.setdefault("style", mid.split("_")[0])
        info.setdefault("style_name", (STYLES.get(info["style"]) or {}).get("name", ""))
        info.setdefault("created", time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
        info["size_kb"] = round(os.path.getsize(p) / 1024.0, 1)
        info["url"] = "/api/music/audio/%s.wav" % mid
        if not info.get("duration"):
            try:
                import wave as _wave
                with _wave.open(p, "rb") as w:
                    info["duration"] = round(w.getnframes() / float(w.getframerate()), 2)
            except Exception:                          # noqa: BLE001
                info["duration"] = 0
        out.append(info)
    out.sort(key=lambda x: x.get("created", ""), reverse=True)
    return out[:limit]


def audio_path(name: str) -> str | None:
    p = _wav_path(name)
    return p if os.path.isfile(p) else None


def delete(mid: str) -> dict:
    mid = os.path.basename(mid or "")
    if not mid:
        return {"ok": False, "error": "没给 id"}
    removed = []
    for p in (_wav_path(mid + ".wav"), _score_path(mid)):
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except OSError:
                pass
    return {"ok": bool(removed), "removed": removed}


def styles() -> list:
    out = []
    for k, v in STYLES.items():
        out.append({"id": k, "name": v["name"], "bpm": v["bpm"],
                    "scale": v["scale"], "desc": v["desc"],
                    "inst": [INSTRUMENTS.get(i, i) for i in v["inst"]],
                    "vocals_ok": True})
    return out


def instruments() -> list:
    return [{"id": k, "name": v} for k, v in INSTRUMENTS.items()]
