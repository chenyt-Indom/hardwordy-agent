# -*- coding: utf-8 -*-
"""文字转语音（TTS）。

双引擎，engine="auto" 时自动选：
  · edge —— 微软 Edge 神经语音（联网，接近真人，14 个中文音色，输出 mp3）
  · sapi —— Windows 系统语音（离线兜底，音质机械，输出 wav）

设计要点：
  1. **不把 edge_tts 放在模块顶层 import** —— 它没装时整个后端不该起不来，
     只有真正合成时才 import，失败就回退 SAPI。
  2. 长文按句子切成 ~900 字的段，逐段合成后按字节拼接（mp3 帧流可直接拼；
     wav 用 wave 模块重写头）。
  3. 文本先清洗：代码块不读、Markdown 符号去掉、链接只留文字 —— 否则会把
     「星号星号」「井号」念出来。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import tempfile
import time
import uuid
import wave

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(_ROOT, "data", "tts")

# 中文可用音色（实测 edge 返回的 zh-* 全集里挑出来的人声，外加本机 SAPI 一个）
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "gender": "女",
     "tag": "自然亲和，通用首选", "engine": "edge"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊", "gender": "女",
     "tag": "年轻活泼", "engine": "edge"},
    {"id": "zh-CN-YunxiNeural", "name": "云希", "gender": "男",
     "tag": "年轻清朗", "engine": "edge"},
    {"id": "zh-CN-YunjianNeural", "name": "云健", "gender": "男",
     "tag": "浑厚有力，适合解说", "engine": "edge"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬", "gender": "男",
     "tag": "播音腔，新闻感", "engine": "edge"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏", "gender": "男",
     "tag": "少年音", "engine": "edge"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北", "gender": "女",
     "tag": "东北话", "engine": "edge"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮", "gender": "女",
     "tag": "陕西话", "engine": "edge"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "晓臻", "gender": "女",
     "tag": "台湾腔", "engine": "edge"},
    {"id": "zh-HK-HiuMaanNeural", "name": "曉曼", "gender": "女",
     "tag": "粤语女声", "engine": "edge"},
    {"id": "sapi:Microsoft Huihui Desktop", "name": "慧慧（离线）", "gender": "女",
     "tag": "断网也能用，机械感明显", "engine": "sapi"},
]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 情感预设。
# ⚠️ edge 的免费接口**不支持 SSML 的 <mstts:express-as style="...">**（那是 Azure 付费
#    接口才有的），所以这里用「语速 + 音高 + 音量」三者的组合去**近似**情绪色彩。
#    实测 pitch 参数有效（+15Hz 能正常出声），但它是整体升降调，不等于真情感合成。
STYLES = [
    {"id": "neutral",  "name": "自然", "rate": 0,   "pitch": 0,   "volume": 0,   "tag": "不额外调整"},
    {"id": "cheerful", "name": "愉悦", "rate": 12,  "pitch": 14,  "volume": 6,   "tag": "偏亮、偏快"},
    {"id": "gentle",   "name": "温柔", "rate": -8,  "pitch": -3,  "volume": -8,  "tag": "放慢、压低音量"},
    {"id": "serious",  "name": "严肃", "rate": -6,  "pitch": -10, "volume": 0,   "tag": "沉稳、降调"},
    {"id": "sad",      "name": "低沉", "rate": -18, "pitch": -14, "volume": -10, "tag": "慢而低"},
    {"id": "news",     "name": "播报", "rate": 5,   "pitch": -2,  "volume": 3,   "tag": "新闻播报感"},
    {"id": "excited",  "name": "激动", "rate": 20,  "pitch": 18,  "volume": 8,   "tag": "快、高、响"},
]


def style_list() -> list:
    """情感预设清单（给前端/工具用）。"""
    return [dict(s) for s in STYLES]


def _style_meta(sid: str) -> dict:
    for s in STYLES:
        if s["id"] == sid:
            return s
    return STYLES[0]

# 中文语速基准：edge 神经语音约 4.5 字/秒（只用于给用户一个「大概多长」的估计）
_CHARS_PER_SEC = 4.5


def voice_list() -> list:
    """给前端/工具用的音色清单。"""
    out = []
    for v in VOICES:
        item = dict(v)
        item["available"] = True
        if v["engine"] == "edge":
            try:
                import importlib.util
                item["available"] = importlib.util.find_spec("edge_tts") is not None
            except Exception:
                item["available"] = False
        out.append(item)
    return out


def _voice_meta(vid: str) -> dict:
    for v in VOICES:
        if v["id"] == vid:
            return v
    return {"id": vid, "name": vid, "gender": "", "tag": "", "engine": "edge"}


# --------------------------------------------------------------------------
#  文本清洗与分段
# --------------------------------------------------------------------------
def clean_text(text: str, max_chars: int = 20000) -> str:
    """去掉不该念出来的东西（Markdown 记号、代码、URL）。"""
    t = str(text or "")
    t = re.sub(r"```.*?```", "。", t, flags=re.S)                 # 代码块整段不读
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)                   # 图片
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)               # 链接只留文字
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)          # 标题井号
    t = re.sub(r"[*_~]{1,3}", "", t)                             # 加粗/斜体/删除线
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)               # 列表符号
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)                   # 引用
    t = re.sub(r"^\s*\|.*\|\s*$", lambda m: m.group(0).replace("|", "，"),
               t, flags=re.M)                                    # 表格竖线
    t = re.sub(r"^[-—=]{3,}\s*$", "", t, flags=re.M)             # 分隔线
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:max_chars]


_SENT_RE = re.compile(r"(?<=[。！？!?；;…\n])")


def split_text(text: str, limit: int = 900) -> list:
    """按句子切段，每段不超过 limit 字（edge 单次请求别太长，失败也好重试）。"""
    parts, buf = [], ""
    for seg in _SENT_RE.split(text):
        if not seg:
            continue
        while len(seg) > limit:
            parts.append(seg[:limit])
            seg = seg[limit:]
        if len(buf) + len(seg) <= limit:
            buf += seg
        else:
            if buf.strip():
                parts.append(buf)
            buf = seg
    if buf.strip():
        parts.append(buf)
    return [p for p in parts if p.strip()]


def _run_async(coro):
    """在没有事件循环的线程里跑协程；万一已有 loop 就丢到新线程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# --------------------------------------------------------------------------
