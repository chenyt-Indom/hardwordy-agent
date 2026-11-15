"""编排：文本 → TTS → 对齐 → 渲染 → mp4

## 对齐方案（这是本项目最关键的取舍）

要做到「嘴型跟每个字对得上」，理想方案是用强制对齐器（whisper）拿字级时间戳。
但实测这台机器上走不通：

    · edge-tts 对中文返回 **0 条 WordBoundary**（英文有，中文没有）
    · faster-whisper 能装（1.2.1），但模型下载被墙：
      huggingface.co 连接超时；hf-mirror.com 返回 429 限流

所以这里改用**零依赖的两级对齐**：

    第一级（硬锚点）：音频里的静音段
        实测「爸爸去北京大学，乌鸦在天上飞。」的包络，
        逗号处有 300ms 静音（t=1.49~1.79）、句尾有 510ms 静音（t=3.04~3.55）。
        这些静音段的位置来自**实际音频**，是精确的。
    第二级（段内分配）：音节时长权重
        段内（一句 4~10 字）按语言学权重分配：韵母成分越多越长、
        带鼻韵尾更长、擦音/塞擦音声母更长。误差被限制在一个短句内。

为什么这够用：嘴型错位的观感主要来自**句子级**的偏移（越说越偏）。
段级锚点把累积误差清零，段内最多偏半个字（约 100ms），肉眼不易察觉。
而口型形状本身由音素表精确决定 —— 那才是「嘴形像不像在说话」的关键。

## 如果以后能下载 whisper

把 `_align_by_silence()` 换成 whisper 的 word_timestamps 即可，
上层接口 `align()` 的签名不用变。
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time
import uuid
import wave
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import phoneme as ph
from . import render as rd
from . import viseme as vs

logger = logging.getLogger(__name__)

FPS = 25
MAX_SECONDS = 120.0
_OUT_DIR = ""


def _out_dir() -> str:
    global _OUT_DIR
    if not _OUT_DIR:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        _OUT_DIR = os.path.join(root, "data", "avatar2", "videos")
    os.makedirs(_OUT_DIR, exist_ok=True)
    return _OUT_DIR


def _ffmpeg() -> str:
    try:
        from .. import avatar2
        return avatar2.ffmpeg() or ""
    except Exception:                                # noqa: BLE001
        logger.debug("ffmpeg lookup failed", exc_info=True)
        return ""


# ---------------------------------------------------------------- 音频


def _read_wav(path: str) -> Tuple[int, np.ndarray]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        y = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        y = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    return sr, y


def _to_wav(src: str) -> str:
    """任意音频 → 16k 单声道 wav（对齐分析用）。"""
    if src.lower().endswith(".wav"):
        try:
            _read_wav(src)
            return src
        except Exception:                            # noqa: BLE001
            logger.debug("wav unreadable, re-encode", exc_info=True)
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("找不到 ffmpeg，无法分析音频")
    dst = src + ".16k.wav"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", src,
                    "-ac", "1", "-ar", "16000", dst], check=True,
                   creationflags=0x08000000)
    return dst


def _silences(wav: str, min_gap: float = 0.07,
              rel: float = 0.06) -> Tuple[float, List[Tuple[float, float]]]:
    """检测静音段。返回 (总时长, [(t0, t1), ...])。"""
    sr, y = _read_wav(wav)
    dur = len(y) / float(sr)
    win = max(1, int(0.02 * sr))
    hop = max(1, int(0.01 * sr))
    if len(y) < win * 2:
        return dur, []
    n = 1 + (len(y) - win) // hop
    idx = np.arange(n)[:, None] * hop + np.arange(win)[None, :]
    frames = y[idx]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    peak = float(np.percentile(rms, 92)) or 1e-6
    thr = max(peak * rel, 1e-4)

    quiet = rms < thr
    out: List[Tuple[float, float]] = []
    i = 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j + 1 < len(quiet) and quiet[j + 1]:
                j += 1
            t0 = i * hop / float(sr)
            t1 = min(dur, (j * hop + win) / float(sr))
            if t1 - t0 >= min_gap:
                out.append((t0, t1))
            i = j + 1
        else:
            i += 1
    return dur, out


# ---------------------------------------------------------------- 对齐


# 音节时长权重：韵母成分越多、带鼻韵尾、擦音声母 → 越长
_INI_W = {"stop": 0.05, "nasal": 0.09, "liquid": 0.06,
          "fric": 0.15, "affr": 0.17}


def _syl_weight(ch: str) -> float:
    ini, fin = ph._split_pinyin(ch)                  # noqa: SLF001
    w = 1.0
    parts = ph._FINALS.get(fin) or [("e", 1.0)]      # noqa: SLF001
    w += 0.17 * (len(parts) - 1)
    if fin.endswith("ng"):
        w += 0.16
    elif fin.endswith("n"):
        w += 0.13
    if ini in ph._INITIAL_CLASS:                     # noqa: SLF001
        cls, _ = ph._INITIAL_CLASS[ini]              # noqa: SLF001
        w += _INI_W.get(cls, 0.08)
    return max(0.5, w)


def _split_clauses(text: str) -> List[str]:
    """按标点切短句，标点跟在前一句末尾。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in ph._PUNCT_PAUSE:                    # noqa: SLF001
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return [c for c in out if c.strip()]


