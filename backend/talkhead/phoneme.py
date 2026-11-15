"""文本 → 音素时间线

把一句话拆成按时间排好的音素段（声母/韵母/停顿），供 viseme.py 转口型。

## 三个必须处理的坑（都是实测踩过的）

### 1. 拼音正字法不等于音系

pypinyin 给的是**正字法拼写**，不是真实音素。实测：

    鱼   → y + u     真实是零声母 + ü
    月亮 → y + ue    真实是零声母 + üe（yue 的 u 是 ü）
    乌鸦 → w + u     真实是零声母 + u（w 只是 u 的书写形式）
    西安 → x + i, '' + an   第二个音节零声母

如果照抄，`u`（撮口圆唇）和 `ü`（更圆的撮口）会被当成同一个，
`y`/`w` 会被当成真声母（舌尖/唇的动作），口型必然错。
所以 `_normalize()` 里做了三条修正：

  · initial 是 y/w/空 → 视为零声母（不产生声母段）
  · initial 是 j/q/x/y 且 final 以 u 开头 → u 改 ü
  · 写作 "ue" 的 → "üe"

### 2. 声母时长按发音方式差别很大

闭塞音（b p d t g k）是先完全闭合再爆破，闭合段占掉大半个声母时长；
擦音（f s sh x h）是持续摩擦，整个声母都在出声；鼻音（m n）是带音浊音。
如果一律给 25%，「爸爸」的两个 b 会显得嘴一直在动，而真人发 b 时嘴是**先闭上停一下**。

### 3. 复合韵母内部必须有过渡

「白」= b + ai，口型是 大张 → 收小 的连续滑动；
「好」= h + ao，是 大张 → 撮圆。
把它压成单一静态口型，「嘴形不像在说话」就出在这里。
所以韵母拆成 韵头/韵腹/韵尾 三段，各占不同时长。

## 时间从哪来

优先用调用方给的字级时间戳（TTS/ASR 都能给 `words=[(字, t0, t1)]`）；
没有就按中文平均语速估算（默认 4.6 字/秒，标点额外停顿）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = __import__("logging").getLogger(__name__)

try:
    from pypinyin import Style, lazy_pinyin
    _HAS_PYPINYIN = True
except Exception:                                    # noqa: BLE001
    _HAS_PYPINYIN = False
    logger.warning("pypinyin 不可用，音素对齐将退回按字数均分的近似方案")

# ---------------------------------------------------------------- 数据结构


@dataclass
class Seg:
    """一个音素段。kind: initial / final / silence"""
    kind: str
    phone: str
    t0: float
    t1: float

    @property
    def dur(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def __repr__(self) -> str:
        return "<%s %s %.2f-%.2f>" % (self.kind, self.phone, self.t0, self.t1)


@dataclass
class Syllable:
    """一个音节（一个字）"""
    ch: str
    initial: str
    final: str
    t0: float
    t1: float
    segs: List[Seg] = field(default_factory=list)


# ---------------------------------------------------------------- 声母表

# 发音方式 → (占音节时长比例, 声母类型标记)
# 实测依据：闭塞音闭合段长、擦音摩擦段长、零声母无段
_INITIAL_CLASS = {
    # 闭塞音：先闭合再爆破，闭合段占大头
    "b": ("stop", 0.20), "p": ("stop", 0.22), "d": ("stop", 0.18),
    "t": ("stop", 0.20), "g": ("stop", 0.18), "k": ("stop", 0.20),
    # 鼻音：浊音，带鼻腔共振，比闭塞音长
    "m": ("nasal", 0.22), "n": ("nasal", 0.20),
    # 边音
    "l": ("liquid", 0.18),
    # 擦音：持续摩擦
    "f": ("fric", 0.26), "s": ("fric", 0.28), "sh": ("fric", 0.28),
    "x": ("fric", 0.26), "h": ("fric", 0.24), "r": ("fric", 0.24),
    # 塞擦音：闭合+摩擦
    "z": ("affr", 0.24), "c": ("affr", 0.26),
    "zh": ("affr", 0.26), "ch": ("affr", 0.28),
    "j": ("affr", 0.24), "q": ("affr", 0.26),
}

# ---------------------------------------------------------------- 韵母表

# 每个韵母拆成若干「目标口型」，按顺序在韵母时长内滑动。
# 每项 (音素名, 时长权重)。权重会在音节内归一化。
# 拆法依据：韵头(介音)短、韵腹(主元音)最长、韵尾短。
_FINALS = {
    # ---- 单韵母 ----
    "a": [("a", 1.0)],
    "o": [("o", 1.0)],
    "e": [("e", 1.0)],
    "ê": [("ê", 1.0)],
    "i": [("i", 1.0)],
    "u": [("u", 1.0)],
    "ü": [("ü", 1.0)],
    "er": [("e", 0.55), ("r", 0.45)],
    # 舌尖元音（zi ci si / zhi chi shi ri 的 -i）：几乎不张嘴，靠舌位
    "-i": [("-i", 1.0)],
    # ---- 复韵母（前响）----
    "ai": [("a", 0.62), ("i", 0.38)],
    "ei": [("e", 0.62), ("i", 0.38)],
    "ao": [("a", 0.60), ("u", 0.40)],
    "ou": [("o", 0.60), ("u", 0.40)],
    # ---- 复韵母（后响，带介音）----
    "ia": [("i", 0.26), ("a", 0.74)],
    "ie": [("i", 0.26), ("ê", 0.74)],
    "ua": [("u", 0.26), ("a", 0.74)],
    "uo": [("u", 0.26), ("o", 0.74)],
    "üe": [("ü", 0.26), ("ê", 0.74)],
    # ---- 复韵母（中响，三段）----
    "iao": [("i", 0.20), ("a", 0.52), ("u", 0.28)],
    "iou": [("i", 0.20), ("o", 0.50), ("u", 0.30)],
    "uai": [("u", 0.20), ("a", 0.52), ("i", 0.28)],
    "uei": [("u", 0.20), ("e", 0.50), ("i", 0.30)],
    # ---- 鼻韵母（前鼻 n）----
    "an": [("a", 0.68), ("n", 0.32)],
    "en": [("e", 0.66), ("n", 0.34)],
    "in": [("i", 0.66), ("n", 0.34)],
    "un": [("u", 0.66), ("n", 0.34)],
    "ün": [("ü", 0.66), ("n", 0.34)],
    "ian": [("i", 0.22), ("a", 0.50), ("n", 0.28)],
    "uan": [("u", 0.22), ("a", 0.50), ("n", 0.28)],
    "üan": [("ü", 0.20), ("ê", 0.50), ("n", 0.30)],
    # ---- 鼻韵母（后鼻 ng）----
    "ang": [("a", 0.66), ("ng", 0.34)],
    "eng": [("e", 0.64), ("ng", 0.36)],
    "ing": [("i", 0.64), ("ng", 0.36)],
    "ong": [("o", 0.64), ("ng", 0.36)],
    "iang": [("i", 0.22), ("a", 0.48), ("ng", 0.30)],
    "uang": [("u", 0.22), ("a", 0.48), ("ng", 0.30)],
    "ueng": [("u", 0.20), ("e", 0.46), ("ng", 0.34)],
    "iong": [("i", 0.20), ("o", 0.46), ("ng", 0.34)],
}

# 拼音缩写 → 完整韵母（pypinyin 有时给缩写）
_FINAL_ALIAS = {
    "iu": "iou", "ui": "uei", "un": "uen" if False else "un",
    "ue": "üe", "ve": "üe",
    "v": "ü", "van": "üan", "vn": "ün",
}

# 数字 → 拼音（TTS 会把数字念出来，口型要跟上）
_DIGITS = {
    "0": "ling", "1": "yi", "2": "er", "3": "san", "4": "si",
    "5": "wu", "6": "liu", "7": "qi", "8": "ba", "9": "jiu",
}

_PUNCT_PAUSE = {
    "，": 0.18, ",": 0.18, "、": 0.14, "；": 0.22, ";": 0.22,
    "：": 0.20, ":": 0.20, "。": 0.34, ".": 0.34, "！": 0.32, "!": 0.32,
    "？": 0.32, "?": 0.32, "…": 0.30, "—": 0.24, "\n": 0.30,
}

_DEFAULT_CPS = 4.6          # 中文平均语速（字/秒），用于没有时间戳时估算


# ---------------------------------------------------------------- 工具


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _norm_final(initial: str, final: str) -> str:
    """拼音正字法 → 音系韵母。三条修正见模块 docstring。"""
    f = (final or "").strip().lower()
    ini = (initial or "").strip().lower()

    # 缩写还原
    f = _FINAL_ALIAS.get(f, f)

    # j/q/x/y 后面写的 u 实际是 ü
    if ini in ("j", "q", "x", "y") and f.startswith("u"):
        f = "ü" + f[1:]
    # y 后面写的 ue 实际是 üe（已被上面覆盖），yue → üe
    if f.startswith("üe"):
        f = "üe"

    # 舌尖元音：zhi/chi/shi/ri/zi/ci/si 的 i 不是舌面 i
    if f == "i" and ini in ("zh", "ch", "sh", "r", "z", "c", "s"):
        f = "-i"

    # 零声母 + 单 i/u → 保持
    if not f:
        f = "e"        # 兜底：极少见，给一个中性元音

    return f


def _norm_initial(initial: str) -> str:
    """w / y 不是真声母，是零声母的书写形式。"""
    ini = (initial or "").strip().lower()
    if ini in ("w", "y"):
        return ""
    return ini


def _split_pinyin(ch: str) -> Tuple[str, str]:
    """一个字 → (声母, 韵母)，已修正正字法。非汉字走近似。"""
    if _is_cjk(ch) and _HAS_PYPINYIN:
        try:
            ini = lazy_pinyin(ch, style=Style.INITIALS, strict=False,
                              errors="ignore")
            fin = lazy_pinyin(ch, style=Style.FINALS, strict=False,
                              errors="ignore")
            i = (ini[0] if ini else "").strip()
            f = (fin[0] if fin else "").strip()
            return _norm_initial(i), _norm_final(i, f)
        except Exception:                            # noqa: BLE001
            logger.debug("pinyin failed for %r", ch, exc_info=True)

    # 数字
    if ch in _DIGITS:
        return _split_pinyin(_DIGITS[ch][0]) if False else _latin(_DIGITS[ch])
    # 英文字母：按近似音
    if ch.isascii() and ch.isalpha():
        return _latin(ch.lower())
    # 兜底：中性
    return "", "e"


_LATIN_VOWEL = {
    "a": "a", "e": "e", "i": "i", "o": "o", "u": "u",
}


def _latin(word: str) -> Tuple[str, str]:
    """英文/拼音串 → 近似 (声母, 韵母)。只取第一个元音当韵腹。"""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return "", "e"
    for idx, c in enumerate(w):
        if c in _LATIN_VOWEL:
            ini = w[:idx] if idx > 0 else ""
            # 声母只认单字母，多字母取最后一个（近似）
            ini = ini[-1] if ini else ""
            if ini not in _INITIAL_CLASS:
                ini = ""
            fin = _LATIN_VOWEL[c]
            # 看后面有没有鼻音尾
            rest = w[idx + 1:]
            if rest.startswith("ng"):
                fin += "ng"
            elif rest.startswith("n"):
                fin += "n"
            return ini, fin
    # 没有元音：当纯辅音
    return (w[0] if w[0] in _INITIAL_CLASS else ""), "e"


# ---------------------------------------------------------------- 时间分配


def _alloc_syllable(ch: str, t0: float, t1: float) -> Syllable:
    """把 [t0, t1] 切成声母段 + 韵母段（韵母内部再拆）。"""
    T = max(0.04, t1 - t0)
    ini, fin = _split_pinyin(ch)
    syl = Syllable(ch=ch, initial=ini, final=fin, t0=t0, t1=t1)

    # 声母占比
    if ini and ini in _INITIAL_CLASS:
        cls, ratio = _INITIAL_CLASS[ini]
        # 声母不能吃掉太多（短音节的声母要压一压）
        ratio = min(ratio, 0.42)
        ini_dur = T * ratio
    else:
        cls, ini_dur = "", 0.0

    fin_dur = T - ini_dur
    if fin_dur <= 0.01:                              # 极端情况：全是声母
        fin_dur = T * 0.6
        ini_dur = T - fin_dur

    # 声母段
    if ini_dur > 0.008:
        syl.segs.append(Seg("initial", ini, t0, t0 + ini_dur))

    # 韵母段：按权重切
    parts = _FINALS.get(fin) or [("e", 1.0)]
    wsum = sum(w for _, w in parts) or 1.0
    cur = t0 + ini_dur
    for name, w in parts:
        d = fin_dur * (w / wsum)
        syl.segs.append(Seg("final", name, cur, cur + d))
        cur += d
    # 收尾对齐（浮点误差）
    if syl.segs:
        syl.segs[-1].t1 = max(syl.segs[-1].t1, t1)
    return syl


def _estimate_words(text: str, cps: float = _DEFAULT_CPS) -> List[Tuple[str, float, float]]:
    """没有字级时间戳时，按语速估算。返回 [(字, t0, t1)]，含标点。"""
    out = []
    t = 0.0
    for ch in text:
        if ch in _PUNCT_PAUSE:
            p = _PUNCT_PAUSE[ch]
            out.append((ch, t, t + p))
            t += p
            continue
        if ch.isspace():
            t += 0.06
            continue
        d = 1.0 / max(1.0, cps)
        out.append((ch, t, t + d))
        t += d
    return out


# ---------------------------------------------------------------- 主入口


def timeline(text: str, words: Optional[List[Tuple[str, float, float]]] = None,
             cps: float = _DEFAULT_CPS) -> List[Seg]:
    """文本 → 音素段列表（按时间排序，含停顿段）。

    words: 可选的字级时间戳 [(字, t0, t1)]。给了就用它，没给按语速估算。
    """
    text = (text or "").strip()
    if not text:
        return []

    if words:
        # 调用方给的可能是 (字, t0, t1)，也可能只有 (字, t0)
        norm = []
        for item in words:
            if not item:
                continue
            ch = str(item[0])
            t0 = float(item[1]) if len(item) > 1 else 0.0
            t1 = float(item[2]) if len(item) > 2 else t0 + 1.0 / _DEFAULT_CPS
            if t1 <= t0:
                t1 = t0 + 0.05
            norm.append((ch, t0, t1))
        items = norm
    else:
        items = _estimate_words(text, cps)

    segs: List[Seg] = []
    for ch, t0, t1 in items:
        if ch in _PUNCT_PAUSE or ch.isspace():
            segs.append(Seg("silence", "-", t0, t1))
            continue
        if not ch.strip():
            segs.append(Seg("silence", "-", t0, t1))
            continue
        syl = _alloc_syllable(ch, t0, t1)
        segs.extend(syl.segs)

    # 合并相邻同音素段（避免过度切分）
    merged: List[Seg] = []
    for s in segs:
        if merged and merged[-1].kind == s.kind and merged[-1].phone == s.phone \
                and abs(merged[-1].t1 - s.t0) < 1e-6:
            merged[-1].t1 = s.t1
        else:
            merged.append(s)
    return merged


def syllables(text: str, words: Optional[List[Tuple[str, float, float]]] = None,
              cps: float = _DEFAULT_CPS) -> List[Syllable]:
    """按音节返回（调试/诊断用）。"""
    text = (text or "").strip()
    if not text:
        return []
    items = words or _estimate_words(text, cps)
    out = []
    for ch, t0, t1 in items:
        if ch in _PUNCT_PAUSE or ch.isspace():
            continue
        out.append(_alloc_syllable(str(ch), float(t0), float(t1)))
    return out
