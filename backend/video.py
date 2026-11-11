# -*- coding: utf-8 -*-
"""视频处理：裁剪 / 拼接 / 配音 / 加字 / 字幕 / 抽音轨 / 图片成片 / 压缩。

底层用 **imageio-ffmpeg 自带的 ffmpeg 可执行文件**（pip 装，约 100 MB），
不依赖系统 PATH —— 用户机器上通常没装 ffmpeg，也不该让他自己去装。

⚠️ Windows 上踩过（或预判到）的三个坑，这里都处理了：

  1. **子进程会闪控制台黑窗**：每次调 ffmpeg 都会弹一个 conhost，一秒钟消失，
     观感很差（用户 2026-11-11 专门报过这个）。所以一律带 CREATE_NO_WINDOW。

  2. **滤镜串里不能直接写 Windows 路径**：drawtext / subtitles 的参数是用
     冒号分隔的，`C:\\Windows\\...` 里的冒号会被当成分隔符切碎；中文路径在
     部分构建上还会编码错乱。做法是：字体、文字、字幕先落到临时目录并改成
     纯 ASCII 短名，再把 cwd 切过去，滤镜里只写相对文件名。

  3. **没有 ffprobe**：imageio-ffmpeg 只给 ffmpeg，不给 ffprobe。
     所以时长/分辨率/有无音轨都靠解析 `ffmpeg -i` 的 stderr 得到。
"""
import os
import re
import subprocess
import tempfile

_WIN = (os.name == "nt")
# CREATE_NO_WINDOW：不弹控制台（见上面坑 1）
_NOWIN = 0x08000000 if _WIN else 0

# drawtext 必须显式给字体文件 —— ffmpeg 内置字体不含中文字形，不给就是一堆方框。
_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑（Win7+ 都有）
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