def align(text: str, wav: str,
          words: Optional[List[Tuple[str, float, float]]] = None
          ) -> List[Tuple[str, float, float]]:
    """文本 → [(字, t0, t1)]。优先用调用方给的 words，否则静音锚点 + 权重分配。"""
    text = (text or "").strip()
    if not text:
        return []
    if words:
        return [(str(w[0]), float(w[1]), float(w[2])) for w in words]

    # ⚠ 只认「标点级」长停顿（≥0.13s）。第一版用 0.07s，结果句内的短气口
    #   也被当成块边界，把后半句 5 个字挤进 0.14 秒（实测 乌 2.84-2.86、
    #   鸦 2.86-2.88）—— 对齐彻底错位。标点停顿实测 300ms 以上，0.13s 足够区分。
    try:
        dur, sil = _silences(wav, min_gap=0.13)
    except Exception:                                # noqa: BLE001
        logger.warning("静音检测失败，退回均分", exc_info=True)
        return ph._estimate_words(text)              # noqa: SLF001

    # 有声块：长停顿之间的区间
    voiced: List[Tuple[float, float]] = []
    cur = 0.0
    for s0, s1 in sil:
        if s0 - cur > 0.05:
            voiced.append((cur, s0))
        cur = max(cur, s1)
    if dur - cur > 0.05:
        voiced.append((cur, dur))
    if not voiced:
        return ph._estimate_words(text)              # noqa: SLF001

    clauses = _split_clauses(text)

    def _chars(c: str) -> List[str]:
        return [ch for ch in c
                if not (ch in ph._PUNCT_PAUSE or ch.isspace())]  # noqa: SLF001

    clauses = [c for c in clauses if _chars(c)]
    if not clauses:
        return ph._estimate_words(text)              # noqa: SLF001

    # ---- 分句 → 有声块 ----
    # 只有「块数 == 分句数」时才一一对应；否则整段按音节权重分配。
    # 不做「按比例合并块」——那个逻辑正是把后半句压扁的元凶。
    if len(voiced) == len(clauses):
        groups: List[Tuple[float, float]] = list(voiced)
    else:
        span0, span1 = voiced[0][0], voiced[-1][1]
        tot = sum(len(_chars(c)) for c in clauses) or 1
        groups = []
        t = span0
        for cl in clauses:
            d = (span1 - span0) * len(_chars(cl)) / tot
            groups.append((t, t + d))
            t += d

    def _spread(gs: List[Tuple[float, float]]) -> List[Tuple[str, float, float]]:
        res: List[Tuple[str, float, float]] = []
        for cl, (g0, g1) in zip(clauses, gs):
            chars = _chars(cl)
            if not chars:
                continue
            ws = [_syl_weight(c) for c in chars]
            s = sum(ws) or 1.0
            t = g0
            for c, w in zip(chars, ws):
                d = (g1 - g0) * w / s
                res.append((c, t, t + d))
                t += d
        return res

    out = _spread(groups)

    # ---- 兜底：单字时长过短说明分配异常 ----
    # 实测过的翻车：后半句 5 个字各分到 20ms。任何字短于 40ms 都判定异常，
    # 退化为「整段有声区间按权重均匀分配」。宁可粗略，也不能错位。
    if out and min(t1 - t0 for _, t0, t1 in out) < 0.04:
        logger.warning("对齐异常（存在 <40ms 的字），退化为整段权重分配")
        span0, span1 = voiced[0][0], voiced[-1][1]
        tot = sum(_syl_weight(c) for c in (_chars("".join(clauses))))
        tot = tot or 1.0
        gs = []
        t = span0
        for cl in clauses:
            w = sum(_syl_weight(c) for c in _chars(cl))
            d = (span1 - span0) * w / tot
            gs.append((t, t + d))
            t += d
        out = _spread(gs)
    return out