#  edge 引擎
# --------------------------------------------------------------------------
async def _edge_one(text: str, voice: str, rate: str, volume: str, path: str,
                    pitch: str = "+0Hz") -> None:
    import edge_tts
    c = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
    await c.save(path)


def _edge_synth(text: str, voice: str, rate: str, volume: str, path: str,
                pitch: str = "+0Hz") -> None:
    """分段合成后按字节拼接成单个 mp3。"""
    import edge_tts  # noqa: F401  只做可用性检查，缺包在这里抛
    parts = split_text(text)
    tmpdir = tempfile.mkdtemp(prefix="tts_")
    blobs = []
    for i, p in enumerate(parts):
        fp = os.path.join(tmpdir, f"{i:03d}.mp3")
        last = None
        for attempt in range(3):
            try:
                _run_async(_edge_one(p, voice, rate, volume, fp, pitch))
                if os.path.exists(fp) and os.path.getsize(fp) > 500:
                    break
                raise RuntimeError("音频为空")
            except Exception as e:      # 网络抖动重试 3 次
                last = e
                time.sleep(0.8 * (attempt + 1))
        else:
            raise RuntimeError(f"第 {i + 1} 段合成失败：{last}")
        with open(fp, "rb") as f:
            blobs.append(f.read())
    with open(path, "wb") as f:
        for b in blobs:
            f.write(b)


# --------------------------------------------------------------------------
#  SAPI 离线引擎
# --------------------------------------------------------------------------
def _sapi_one(text: str, voice_name: str, rate_int: int, path: str) -> None:
    import win32com.client  # type: ignore
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(path, 3)              # 3 = SSFMCreateForWrite
    try:
        if voice_name:
            for v in speaker.GetVoices():
                if voice_name.lower() in str(v.GetDescription()).lower():
                    speaker.Voice = v
                    break
        speaker.Rate = max(-10, min(10, int(rate_int)))
        speaker.AudioOutputStream = stream
        speaker.Speak(text)
    finally:
        try:
            stream.Close()
        except Exception:
            pass


def _sapi_synth(text: str, voice_id: str, rate_int: int, path: str) -> None:
    """分段合成 wav，再用 wave 重写成一个完整 wav。"""
    voice_name = voice_id.split("sapi:", 1)[1] if voice_id.startswith("sapi:") else ""
    parts = split_text(text, limit=700)
    tmpdir = tempfile.mkdtemp(prefix="tts_sapi_")
    files = []
    for i, p in enumerate(parts):
        fp = os.path.join(tmpdir, f"{i:03d}.wav")
        _sapi_one(p, voice_name, rate_int, fp)
        if not os.path.exists(fp) or os.path.getsize(fp) < 200:
            raise RuntimeError(f"第 {i + 1} 段离线合成失败")
        files.append(fp)

    params = None
    with wave.open(path, "wb") as out:
        for fp in files:
            with wave.open(fp, "rb") as r:
                if params is None:
                    params = r.getparams()
                    out.setparams(params)
                out.writeframes(r.readframes(r.getnframes()))


# --------------------------------------------------------------------------
#  对外入口
# --------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", str(name or "")).strip().strip(".")
    return name[:60] or ("tts_" + time.strftime("%Y%m%d_%H%M%S"))


