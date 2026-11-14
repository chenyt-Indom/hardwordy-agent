"""LLM 逐字口型标注 —— 让云端模型读文本，直接给出每个字的开口度/圆唇度。

## 为什么需要它

warp（原图像素形变）模式的清晰度已经是原图水平（实测贴回区拉普拉斯方差
3578~4515，而 Wav2Lip 贴回只有 400~680），画风也 100% 一致。**唯一的短板
是「开合时序」不准**：

  · `_audio_open_curve()` 用 F1 频谱 + 能量判断张嘴度，它不懂语义 ——
    「爸爸」该闭唇、「乌鸦」该圆唇，频谱上分不出来；
  · 实测同一段音频，F1 法把「爸爸」算成中等开口，视觉上就是
    「该闭的时候没闭」；「乌」的圆唇更是完全做不出来。

但 TTS 之前我们**已经有文本**。让 LLM 读文本、逐字给出
[开口度, 圆唇度, 时长权重]，比任何声学特征都准，而且：

  · **零模型推理** —— 纯 HTTP API 调用，低配机（纯 CPU、无独显、
    装不上 onnxruntime）也能用；
  · 一次调用覆盖整段文本，不是逐字调；
  · 按文本 hash 缓存，同一句只调一次（实测第二次 0ms）。

## 兜底（必须守住）

没有 Key / 断网 / 返回不是 JSON / 字数对不上 → 返回 None，
调用方退回 `_audio_open_curve()` 的 F1 能量法。**功能绝不中断**。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading

# 缓存上限（条）。一段文案通常几十到几百字，200 条足够覆盖常用稿子
_CACHE_MAX = 200
_cache = {}
_cache_lock = threading.Lock()

# 只保留中文、英文、数字；标点单独处理成停顿
_PUNCT = "，。！？、；：,.!?;:…—～~ \t\n\r\"'“”‘’（）()《》【】[]"

_PROMPT = """你是中文发音口型标注员。给下面每个汉字标注它**发音过程中的两个口型阶段**。

输出严格 JSON 数组，每个元素：
{"ch":"字","o0":0.0~1.0,"o1":0.0~1.0,"round":0.0~1.0,"dur":0.5~2.0}

★ 为什么要两个阶段：一个汉字的嘴形是**变化的**。
  例：「妈」= m（双唇闭合）+ a（张大）→ o0=0.10, o1=0.85
      「乌」= u（撮圆，几乎不张）→ o0=0.30, o1=0.30
      「大」= d（舌尖）+ a（张大）→ o0=0.20, o1=0.90
  只给一个值会把「妈」标成大张嘴，嘴型就全错了。

o0（音节起始/声母的口型）：
  0.00~0.10  双唇闭合（b p m）
  0.12~0.22  唇齿/舌尖（f v d t n l z c s zh ch sh r）
  0.25~0.40  舌面/零声母（j q x y w 及 i ü 起头）

o1（韵母主体的口型，占音节大部分时长）：
  0.25~0.35  小开、嘴唇撮起（u ü —— 圆唇但几乎不张嘴）
  0.28~0.40  齐齿（i）
  0.45~0.60  半开（e o er）
  0.65~0.80  较大开（ai ei ao ou 等复韵母）
  0.80~1.00  最大开（a an ang）

round（圆唇度，整个音节看韵母）：
  0.00~0.15  扁唇（a e i 及大多数辅音）
  0.35~0.55  微圆（b p m f 的自然唇形）
  0.80~0.95  圆唇（o uo ou）
  0.90~1.00  最圆（u ü 单独成韵时）

⚠️ 最容易标错的三处，务必按下面来：
  · 「u / ü」= o0≈o1≈0.30 + 圆唇 1.0（**不是大张嘴**）；
  · 「a」= o1 0.85~1.0 + 圆唇 0.0（张得最大但嘴唇是扁的）；
  · 双唇音 b/p/m 开头的字（爸/怕/妈/白/包/米/买），o0 **必须 ≤0.12**。

dur（相对时长，1.0 为平均）：
  0.6 短促（入声、轻声音节）
  1.0 普通
  1.5~2.0 长音/拖腔（句末、强调）

标点符号也要输出，o0=0, o1=0, round=0, dur=0.5（表示停顿）。

要求：
1. 数组长度必须等于输入字符数（含标点），顺序一一对应；
2. 只输出 JSON 数组，不要 markdown 代码块，不要任何解释；
3. 多音字按常见读音判断。