# ---------------------------------------------------------------- 嘴部几何


def mouth_geom(img_path: str, img_w: int, img_h: int,
               override: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """归一化嘴部框 → 像素几何 {cx,cy,w,h}。"""
    g = override
    if not g:
        try:
            from .. import avatar2
            d = avatar2.detect_mouth(img_path)
            if d.get("ok"):
                g = d.get("mouth") or d
        except Exception:                            # noqa: BLE001
            logger.warning("嘴部检测失败", exc_info=True)
    if not g:
        # 兜底：画面中央偏下（宁可粗略也不要崩）
        g = {"x": 0.5, "y": 0.66, "w": 0.16, "h": 0.07}
    return {
        "cx": float(g.get("x", 0.5)) * img_w,
        "cy": float(g.get("y", 0.66)) * img_h,
        "w": float(g.get("w", 0.16)) * img_w,
        "h": float(g.get("h", 0.07)) * img_h,
    }


# ---------------------------------------------------------------- 渲染


def render_text(text: str, img_path: str, out_mp4: str = "",
                voice: str = "", rate: int = 0, engine: str = "auto",
                style: str = "", fps: int = FPS,
                on_progress=None, geom: Optional[Dict[str, float]] = None,
                words: Optional[List[Tuple[str, float, float]]] = None) -> dict:
    """文本 → mp4。这是对外主入口。"""
    from PIL import Image

    from .. import tts as tts_mod

    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有数字人形象图"}

    # ---- 1. TTS ----
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=style or "")
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    audio = a.get("path") or ""
    if not audio or not os.path.isfile(audio):
        return {"ok": False, "error": "配音文件不存在"}

    return render_audio(audio, img_path, text=t, out_mp4=out_mp4, fps=fps,
                        on_progress=on_progress, geom=geom, words=words)