def synth(text: str, voice: str = "", rate: int = 0, engine: str = "auto",
          filename: str = "", pitch: int = 0, style: str = "") -> dict:
    """把文本合成音频。

    rate:  语速百分比，0 为原速，20 = 快 20%，-15 = 慢 15%
    pitch: 音高偏移（Hz），0 为原调，+15 更亮，-12 更沉（仅 edge 引擎有效）
    style: 情感预设 id（见 STYLES），会在 rate/pitch/volume 上叠加偏移
    engine: auto / edge / sapi
    """
    t = clean_text(text)
    if not t:
        return {"ok": False, "error": "文本是空的（或只有代码/符号，没有可读内容）"}

    os.makedirs(OUT_DIR, exist_ok=True)
    vid = (voice or DEFAULT_VOICE).strip()
    meta = _voice_meta(vid)
    if engine == "auto" and meta.get("engine") == "sapi":
        engine = "sapi"
    if engine == "auto":
        engine = "edge"

    rate = max(-50, min(80, int(rate or 0)))
    pitch = max(-50, min(50, int(pitch or 0)))
    st = _style_meta(style) if style else None
    if st and st["id"] != "neutral":
        rate = max(-50, min(80, rate + int(st["rate"])))
        pitch = max(-50, min(50, pitch + int(st["pitch"])))
    vol_pct = int(st["volume"]) if st else 0
    base = _safe_name(filename) if filename else (
        "tts_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4])

    warnings = []
    path, used = "", ""

    if engine == "edge" and not vid.startswith("sapi:"):
        try:
            path = os.path.join(OUT_DIR, base + ".mp3")
            _edge_synth(t, vid, f"{rate:+d}%", f"{vol_pct:+d}%", path, f"{pitch:+d}Hz")
            if os.path.getsize(path) < 1000:
                raise RuntimeError("生成的音频过小")
            used = "edge"
        except Exception as e:
            path = ""
            warnings.append(f"在线语音失败（{type(e).__name__}: {str(e)[:120]}），已改用离线语音")

    if not path:
        # 离线兜底：换成本机中文 SAPI 音色
        sapi_id = next((v["id"] for v in VOICES if v["engine"] == "sapi"), "")
        if not sapi_id:
            return {"ok": False, "error": "；".join(warnings) or "没有可用的语音引擎"}
        try:
            path = os.path.join(OUT_DIR, base + ".wav")
            _sapi_synth(t, sapi_id, int(rate / 10), path)
            used = "sapi"
            if not vid.startswith("sapi:"):
                warnings.append(f"实际使用离线音色「{_voice_meta(sapi_id)['name']}」")
        except Exception as e:
            return {"ok": False, "error": "；".join(warnings + [
                f"离线语音也失败（{type(e).__name__}: {str(e)[:120]}）"])}

    size = os.path.getsize(path)
    name = os.path.basename(path)
    from urllib.parse import quote
    return {
        "ok": True,
        "engine": used,
        "path": path,
        "name": name,
        "url": "/api/tts/audio/" + quote(name),
        "bytes": size,
        "chars": len(t),
        "est_seconds": round(len(t) / _CHARS_PER_SEC, 1),
        "voice": vid if used == "edge" else "sapi",
        "voice_name": _voice_meta(vid)["name"] if used == "edge" else _voice_meta(
            next((v["id"] for v in VOICES if v["engine"] == "sapi"), ""))["name"],
        "rate": rate,
        "pitch": pitch,
        "style": (st["id"] if st else "neutral"),
        "style_name": (st["name"] if st else "自然"),
        "warnings": warnings,
    }


def audio_path(name: str) -> str:
    """按文件名取音频绝对路径（防目录穿越）。"""
    name = os.path.basename(str(name or ""))
    p = os.path.join(OUT_DIR, name)
    if not os.path.exists(p):
        return ""
    return p


# --------------------------------------------------------------------------
#  音频库（界面「🎵 音频」栏用的）
# --------------------------------------------------------------------------
_AUD_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac")


def list_audio() -> list:
    """列出 data/tts 下的音频，新的排前面。"""
    from urllib.parse import quote
    os.makedirs(OUT_DIR, exist_ok=True)
    out = []
    try:
        names = os.listdir(OUT_DIR)
    except OSError:
        return out
    for fn in names:
        if not fn.lower().endswith(_AUD_EXT):
            continue
        p = os.path.join(OUT_DIR, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": fn,
            "bytes": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "mtime": st.st_mtime,
            "url": "/api/tts/audio/" + quote(fn),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def delete_audio(name: str) -> dict:
    """删除一个音频（只删 OUT_DIR 里的，防目录穿越）。"""
    p = audio_path(name)
    if not p:
        return {"ok": False, "error": "文件不存在"}
    try:
        os.remove(p)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def rename_audio(name: str, new_name: str) -> dict:
    """重命名（保留原扩展名）。"""
    from urllib.parse import quote
    p = audio_path(name)
    if not p:
        return {"ok": False, "error": "文件不存在"}
    ext = os.path.splitext(name)[1] or ".mp3"
    stem = _safe_name(new_name)
    if not stem:
        return {"ok": False, "error": "新名字是空的"}
    if not stem.lower().endswith(ext.lower()):
        stem += ext
    dst = os.path.join(OUT_DIR, stem)
    if os.path.abspath(dst) != os.path.abspath(p) and os.path.exists(dst):
        return {"ok": False, "error": "同名文件已存在"}
    try:
        os.rename(p, dst)
        return {"ok": True, "name": stem, "url": "/api/tts/audio/" + quote(stem)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