# 视频输出的公共参数：H.264 + faststart，保证「发给别人也能播」
_VOUT = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
_AOUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def ffmpeg_exe():
    """imageio-ffmpeg 自带的 ffmpeg 路径；没装就返回空串。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if (p and os.path.isfile(p)) else ""
    except Exception:
        return ""


def available():
    return bool(ffmpeg_exe())


def font_file():
    for p in _FONTS:
        if os.path.isfile(p):
            return p
    return ""


def _run(args, cwd=None, timeout=1800):
    exe = ffmpeg_exe()
    if not exe:
        return {"ok": False,
                "error": "本机没有可用的 ffmpeg。装一个就行：pip install imageio-ffmpeg"}
    cmd = [exe, "-hide_banner", "-nostdin", "-y"] + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           creationflags=_NOWIN)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg 超时（超过 %d 秒）" % timeout}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    log = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        return {"ok": False, "error": _tail(log) or ("ffmpeg 返回码 %d" % r.returncode)}
    return {"ok": True, "log": log}


def _tail(log, n=1000):
    """只留最后几行有内容的输出 —— 前面都是版权声明和进度行，喂给模型没用。"""
    lines = [l.strip() for l in (log or "").splitlines() if l.strip()]
    return "\n".join(lines[-8:])[:n]


def info(path):
    """探测时长 / 分辨率 / 帧率 / 有无音轨。靠解析 `ffmpeg -i` 的 stderr。"""
    exe = ffmpeg_exe()
    if not exe:
        return {"ok": False, "error": "本机没有可用的 ffmpeg"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：%s" % path}
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", path],
                           capture_output=True, timeout=120, creationflags=_NOWIN)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    txt = (r.stderr or b"").decode("utf-8", "replace")

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    dur = ((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
           if m else 0.0)

    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    fps = 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
    if m:
        fps = float(m.group(1))

    has_video = "Video:" in txt
    has_audio = bool(re.search(r"Stream #\d+:\d+.*?: Audio:", txt))
    if not has_video and not has_audio:
        return {"ok": False, "error": "这个文件既没有视频流也没有音频流（不是媒体文件？）"}
    return {"ok": True, "duration": round(dur, 2), "width": w, "height": h,
            "fps": fps, "has_video": has_video, "has_audio": has_audio}


def _even(n):
    """H.264 要求宽高是偶数，奇数会直接编码失败。"""
    n = int(n or 0)
    return n - (n % 2) if n >= 2 else 2


# ---------------------------------------------------------------- 裁剪
def trim(src, out, start=0.0, duration=None, end=None):
    """按时间裁剪，**保留原声**。

    `-ss` 放在 `-i` 之后是精确裁剪（解码后丢弃），放在前面是快速定位但会
    从前一个关键帧开始、首尾可能多出几帧。用户说的是「1分23到2分05」这种，
    要的是准，所以选精确模式。
    """
    start = max(0.0, float(start or 0))
    if duration is None and end is not None:
        duration = float(end) - start
    args = ["-i", src]
    if start > 0:
        args += ["-ss", "%.3f" % start]
    if duration and float(duration) > 0:
        args += ["-t", "%.3f" % float(duration)]
    args += _VOUT + _AOUT + [out]
    return _run(args)


# ---------------------------------------------------------------- 拼接
def concat(sources, out, width=None, height=None, fps=30, tmpdir=None):
    """多段拼接。

    不能直接把几段丢给 concat demuxer —— 只有**编码参数完全一致**时它才肯拼，
    手机拍的、剪映导的、录屏的混在一起必然失败。所以先把每段统一转成
    同分辨率 / 同帧率 / 同像素格式的中间文件，再拼。

    音频：全都有音轨就保留，有一段没有就整体去音轨（否则拼接时音画会错位）。
    """
    sources = [p for p in (sources or []) if p and os.path.isfile(p)]
    if len(sources) < 2:
        return {"ok": False, "error": "拼接至少要两个能读到的视频文件"}

    infos = []
    for p in sources:
        i = info(p)
        if not i.get("ok"):
            return {"ok": False, "error": "读不出这段视频：%s（%s）" % (p, i.get("error"))}
        if not i.get("has_video"):
            return {"ok": False, "error": "这个文件里没有画面，不能拼接：%s" % p}
        infos.append(i)

    W = _even(width or infos[0]["width"] or 1280)
    H = _even(height or infos[0]["height"] or 720)
    keep_audio = all(i.get("has_audio") for i in infos)

    tmp = tmpdir or tempfile.mkdtemp(prefix="vidcat_")
    segs = []
    for idx, p in enumerate(sources):
        seg = os.path.join(tmp, "seg_%02d.mp4" % idx)
        # 等比缩放 + 补黑边：宁可留黑边，也不要把画面拉变形
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=%d"
              % (W, H, W, H, int(fps or 30)))
        args = ["-i", p, "-vf", vf] + _VOUT
        args += _AOUT if keep_audio else ["-an"]
        args += [seg]
        r = _run(args)
        if not r.get("ok"):
            return {"ok": False, "error": "第 %d 段转码失败：%s" % (idx + 1, r.get("error"))}
        segs.append(seg)

    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for seg in segs:
            f.write("file '%s'\n" % os.path.basename(seg))

    r = _run(["-f", "concat", "-safe", "0", "-i", "list.txt",
              "-c", "copy", "-movflags", "+faststart", out], cwd=tmp)
    if not r.get("ok"):
        return {"ok": False, "error": "拼接失败：%s" % r.get("error")}
    return {"ok": True, "width": W, "height": H, "audio": keep_audio,
            "count": len(segs)}


# ---------------------------------------------------------------- 音轨
def add_audio(video, audio, out, mode="replace", volume=1.0, loop=True):
    """加背景音乐 / 替换音轨。

    mode=replace：原声整条换掉，只用新音频
    mode=mix    ：原声 + 新音频混合（原视频没声音时自动退化成 replace）
    loop=True   ：音乐比视频短就循环垫满（用 -stream_loop -1），配合 -shortest 截到视频长度
    """
    vi = info(video)
    if not vi.get("ok"):
        return {"ok": False, "error": "读不出视频：%s" % vi.get("error")}
    if not os.path.isfile(audio):
        return {"ok": False, "error": "找不到音频文件：%s" % audio}

    vol = max(0.0, min(4.0, float(volume if volume is not None else 1.0)))
    if mode == "mix" and vi.get("has_audio"):
        fc = ("[0:a]volume=1.0[a0];"
              "[1:a]volume=%.3f[a1];"
              "[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]"
              % vol)
        args = ["-i", video]
        if loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", audio,
                 "-filter_complex", fc,
                 "-map", "0:v:0", "-map", "[a]",
                 "-c:v", "copy"] + _AOUT + ["-shortest", out]
    else:
        # 替换：视频流直接 copy，不重编码 —— 快，且画质零损失
        args = ["-i", video]
        if loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", audio,
                 "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "copy",
                 "-af", "volume=%.3f" % vol] + _AOUT + ["-shortest", out]
    return _run(args)


# ---------------------------------------------------------------- 文字 / 水印
_POS = {
    "top": ("(w-text_w)/2", "{m}"),
    "bottom": ("(w-text_w)/2", "h-text_h-{m}"),
    "center": ("(w-text_w)/2", "(h-text_h)/2"),
    "top-left": ("{m}", "{m}"),
    "top-right": ("w-text_w-{m}", "{m}"),
    "bottom-left": ("{m}", "h-text_h-{m}"),
    "bottom-right": ("w-text_w-{m}", "h-text_h-{m}"),
}


def add_text(video, out, text, position="bottom", fontsize=48, color="white",
             box=True, box_color="black@0.45", opacity=1.0,
             start=None, end=None, margin=60, tmpdir=None):
    """压一行字上去：标题、说明、水印都走这个。

    中文必须靠 textfile 传 —— 直接把中文塞进滤镜串，在 Windows 上会被
    命令行编码搞坏（坑 2）。所以先把文字写成 label.txt，再用 textfile= 引用。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "没有要加的文字"}

    font = font_file()
    if not font:
        return {"ok": False, "error": "找不到可用的中文字体，没法在视频上写字"}

    tmp = tmpdir or tempfile.mkdtemp(prefix="vidtxt_")
    # 字体和文字都复制/落到临时目录，用 ASCII 短名（坑 2）
    fname = "font" + os.path.splitext(font)[1].lower()
    fpath = os.path.join(tmp, fname)
    if not os.path.isfile(fpath):
        try:
            import shutil
            shutil.copyfile(font, fpath)
        except Exception as e:
            return {"ok": False, "error": "字体复制失败：%s" % e}
    with open(os.path.join(tmp, "label.txt"), "w", encoding="utf-8") as f:
        f.write(text)

    pos = _POS.get(str(position or "bottom").strip().lower(), _POS["bottom"])
    x = pos[0].replace("{m}", str(int(margin)))
    y = pos[1].replace("{m}", str(int(margin)))

    op = max(0.0, min(1.0, float(opacity if opacity is not None else 1.0)))
    parts = [
        "fontfile=%s" % fname,
        "textfile=label.txt",
        "expansion=none",              # 别把文字里的 % 当变量展开
        "fontsize=%d" % int(fontsize or 48),
        "fontcolor=%s@%.2f" % (color or "white", op),
        "x=%s" % x,
        "y=%s" % y,
        "line_spacing=8",
    ]
    if box:
        parts += ["box=1", "boxcolor=%s" % (box_color or "black@0.45"),
                  "boxborderw=14"]
    if start is not None or end is not None:
        s = float(start or 0)
        e = float(end) if end is not None else 1e6
        parts.append("enable='between(t,%.2f,%.2f)'" % (s, e))

    vf = "drawtext=" + ":".join(parts)
    args = ["-i", video, "-vf", vf] + _VOUT
    args += _AOUT
    args += [out]
    return _run(args, cwd=tmp)


