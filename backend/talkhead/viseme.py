"""音素 → 6 维口型参数（含协同发音）

## 为什么要 6 维

旧链路只有 `openness` 和 `width` 两个数。但真人发音的口型至少是六维的：

    open    开口度     —— 上下唇分开多少（a 最大，b/m 为 0）
    round   圆唇度     —— 嘴唇撮圆（u / ü / o 最圆）
    spread  展唇度     —— 嘴角向两边拉（i / e / a 扁）
    teeth   上齿露出   —— 只有张嘴到一定程度才看得见牙
    lower   下唇下沉   —— 说话时下唇动作比上唇大
    press   闭唇压力   —— b / p / m 的完全闭合

「乌」= open 0.20 + round 1.00，「衣」= open 0.24 + spread 1.00。
在 2 维空间里这两个会被压成几乎一样的值 —— 这就是「嘴形不像在说话」。

## 协同发音（coarticulation）为什么必须有

人发「爸」时，b 的闭合不是在 b 段结束时突然松开，而是**提前**就开始向 a 过渡；
发「安」时，n 的舌尖动作在 a 段后段就已经启动。
如果把每个音素的目标值直接按段贴上去，嘴会在段边界处**跳变**，观感就是抽搐。

做法：每个音素段在自己的时间窗内产生一条高斯影响曲线，所有段加权叠加。
核宽与段时长挂钩（短段窄、长段宽），这样闭塞音的闭合不会被相邻元音稀释掉。

## press 为什么单独用 max 而不是加权平均

b/p/m 的闭合只有 30~50ms，如果和相邻的 a（open 0.88）做加权平均，
闭合会被平均成 0.5 —— 嘴半张着，双唇音就废了。
所以 press 用「取所有段的最大影响」：只要当前时刻有闭塞音在影响窗内，就压住。
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np

from .phoneme import Seg

logger = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------- 音素 → 口型

# (open, round, spread, teeth, lower, press)
# 数值依据：开口度按国际音标元音图的舌位高度与开口度换算；
# 圆唇度按唇形圆展；teeth 按「这个音正常发出时上齿是否可见」。
_PHONE: Dict[str, tuple] = {
    # ---- 单韵母 ----
    "a":  (0.88, 0.00, 0.85, 0.75, 0.55, 0.00),
    "o":  (0.58, 0.82, 0.00, 0.30, 0.40, 0.00),
    "e":  (0.50, 0.12, 0.55, 0.55, 0.35, 0.00),
    "ê":  (0.58, 0.00, 0.70, 0.60, 0.40, 0.00),
    "i":  (0.24, 0.00, 1.00, 0.55, 0.15, 0.00),
    "u":  (0.20, 1.00, 0.00, 0.00, 0.10, 0.00),
    "ü":  (0.18, 1.00, 0.00, 0.00, 0.08, 0.00),
    "-i": (0.14, 0.10, 0.80, 0.30, 0.05, 0.00),
    # ---- 鼻韵尾 ----
    "n":  (0.22, 0.05, 0.40, 0.15, 0.12, 0.00),
    "ng": (0.30, 0.15, 0.30, 0.10, 0.15, 0.00),
    "r":  (0.35, 0.30, 0.20, 0.05, 0.20, 0.00),
    # ---- 声母：闭塞音（完全闭合）----
    "b":  (0.02, 0.20, 0.00, 0.00, 0.00, 1.00),
    "p":  (0.02, 0.20, 0.00, 0.00, 0.00, 1.00),
    "m":  (0.02, 0.22, 0.00, 0.00, 0.00, 1.00),
    # ---- 声母：唇齿 ----
    "f":  (0.10, 0.05, 0.30, 0.30, 0.05, 0.25),
    # ---- 声母：舌尖 ----
    "d":  (0.16, 0.02, 0.45, 0.25, 0.10, 0.15),
    "t":  (0.18, 0.02, 0.45, 0.28, 0.12, 0.10),
    "n_": (0.16, 0.05, 0.40, 0.15, 0.10, 0.00),
    "l":  (0.18, 0.05, 0.45, 0.20, 0.12, 0.00),
    # ---- 声母：舌尖前（平舌）----
    "z":  (0.10, 0.05, 0.75, 0.35, 0.04, 0.10),
    "c":  (0.12, 0.05, 0.75, 0.38, 0.05, 0.05),
    "s":  (0.12, 0.03, 0.78, 0.38, 0.05, 0.00),
    # ---- 声母：卷舌 ----
    "zh": (0.18, 0.20, 0.50, 0.20, 0.12, 0.10),
    "ch": (0.20, 0.22, 0.50, 0.22, 0.14, 0.05),
    "sh": (0.20, 0.24, 0.45, 0.15, 0.14, 0.00),
    "r_": (0.20, 0.25, 0.30, 0.05, 0.18, 0.00),
    # ---- 声母：舌面 ----
    "j":  (0.16, 0.02, 0.65, 0.30, 0.10, 0.10),
    "q":  (0.18, 0.02, 0.65, 0.32, 0.12, 0.05),
    "x":  (0.18, 0.02, 0.68, 0.32, 0.12, 0.00),
    # ---- 声母：舌根 ----
    "g":  (0.20, 0.05, 0.30, 0.10, 0.15, 0.15),
    "k":  (0.22, 0.05, 0.30, 0.12, 0.16, 0.10),
    "h":  (0.24, 0.08, 0.28, 0.08, 0.16, 0.00),
    # ---- 停顿 ----
    "-":  (0.02, 0.05, 0.05, 0.00, 0.00, 0.35),
}

DIMS = ("open", "round", "spread", "teeth", "lower", "press")

# 各维度的平滑核宽（秒）——发音器官的惯性不同
_KERNEL = {
    "open":   (0.030, 0.110),   # (最小, 最大)
    "round":  (0.035, 0.100),
    "spread": (0.035, 0.100),
    "teeth":  (0.030, 0.090),
    "lower":  (0.030, 0.100),
    "press":  (0.028, 0.070),   # 闭塞音的闭合要锐利
}

# 攻击/释放：张嘴比闭嘴快（真人如此）
_ATTACK = 0.65      # 上升段压缩到 65%（更快到达）
_RELEASE = 1.35     # 下降段拉长到 135%（更慢离开）


def phone_params(phone: str) -> tuple:
    """单个音素的 6 维目标值。未知音素返回中性（半开）。"""
    if phone in _PHONE:
        return _PHONE[phone]
    # 声母表里 n / r 有两种写法，统一一下
    p = phone.rstrip("_")
    if p in _PHONE:
        return _PHONE[p]
    return (0.30, 0.10, 0.40, 0.20, 0.15, 0.00)


def track(segs: List[Seg], duration: float, fps: int = 25) -> Dict[str, np.ndarray]:
    """音素段 → 每帧的 6 维参数（长度为帧数）。

    返回 {"open": arr, "round": arr, ...}，每个 arr 是 float32，值域 0~1。
    """
    n = max(1, int(round(duration * fps)) + 1)
    times = np.arange(n, dtype=np.float64) / float(fps)
    out = {d: np.zeros(n, dtype=np.float32) for d in DIMS}
    if not segs:
        out["press"][:] = 0.35
        return out

    wsum = np.zeros(n, dtype=np.float64)
    for dim in DIMS:
        if dim == "press":
            continue
        acc = np.zeros(n, dtype=np.float64)
        wsum_d = np.zeros(n, dtype=np.float64)
        for s in segs:
            tgt = phone_params(s.phone)
            idx = DIMS.index(dim)
            val = float(tgt[idx])
            if val <= 0.0:
                continue
            d = max(1e-3, s.dur)
            lo, hi = _KERNEL[dim]
            sig = float(np.clip(d * 0.9, lo, hi))
            c = 0.5 * (s.t0 + s.t1)
            w = np.exp(-((times - c) / sig) ** 2)
            # 段内时间弯曲：在段中心附近权重最高，边缘衰减
            acc += w * val
            wsum_d += w
        # 归一化（没有影响的地方保持 0）
        safe = np.maximum(wsum_d, 1e-9)
        out[dim] = np.clip(acc / safe, 0.0, 1.0).astype(np.float32)

    # press：取最大影响，保住闭塞音
    for s in segs:
        tgt = phone_params(s.phone)
        val = float(tgt[5])
        if val <= 0.0:
            continue
        d = max(1e-3, s.dur)
        sig = float(np.clip(d * 0.85, *_KERNEL["press"]))
        c = 0.5 * (s.t0 + s.t1)
        w = np.exp(-((times - c) / sig) ** 2)
        contrib = (w * val).astype(np.float32)
        out["press"] = np.maximum(out["press"], contrib)
    out["press"] = np.clip(out["press"], 0.0, 1.0)

    # 首尾强制闭合（说话前/说完后嘴是闭的），用 120ms 过渡
    # ⚠ 必须连 round/spread 一起收 —— 实测首帧 press=0.71 却 spread=0.85，
    #   闭嘴却嘴角外展，观感是「抿着嘴笑」。
    # ⚠ ramp 必须从 0 开始：写成 linspace(1,0) 时首帧系数是 1.0，
    #   等于没衰减 —— 实测首帧 open=0.08 且 spread=0.81，一上来就咧着嘴。
    edge = max(1, int(0.12 * fps))
    ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
    for dim in ("open", "teeth", "lower", "spread", "round"):
        out[dim][:edge] *= ramp
        out[dim][-edge:] *= ramp[::-1]
    out["press"][:edge] = np.maximum(out["press"][:edge],
                                     np.linspace(0.30, 0.0, edge, dtype=np.float32))
    out["press"][-edge:] = np.maximum(out["press"][-edge:],
                                      np.linspace(0.0, 0.30, edge, dtype=np.float32))

    # ---- 物理约束（三条，都是实测参数曲线暴露出来的）----
    # 1) 闭唇压力与开口互斥：press 高的时候不能还张着嘴
    out["open"] = (out["open"] * (1.0 - 0.92 * out["press"])).astype(np.float32)

    # 2) 露齿必须由开口门控。实测 t=3.20 处 open=0.02 而 teeth=0.39 ——
    #    嘴闭着却露牙，是最刺眼的一种假。齿只在开口超过 12% 后才出现，
    #    到 40% 开口才完全可见。
    gate = np.clip((out["open"] - 0.12) / 0.28, 0.0, 1.0)
    gate = (gate * gate * (3.0 - 2.0 * gate)).astype(np.float32)
    out["teeth"] = (out["teeth"] * gate * (1.0 - out["press"])).astype(np.float32)

    # 3) 圆唇与展唇互斥。实测 t=2.00 处 round=1.00 且 spread=0.84 ——
    #    加权平均把 u 的撮口和 a 的展唇混在了一起，物理上不可能同时存在。
    #    两者和超过 1 时按比例压回，保持相对强度。
    s = out["round"] + out["spread"]
    over = np.maximum(1.0, s)
    out["round"] = (out["round"] / over).astype(np.float32)
    out["spread"] = (out["spread"] / over).astype(np.float32)

    return out


def smooth_params(p: Dict[str, np.ndarray], fps: int, tau_ms: float = 28.0) -> Dict[str, np.ndarray]:
    """一阶低通，去掉帧级毛刺（参数本身已是连续的，这步只压残余抖动）。"""
    if tau_ms <= 0:
        return p
    alpha = 1.0 - float(np.exp(-1.0 / max(1.0, fps * tau_ms / 1000.0)))
    out = {}
    for k, v in p.items():
        y = np.empty_like(v)
        acc = float(v[0]) if len(v) else 0.0
        for i in range(len(v)):
            acc += alpha * (float(v[i]) - acc)
            y[i] = acc
        out[k] = y
    return out


def frames_params(segs: List[Seg], duration: float, fps: int = 25,
                  tau_ms: float = 28.0) -> List[Dict[str, float]]:
    """主入口：音素段 → 每帧一个 dict。"""
    t = track(segs, duration, fps)
    t = smooth_params(t, fps, tau_ms)
    n = len(t["open"])
    out = []
    for i in range(n):
        out.append({d: float(t[d][i]) for d in DIMS})
    return out