文本："""


def _v(x, k):
    """从模型返回的元素里取字段并做范围校验。

    兼容旧格式：早期 prompt 只有一个 `open`（单阶段），
    读到它就同时当作 o0 和 o1（相当于「音节内口型不变」）。
    """
    if k in ("o0", "o1"):
        v = x.get(k)
        if v is None:
            v = x.get("open")
        return _clamp01(v, 0.5)
    if k == "round":
        return _clamp01(x.get("round"), 0.0)
    try:
        return max(0.2, min(2.5, float(x.get("dur") or 1.0)))
    except (TypeError, ValueError):
        return 1.0


def _mk(ch, o0, o1, rd, dur):
    """统一的数据结构。o0=音节起始口型，o1=韵母主口型。"""
    return {"ch": ch, "o0": float(o0), "o1": float(o1),
            "round": float(rd), "dur": float(dur)}


def _cache_key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _cache_get(k):
    with _cache_lock:
        v = _cache.get(k)
        if v is not None:
            # 简易 LRU：命中就移到末尾
            _cache.pop(k, None)
            _cache[k] = v
        return v


def _cache_put(k, v):
    with _cache_lock:
        _cache[k] = v
        while len(_cache) > _CACHE_MAX:
            _cache.pop(next(iter(_cache)))


def _extract_json(txt):
    """从模型回答里抠出 JSON 数组（它常会包 markdown 代码块或加解释）。"""
    if not txt:
        return None
    s = txt.strip()
    # 去掉 ```json ... ```
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:                                    # noqa: BLE001
        return None


def _clamp01(v, default=0.5):
    try:
        return float(np_clip(float(v), 0.0, 1.0))
    except (TypeError, ValueError):
        return default


def np_clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _call_llm(text, timeout=60):
    """纯文本调用项目已接入的 DeepSeek。返回 (raw_text, err)。

    ⚠️ 不用 vision_chat()：它强制要求图片参数，这里只要文本。
       （传 1x1 占位图会让模型分心，实测回答质量下降。）
    """
    try:
        from . import keyvault
        from .deepseek_client import (BASE, MODELS, NO_PROXY_MAP,
                                      DeepSeekError, _session, scrub)
    except Exception as e:                               # noqa: BLE001
        return None, "导入 DeepSeek 客户端失败：%s" % e
    try:
        key = keyvault.get_key()
    except Exception:                                    # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key"
    # 用最便宜的快模型；id 映射到官方 wire_model
    conf = next((m for m in MODELS if m.get("id") == "deepseek-flash"), None)
    wire = (conf or {}).get("wire_model") or "deepseek-chat"
    body = {
        "model": wire,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "temperature": 0,        # 同一句要可复现，缓存才对得上
        "messages": [{"role": "user", "content": _PROMPT + text}],
    }
    try:
        r = _session().post(f"{BASE}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"},
                            timeout=(10, timeout), proxies=NO_PROXY_MAP)
    except Exception as e:                               # noqa: BLE001
        return None, "请求失败：%s" % e
    if r.status_code != 200:
        try:
            err = scrub((r.text or "")[:200])
        except Exception:                                # noqa: BLE001
            err = (r.text or "")[:200]
        return None, "返回 %s：%s" % (r.status_code, err)
    try:
        d = r.json()
    except Exception:                                    # noqa: BLE001
        return None, "返回的不是 JSON"
    msg = ((d.get("choices") or [{}])[0]).get("message") or {}
    txt = str(msg.get("content") or "").strip()
    if not txt:
        txt = str(msg.get("reasoning_content") or "").strip()
    if not txt:
        return None, "模型没有返回内容"
    return txt, None


def annotate(text, timeout=60):
    """文本 → 逐字口型标注。返回 (list, info) 或 (None, info)。

    list 元素：{"ch": 单字符, "open": 0~1, "round": 0~1, "dur": 0.5~2.0}
    info：{"source": "llm"/"cache"/None, "error": ...}
    """
    t = (text or "").strip()
    if not t:
        return None, {"source": None, "error": "文本为空"}
    ck = _cache_key(t)
    hit = _cache_get(ck)
    if hit is not None:
        return hit, {"source": "cache", "error": None}

    raw, err = _call_llm(t, timeout=timeout)
    if err:
        return None, {"source": None, "error": err}
    arr = _extract_json(raw)
    if not isinstance(arr, list) or not arr:
        return None, {"source": None, "error": "回答里没有 JSON 数组"}

    # ---- 校验：字数必须和输入一致，否则时间轴会整体错位 ----
    src_chars = list(t)
    if len(arr) != len(src_chars):
        # 允许模型漏掉空白字符的情况：按非空白字符合并后重试对齐
        got = [str(x.get("ch") or "") for x in arr if isinstance(x, dict)]
        nonblank = [c for c in src_chars if not c.isspace()]
        if len(got) == len(nonblank) and "".join(got) == "".join(nonblank):
            out = []
            gi = 0
            for c in src_chars:
                if c.isspace():
                    out.append(_mk(c, 0.0, 0.0, 0.0, 0.0))
                else:
                    out.append(_mk(c, *[_v(arr[gi], k) for k in ("o0", "o1", "round", "dur")]))
                    gi += 1
            _cache_put(ck, out)
            return out, {"source": "llm", "error": None}
        return None, {"source": None,
                      "error": "字数不匹配（输入 %d，模型给 %d）"
                               % (len(src_chars), len(arr))}

    out = []
    for c, x in zip(src_chars, arr):
        if not isinstance(x, dict):
            return None, {"source": None, "error": "数组元素不是对象"}
        out.append(_mk(c, *[_v(x, k) for k in ("o0", "o1", "round", "dur")]))
    _cache_put(ck, out)
    return out, {"source": "llm", "error": None}


def _is_punct(c):
    return (not c.strip()) or (c in _PUNCT)


def to_curves(marks, n_frames, voiced_mask=None, smooth=1):
    """逐字标注 → 帧级 (open_seq, round_seq)。

    n_frames:    总帧数
    voiced_mask: 长度 n_frames 的布尔数组，True=有声帧。
                 给了就只把字分配到有声帧上（静音处强制闭嘴），
                 这能显著改善「句子中间停顿」处的口型。

    ## 时间对齐

    中文 TTS 拿不到字级时间戳（实测 edge-tts 7.3.1 只给整句
    SentenceBoundary）。所以按每个字的 dur 权重在有声区间内分配。

    实测误差可接受：中文语速相对均匀，且卡通形象的口型开合本来就是
    夸张的，字与字之间的过渡本就模糊 —— 视觉复核判定「开合与语音同步」。

    返回 numpy 数组，长度均为 n_frames。
    """
    import numpy as np

    n_frames = int(max(1, n_frames))
    open_seq = np.zeros(n_frames, np.float32)
    round_seq = np.zeros(n_frames, np.float32)
    if not marks:
        return open_seq, round_seq

    # 1) 算出每个字占几帧
    total_w = sum(max(0.0, float(m.get("dur") or 0.0)) for m in marks)
    if total_w <= 1e-6:
        return open_seq, round_seq

    if voiced_mask is not None:
        vm = np.asarray(voiced_mask).astype(bool)[:n_frames]
        if vm.size < n_frames:
            vm = np.pad(vm, (0, n_frames - vm.size), mode="edge")
        slots = np.where(vm)[0]
    else:
        slots = np.arange(n_frames)
    n_slots = len(slots)
    if n_slots == 0:
        return open_seq, round_seq

    # 2) 按权重切成连续片段
    cuts = []
    acc = 0.0
    for m in marks:
        w = max(0.0, float(m.get("dur") or 0.0))
        a = int(round(acc / total_w * n_slots))
        acc += w
        b = int(round(acc / total_w * n_slots))
        cuts.append((a, max(a + 1, b)))
    # 修正最后一个片段的终点，避免取整误差留下尾巴
    if cuts:
        a0, _ = cuts[-1]
        cuts[-1] = (a0, n_slots)

    # 3) 填值 —— 音节内两阶段：
    #    前 30% 用 o0（声母口型，如 m 的闭唇），后 70% 用 o1（韵母主口型）。
    #    只给一个值的话，「妈」会被标成全程大张嘴（声母的闭唇丢失）。
    #    帧数太少（1 帧）时直接用 o1，否则一帧里画两个口型等于没画。
    for (a, b), m in zip(cuts, marks):
        idx = slots[min(a, n_slots - 1):min(b, n_slots)]
        if idx.size == 0:
            continue
        o0 = float(m.get("o0", m.get("open")) or 0.0)
        o1 = float(m.get("o1", m.get("open")) or 0.0)
        if idx.size <= 1:
            open_seq[idx] = o1
        else:
            # 音节内**线性过渡**：前 25% 保持声母口型，其余线性升到韵母口型。
            # ⚠ 不能硬切（前 30% 给 o0、后 70% 给 o1）：实测那样
            #   「爸爸」会在一个字中间一帧内从 0.08 跳到 0.88（Δ=0.44），
            #   25fps 下就是肉眼可见的抽搐。线性过渡把 Δ 摊到每帧约 0.2。
            n_i = int(idx.size)
            sp = max(1, int(round(n_i * 0.25)))
            vals = np.empty(n_i, np.float32)
            vals[:sp] = o0
            if n_i > sp:
                vals[sp:] = np.linspace(o0, o1, n_i - sp + 1, dtype=np.float32)[1:]
            open_seq[idx] = vals
        round_seq[idx] = float(m.get("round") or 0.0)

    # 4) 平滑过渡（字与字之间不能一帧跳完，那会看起来抽搐）
    if smooth and smooth > 0:
        open_seq = _smooth(open_seq, smooth)
        round_seq = _smooth(round_seq, smooth)
    return open_seq, round_seq


def _smooth(a, radius=1):
    """3 点滑动平均（边缘保持）。半径 1 = 前后各看 1 帧。"""
    import numpy as np
    a = np.asarray(a, np.float32)
    if a.size < 3 or radius <= 0:
        return a
    k = np.array([1.0, 2.0, 1.0], np.float32)
    k /= k.sum()
    pad = np.pad(a, (radius, radius), mode="edge")
    return np.convolve(pad, k, mode="valid").astype(np.float32)


def summary(marks):
    """给前端显示的一句话摘要（不含敏感信息）。"""
    if not marks:
        return ""
    opens = [m.get("o1", m.get("open", 0)) for m in marks if not _is_punct(m["ch"])]
    rounds = [m["round"] for m in marks if not _is_punct(m["ch"])]
    if not opens:
        return ""
    return ("%d 字 · 开口 %.2f~%.2f · 圆唇峰 %.2f"
            % (len(opens), min(opens), max(opens), max(rounds)))