def render_audio(audio: str, img_path: str, text: str = "",
                 out_mp4: str = "", fps: int = FPS,
                 on_progress=None, geom: Optional[Dict[str, float]] = None,
                 words: Optional[List[Tuple[str, float, float]]] = None) -> dict:
    """已有音频 → mp4。"""
    from PIL import Image

    ff = _ffmpeg()
    if not ff:
        return {"ok": False, "error": "找不到 ffmpeg"}

    wav = _to_wav(audio)
    sr, y = _read_wav(wav)
    dur = len(y) / float(sr)
    if dur < 0.15:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}
    if dur > MAX_SECONDS:
        return {"ok": False, "error": "音频 %.0f 秒，超过 %d 秒上限"
                % (dur, MAX_SECONDS)}

    # ---- 2. 对齐 ----
    ws = align(text, wav, words) if text else []

    # ---- 3. 音素 → 参数 ----
    if text:
        segs = ph.timeline(text, ws)
    else:
        segs = [ph.Seg("silence", "-", 0.0, dur)]
    params = vs.frames_params(segs, dur, fps)

    # ---- 4. 图像准备 ----
    with Image.open(img_path) as im:
        img = im.convert("RGB")
    # 过大就缩到长边 960，CPU 渲染更快且画质够用
    # ⚠ 小图必须放大再渲染。实测形象图 334x225 时嘴部只有 62x15 像素，
    #   开口 20 像素在整张脸上只占 9%，观感就是「嘴几乎没动」。
    #   放大不增加真实细节，但让形变有足够像素可用，边缘也更平滑。
    maxside = 960
    minside = 720
    _m = max(img.size)
    if _m > maxside:
        k = maxside / float(_m)
        img = img.resize((max(2, int(img.width * k)), max(2, int(img.height * k))),
                         Image.LANCZOS)
    elif _m < minside:
        k = minside / float(_m)
        img = img.resize((max(2, int(img.width * k)), max(2, int(img.height * k))),
                         Image.LANCZOS)
    # ⚠ libx264 + yuv420p 要求宽高都是偶数。实测原图 334x225（高是奇数），
    #   ffmpeg 直接报 "height not divisible by 2" 并退出，主进程写 stdin
    #   就抛 BrokenPipeError —— 报错信息完全指不到真因，必须在这里裁掉。
    _w, _h = img.size
    if _w % 2 or _h % 2:
        img = img.crop((0, 0, _w - (_w % 2), _h - (_h % 2)))
    arr = np.array(img)
    H, W = arr.shape[:2]
    g = mouth_geom(img_path, W, H, geom)
    p = rd.prep(arr, g)

    # ---- 5. 渲染 + ffmpeg ----
    if not out_mp4:
        out_mp4 = os.path.join(_out_dir(), "v3_%s_%s.mp4"
                               % (time.strftime("%Y%m%d_%H%M%S"),
                                  uuid.uuid4().hex[:4]))
    cmd = [ff, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-",
           "-i", audio,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-shortest", "-movflags", "+faststart", out_mp4]
    # ⚠ stderr 不能给 subprocess.PIPE 然后不读 —— 实测 ffmpeg 会输出
    #   "Estimating duration from bitrate" 这类警告，缓冲区（约 64KB）填满后
    #   ffmpeg 阻塞并退出，主进程写 stdin 就抛 BrokenPipeError。
    #   所以写到临时文件，失败时再读出来当错误信息。
    import tempfile as _tf
    errf = _tf.NamedTemporaryFile(prefix="th_ff_", suffix=".log", delete=False)
    err_path = errf.name
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=errf,
                            creationflags=0x08000000)
    n = len(params)
    t_start = time.time()
    try:
        for i, prm in enumerate(params):
            fr = rd.frame(p, prm)
            proc.stdin.write(fr.tobytes())
            if on_progress and (i % 8 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                    # noqa: BLE001
                    logger.debug("progress cb failed", exc_info=True)
    except Exception as e:                           # noqa: BLE001
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            logger.debug("kill failed", exc_info=True)
        try:
            errf.close()
        except Exception:                            # noqa: BLE001
            logger.debug("close errf failed", exc_info=True)
        _e = ""
        try:
            with open(err_path, "r", encoding="utf-8", errors="ignore") as _f:
                _e = _f.read()[:400]
        except Exception:                            # noqa: BLE001
            logger.debug("read err failed", exc_info=True)
        return {"ok": False, "error": "渲染中断：%s: %s | ffmpeg: %s"
                % (type(e).__name__, e, _e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                            # noqa: BLE001
            logger.debug("stdin close failed", exc_info=True)
        try:
            errf.close()
        except Exception:                            # noqa: BLE001
            logger.debug("close errf failed", exc_info=True)
    rc = proc.wait()
    try:
        with open(err_path, "r", encoding="utf-8", errors="ignore") as f:
            err = f.read()
    except Exception:                                # noqa: BLE001
        err = ""
    finally:
        try:
            os.unlink(err_path)
        except Exception:                            # noqa: BLE001
            logger.debug("unlink err failed", exc_info=True)
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s" % (err or "")[:300]}
    return {"ok": True, "path": out_mp4, "duration": round(dur, 2),
            "frames": n, "size": [W, H], "fps": fps,
            "audio": audio, "bytes": os.path.getsize(out_mp4),
            "seconds": round(time.time() - t_start, 2),
            "syllables": len([s for s in segs if s.kind != "silence"]),
            "aligned": len(ws), "geom": {k: round(v, 1) for k, v in g.items()}}