# ---------------------------------------------------------------- 字幕
def add_subtitle(video, out, srt, fontsize=22, tmpdir=None):
    """烧入 .srt 字幕（硬字幕，任何播放器都显示）。

    用 subtitles 滤镜（libass），路径同样只能给相对名（坑 2）。
    """
    if not os.path.isfile(srt):
        return {"ok": False, "error": "找不到字幕文件：%s" % srt}
    tmp = tmpdir or tempfile.mkdtemp(prefix="vidsub_")
    import shutil
    try:
        shutil.copyfile(srt, os.path.join(tmp, "sub.srt"))
    except Exception as e:
        return {"ok": False, "error": "字幕复制失败：%s" % e}

    style = ("FontName=Microsoft YaHei,FontSize=%d,"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&HA0000000,"
             "BorderStyle=1,Outline=2,Shadow=0,MarginV=40" % int(fontsize or 22))
    vf = "subtitles=sub.srt:force_style='%s'" % style
    args = ["-i", video, "-vf", vf] + _VOUT + _AOUT + [out]
    return _run(args, cwd=tmp)


# ---------------------------------------------------------------- 抽音轨
def extract_audio(video, out, fmt="mp3", bitrate="192k"):
    """把音轨单独导出来（mp3 / m4a / wav）。"""
    vi = info(video)
    if vi.get("ok") and not vi.get("has_audio"):
        return {"ok": False, "error": "这个视频本身没有声音，抽不出音轨"}
    fmt = (fmt or "mp3").lower().lstrip(".")
    if fmt == "mp3":
        args = ["-i", video, "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, out]
    elif fmt in ("m4a", "aac"):
        args = ["-i", video, "-vn", "-c:a", "aac", "-b:a", bitrate, out]
    elif fmt == "wav":
        args = ["-i", video, "-vn", "-c:a", "pcm_s16le", out]
    else:
        return {"ok": False, "error": "不支持导出 %s，只能 mp3 / m4a / wav" % fmt}
    return _run(args)


# ---------------------------------------------------------------- 图片成片
def images_to_video(images, out, seconds=3.0, fps=30, width=1920, height=None,
                    tmpdir=None):
    """把一组图片做成视频（轮播 / 片头）。每张停留 `seconds` 秒。

    ⚠️ height 默认**按 16:9 由 width 推出来**。原来写死 1080，用户只给
       width=1280 时会得到 1280×1080 这种怪异比例（2026-11-11 实测踩到）。
    """
    images = [p for p in (images or []) if p and os.path.isfile(p)]
    if not images:
        return {"ok": False, "error": "没有可用的图片"}

    W = _even(width or 1920)
    H = _even(height or round(W * 9 / 16.0))
    tmp = tmpdir or tempfile.mkdtemp(prefix="vidimg_")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in images:
            # concat demuxer 的路径同样用相对名，避免中文/空格（坑 2）
            f.write("file '%s'\n" % p.replace("\\", "/").replace("'", r"'\''"))
            f.write("duration %.3f\n" % float(seconds or 3.0))
        f.write("file '%s'\n" % images[-1].replace("\\", "/").replace("'", r"'\''"))

    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=%d"
          % (W, H, W, H, int(fps or 30)))
    args = ["-f", "concat", "-safe", "0", "-i", "list.txt",
            "-vf", vf] + _VOUT + ["-an", out]
    r = _run(args, cwd=tmp)
    if not r.get("ok"):
        return {"ok": False, "error": "图片合成视频失败：%s" % r.get("error")}
    return {"ok": True, "width": W, "height": H, "count": len(images)}


# ---------------------------------------------------------------- 压缩
def compress(src, out, crf=28, width=None, preset="medium"):
    """压缩 / 转码 / 改分辨率。crf 越大文件越小、画质越差（18~32 常用）。"""
    crf = max(0, min(51, int(crf if crf is not None else 28)))
    vf = []
    if width:
        vf.append("scale=%d:-2" % _even(width))
    args = ["-i", src]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"] + _AOUT + [out]
    return _run(args)
