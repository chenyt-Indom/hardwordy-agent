"""Wav2Lip 驱动的口型同步数字人

与 avatar2 的区别：
  avatar2      —— 几何形变（拉伸嘴部），单图没有口腔信息，口型天然受限
  avatar_lipsync —— Wav2Lip 生成式模型，直接生成含牙齿/口腔的真实嘴型

复用 avatar2 的：TTS、音频转码、ffmpeg 编码、视频目录
新增：SCRFD 检测 + Wav2Lip ONNX 推理 + 眨眼/头动层
"""
import os
import subprocess
import time
import uuid

import numpy as np
import cv2

from . import avatar2 as A2
from . import wav2lip_engine as W2L
from .face_detect_scrfd import SCRFD

_MODELS = os.path.join(A2._root(), "data", "models", "wav2lip")
_ENGINE = None
_DET = None


def _paths():
    return {
        "onnx": os.path.join(_MODELS, "wav2lip_gan.onnx"),
        "det": os.path.join(_MODELS, "scrfd_2.5g_bnkps.onnx"),
        "mel": os.path.join(_MODELS, "_mel_basis.npy"),
        "sr": os.path.join(_MODELS, "bigface_x4.onnx"),
    }


def available() -> bool:
    p = _paths()
    return os.path.isfile(p["onnx"]) and os.path.isfile(p["det"])


def _engine():
    global _ENGINE, _DET
    if _ENGINE is None:
        p = _paths()
        _DET = SCRFD(p["det"])
        # 注：4x 超分(bigface_x4.onnx)实测在合成阶段是负收益
        # （嘴部最终显示尺寸远小于 384，超分高频被缩放与细节回注吃掉），故不启用
        _ENGINE = W2L.Wav2LipEngine(p["onnx"], _DET, mel_npy=p["mel"], threads=8)
    return _ENGINE


def info() -> dict:
    p = _paths()
    ok = available()
    d = {
        "available": ok,
        "engine": "wav2lip_gan (ONNX)",
        "model_dir": _MODELS,
        "files": {k: (os.path.isfile(v) and os.path.getsize(v)) or 0 for k, v in p.items()},
        "fps": W2L.FPS,
        "note": "生成式口型同步：直接生成嘴部像素，含口腔结构",
    }
    if ok:
        try:
            d["onnx_inputs"] = [i.name for i in _engine().sess.get_inputs()]
        except Exception as e:                          # noqa: BLE001
            d["available"] = False
            d["load_error"] = "%s: %s" % (type(e).__name__, e)
    return d


# ------------------------------------------------------------------ 动作层
def _energy_curve(y, sr, n, fps):
    """每帧音频能量（0..1），让头跟着说话节奏动，而不是机械匀速摆。"""
    e = np.zeros(n, np.float32)
    if n <= 0 or len(y) == 0:
        return e
    for i in range(n):
        a = int(i / float(fps) * sr)
        b = int((i + 1) / float(fps) * sr)
        if b > a and a < len(y):
            seg = np.asarray(y[a:min(b, len(y))], np.float32)
            if seg.size:
                e[i] = float(np.sqrt(np.mean(seg * seg)))
    ref = float(np.percentile(e, 95)) if n > 1 else float(e.max())
    if ref < 1e-6:
        return e
    return np.clip(e / ref, 0.0, 1.0)


def _apply_motion(frames, img_path, proc_side, y, sr, fps, blink, motion):
    """在 Wav2Lip 生成帧上叠加头部动作。

    优先走 25D 深度位移（3DMM 拟合出真实深度图，按头姿做位移 → 有真正的
    转动和体积感）；3D 模型缺失或拟合失败时，回退到旧的「整体平移 + 眼带
    压缩」，保证功能不会因为缺模型直接挂掉。
    """
    n = len(frames)
    if n == 0:
        return frames, "none"
    m = float(max(0.0, min(2.0, motion)))

    prep = None
    poses = None
    try:
        from . import avatar25d as A25
        if A25.available():
            prep = A25._prep3d(img_path, max_side=proc_side)
            poses = A25._pose_curve(n, fps, _energy_curve(y, sr, n, fps))
    except Exception:                                   # noqa: BLE001
        prep, poses = None, None

    if prep is None or poses is None:
        # ---- 回退：旧的平移 + 眼带压缩（无旋转，但保证能动）
        out = []
        for i, fr in enumerate(frames):
            t = i / float(fps)
            if m > 0.01:
                fr = _head_move(fr, (0, 0, fr.shape[1], fr.shape[0]), t,
                                amp_x=9.0 * m, amp_y=6.0 * m)
            if blink:
                fr = _blink_frame(fr, (0, 0, fr.shape[1], fr.shape[0]), t)
            out.append(fr)
        return out, "pan"

    # ---- 25D：旋转 + 平移 + 缩放 + 眨眼（眼睛位置来自 3DMM，比固定眼带准）
    # 增益：25D 那套常量是为「伪3D 全屏」调的，套到 Wav2Lip 成片上视觉复核
    # 判定「几乎静止」。实测 ±13px 只是画面 1.3%，而真人说话头部摆动是
    # 1.5~3%，所以这里乘 1.3 补上。pitch 已有独立 boost，不再重复乘。
    out = []
    for i, fr in enumerate(frames):
        yaw, pit, rol, tx, ty, scl, bl = poses[i]
        yaw *= m * MOTION_GAIN
        pit *= m * PITCH_BOOST
        rol *= m * MOTION_GAIN * ROLL_DAMP
        tx *= m * MOTION_GAIN
        ty *= m * MOTION_GAIN
        scl = 1.0 + (scl - 1.0) * m
        # 单图侧面没有纹理，旋转超过 ~7.5° 就开始露馅，这里兜一道
        yaw = max(-7.5, min(7.5, yaw))
        rol = max(-3.2, min(3.2, rol))
        if m <= 0.01:
            out.append(fr)
            continue
        fr = A25._warp(fr, prep, yaw=yaw, pitch=pit, roll=rol,
                       tx=tx, ty=ty, scl=scl, iters=1)
        if blink and bl > 0.02:
            # roll / scl 必须一起传：侧倾 4° 时脸缘能差 14px，漏传就是眨眼偏位
            fr = A25._blink(fr, prep, min(1.0, bl * max(m, 0.5)),
                            yaw, pit, tx, ty, rol, scl)
        out.append(fr)
    return out, "25d"


# 点头（pitch）在正脸照片上的透视变化天然很弱（实测 pitch=10° 全图差仅 2.0，
# 而 yaw=6° 有 1.06 但左右不对称、肉眼立刻能看出）。所以给 pitch 单独加一档
# 增益，让「说话时点头」这个最重要的动作看得出来。
PITCH_BOOST = 2.0

# 平移 / 左右转 / 侧倾的整体增益（pitch 不乘这个，它有自己的 boost）
MOTION_GAIN = 1.3

# 侧倾（roll）单独降幅。yaw/pitch 是「深度视差」，观感自然；roll 是整块旋转，
# 支点再准也会让脖子跟着摆。实测 roll 从 4°→5.2°（乘 MOTION_GAIN 后）时，
# 脖子带 12.74% 的像素变化超过 8 灰度级 —— 视觉上就是「脖子一直扭」。
# 真人说话时头部侧倾极少超过 3°，所以这里单独压到 0.62 倍、上限 3.2°。
ROLL_DAMP = 0.62


def _blink_frame(frame, face_box, t, period=3.2, dur=0.18):
    """在眼部做上下位移模拟眨眼（只压缩眼裂，不改五官）"""
    x1, y1, x2, y2 = face_box
    h = y2 - y1
    # 眼带：人脸框 28%~46% 高度
    ey1 = int(y1 + h * 0.28)
    ey2 = int(y1 + h * 0.46)
    if ey2 - ey1 < 4:
        return frame
    # 眨眼相位
    ph = (t % period) / period
    blink_center = 0.82
    if not (blink_center <= ph <= blink_center + dur / period):
        return frame
    k = (ph - blink_center) / (dur / period)     # 0..1
    close = np.sin(np.pi * k)                    # 0->1->0
    band = frame[ey1:ey2, x1:x2]
    bh = band.shape[0]
    if bh < 6:
        return frame
    # 垂直压缩 + 轻微下移，模拟眼皮落下
    new_h = max(2, int(bh * (1.0 - 0.45 * close)))
    comp = cv2.resize(band, (band.shape[1], new_h), interpolation=cv2.INTER_LINEAR)
    out = frame.copy()
    out[ey1:ey2, x1:x2] = band                # 先保留原样
    top = ey1 + int((bh - new_h) * 0.35)
    out[top:top + new_h, x1:x2] = comp
    return out


def _head_move(frame, box, t, amp_x=9.0, amp_y=6.0):
    """整体亚像素级平移，让头部有呼吸感（边缘镜像填充，不留黑边）"""
    dx = amp_x * np.sin(2 * np.pi * t / 5.3) + 2.2 * np.sin(2 * np.pi * t / 2.1)
    dy = amp_y * np.sin(2 * np.pi * t / 6.7 + 1.1) + 1.4 * np.sin(2 * np.pi * t / 3.3)
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0]),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REFLECT_101)


# ------------------------------------------------------------------ 主流程
def render(img_path, wav_path, out_mp4, blink=True, head=True, fps=W2L.FPS,
           on_progress=None, max_side=1600, motion=1.0, mode="auto", text="",
           amp=None):
    exe = A2.ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}
    if not available():
        return {"ok": False, "error": "Wav2Lip 模型未就绪，请先下载模型"}

    img = cv2.imread(img_path)
    if img is None:
        return {"ok": False, "error": "形象图读取失败"}

    # warp 模式必须用「本身张着嘴」的源图 —— 它的原理是把嘴压小，
    # 源图要是闭着的就没东西可压（会退化成 open_from_closed 的几何外推，
    # 质量差一档）。而 render_source() 在有闭嘴基准时会优先返回闭嘴图，
    # 所以这里对 warp 单独换回原图。
    if str(mode).lower() == "warp":
        try:
            alt_p = A2._img_path()
            if os.path.isfile(alt_p) and os.path.abspath(alt_p) != os.path.abspath(img_path):
                alt = cv2.imread(alt_p)
                if alt is not None:
                    from . import avatar_mouth as _AM
                    _g = _AM.analyze(alt, A2.mouth())
                    if _g and _g.get("source_open"):
                        img_path, img = alt_p, alt
        except Exception:                               # noqa: BLE001
            pass
    # 尺寸策略（两条线分开，很重要）：
    #   · 处理尺寸 proc —— Wav2Lip 只在 96x96 上生成嘴部，喂给它更大的图**不会**
    #     增加嘴部信息量，反而让脸框更大、贴回时放大倍数更高。所以处理尺寸
    #     取 min(用户选择, 原图长边)，绝不上采样原图。
    #   · 输出尺寸 out —— 用户要的画面尺寸，靠渲染完再放大（纯插值）。
    # 结果：选 1536 得到更大的画面，但嘴部细节仍由 96x96 决定 —— 这点已在
    # 前端选项文案里如实写明，不吹成"更清晰"。
    H, W = img.shape[:2]
    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        max_side = 1024
    max_side = max(480, min(1920, max_side))
    proc_side = min(max_side, max(H, W))
    if max(H, W) > proc_side:
        s = proc_side / float(max(H, W))
        img = cv2.resize(img, (int(W * s) // 2 * 2, int(H * s) // 2 * 2),
                         interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]

    sr, y = A2._read_wav(wav_path)
    if sr != W2L.SR:
        # 重采样到 16k（线性插值，够用）
        n_new = int(len(y) * W2L.SR / sr)
        y = np.interp(np.linspace(0, len(y) - 1, n_new),
                      np.arange(len(y)), y).astype(np.float32)
        sr = W2L.SR
    dur = len(y) / float(sr)
    if dur < 0.2:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}
    if dur > A2.MAX_SECONDS:
        return {"ok": False, "error": "音频 %.1f 秒，超过 %d 秒上限" % (dur, A2.MAX_SECONDS)}

    eng = _engine()
    # 卡通 / 非人类形象（实测 SCRFD 返回 0 个框）：退回用「用户框出来的
    # 嘴部」反推一个裁剪框。这样任何形象都能生成，不再直接报「未检测到人脸」。
    box_override = None
    # 低配机保底：onnxruntime 不兼容 / 模型缺失时 detect 会抛异常（实测
    # 'InferenceSession' object has no attribute 'is_webgpu_graph_capture_enabled'）。
    # 卡通形象本来就不需要人脸框 —— 捕获后按「0 个框」处理，自动走 warp。
    try:
        dets, _ = eng.det.detect(img, thresh=0.5)
    except Exception:                                   # noqa: BLE001
        dets = np.zeros((0, 5), np.float32)
    # 合成模式：auto = 能检测到人脸就走 Wav2Lip 贴回，检测不到（卡通/3D）就走
    # 原图像素形变。实测卡通形象贴 96x96 像素时贴回区清晰度只有 400~680
    # （原图 3751），形变路径 3200~4500，而且画风 100% 一致。
    use_mode = str(mode or "auto").lower()
    if use_mode not in ("w2l", "warp"):
        use_mode = "w2l" if len(dets) > 0 else "warp"
    if len(dets) == 0:
        box_override = A2.face_box_from_mouth((W, H))
        if box_override is None:
            return {"ok": False,
                    "error": "没检测到人脸，也没标定嘴部位置 —— 先在形象图上框一下嘴"}
    # ---- LLM 按这张形象给一套渲染参数 ----
    # 椭圆大小、羽化、纹理回注、口腔加深、圆唇幅度、时间平滑、开合增益
    # 这十几个值原来都是照真人正脸写死的，换一张 3D 卡通就有大半不合适。
    # 这里让多模态 LLM 看图直接给（结果按「图片 sha + 嘴部几何」缓存，
    # 同一张形象只调一次；云端不通就退回预设，不会挡住生成）。
    tune = None
    try:
        from . import avatar_mouth as _AMt
        from . import avatar_tune as _ATt
        _geom_t = _AMt.analyze(img, A2.mouth())
        tune = _ATt.tune(img_path, geom=_geom_t,
                         subject=("human" if len(dets) > 0 else "cartoon"))
    except Exception as _e:                             # noqa: BLE001
        tune = {"ok": False, "params": None, "source": "preset",
                "reason": "调参异常：%s" % _e}

    # ---- 用户手动「口型幅度」覆盖 LLM 的 open_gain ----
    # 用户既然亲手拖了滑块，就该以他为准（AI 再准也不如人眼当下要的效果）。
    # 只覆盖这一项，其余十几项仍由 LLM 按形象给。
    if amp is not None:
        try:
            _amp = float(amp)
        except (TypeError, ValueError):
            _amp = None
        if _amp is not None and _amp > 0:
            _amp = min(2.0, max(0.6, _amp))
            _pr = dict((tune or {}).get("params") or {})
            _old = _pr.get("open_gain")
            _pr["open_gain"] = _amp
            tune = dict(tune or {})
            tune["params"] = _pr
            tune["amp_override"] = {"from": _old, "to": _amp}

    t0 = time.time()
    frames, meta = eng.render(img, y.astype(np.float32), box=box_override,
                              mode=use_mode, mouth_cfg=A2.mouth(),
                              tune=(tune or {}).get("params"),
                              text=text)
    t_infer = time.time() - t0
    if not frames:
        return {"ok": False, "error": "没有生成任何帧"}
    box = meta["box"]

    # 动作层
    motion_mode = "none"
    t_motion = 0.0
    if head or blink or motion > 0.01:
        t1 = time.time()
        frames, motion_mode = _apply_motion(
            frames, img_path, proc_side, y, sr, fps, blink,
            motion if head else 0.0)
        t_motion = time.time() - t1

    # 输出放大：处理尺寸之上只做插值放大（libx264 要求宽高偶数）
    scale = max_side / float(max(W, H))
    if scale > 1.001:
        oW = max(2, int(round(W * scale / 2.0)) * 2)
        oH = max(2, int(round(H * scale / 2.0)) * 2)
        frames = [cv2.resize(f, (oW, oH), interpolation=cv2.INTER_CUBIC)
                  for f in frames]
        W, H = oW, oH

    # libx264 + yuv420p 要求宽高都是偶数。上面的偶数对齐只在「放大」分支里做，
    # 原生分辨率（这张形象 787×1400，宽是奇数）会直接让 ffmpeg 断管：
    # 实测报 BrokenPipeError: [Errno 32]，产出 0 字节文件。这里统一补 1px。
    if W % 2 or H % 2:
        frames = [cv2.copyMakeBorder(f, 0, H % 2, 0, W % 2,
                                     cv2.BORDER_REPLICATE) for f in frames]
        H, W = frames[0].shape[:2]

    n = len(frames)
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-",
           "-i", wav_path,
           # 实测（40 帧 1024²，与原帧锐度 54.82 比）：
           #   veryfast/crf20 → 42.02 (76.6%)  153KB   ← 旧设置，掉 23%
           #   medium/crf18   → 51.13 (93.3%)  307KB
           #   medium/crf16   → 53.06 (96.8%)  431KB   ← 现在用的
           #   slow/crf14     → 54.58 (99.6%)  605KB   （更慢，收益小）
           # veryfast 会跳过大量分析，是「画面发糊」的直接来源之一。
           "-c:v", "libx264", "-preset", "medium", "-crf", "16",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, fr in enumerate(frames):
            rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
            proc.stdin.write(rgb.tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                       # noqa: BLE001
                    pass
    except Exception as e:                              # noqa: BLE001
        try:
            proc.kill()
        except Exception:                               # noqa: BLE001
            pass
        return {"ok": False, "error": "编码中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
    err = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s" % (err or b"")[:400].decode("utf-8", "ignore")}
    return {"ok": True, "frames": n, "duration": round(dur, 2),
            "size": [W, H], "path": out_mp4,
            "bytes": os.path.getsize(out_mp4),
            "infer_sec": round(t_infer, 2),
            "motion": motion_mode, "motion_sec": round(t_motion, 2),
            "box": box, "fps": fps,
            "mode": meta.get("mode") or "w2l",
            "mouth_geom": meta.get("mouth_geom"),
            "tune": ({"source": (tune or {}).get("source"),
                      "style": (tune or {}).get("style"),
                      "reason": (tune or {}).get("reason"),
                      "params": (tune or {}).get("params"),
                      "notes": (tune or {}).get("notes"),
                      # 用户手动拖了「口型幅度」时，记下它覆盖了 AI 的哪个值
                      "amp_override": (tune or {}).get("amp_override")}
                     if tune else None)}


def _grab_frames(mp4, n=2):
    """从成片里抽 n 帧（质检用）。抽不出来就返回空列表，不抛异常。"""
    exe = A2.ffmpeg()
    if not exe or not os.path.isfile(mp4):
        return []
    out = []
    tmpdir = os.path.join(os.path.dirname(mp4), "_qa")
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        return []
    try:
        probe = subprocess.run([exe, "-i", mp4], capture_output=True, text=True, timeout=20)
        import re as _re
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", probe.stderr or "")
        dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 2.0
        for i in range(n):
            t = dur * (i + 0.5) / max(1, n)
            p = os.path.join(tmpdir, "qa_%d.jpg" % i)
            subprocess.run([exe, "-y", "-loglevel", "error", "-ss", "%.2f" % t,
                            "-i", mp4, "-vframes", "1", "-q:v", "2", p],
                           capture_output=True, timeout=30)
            if os.path.isfile(p):
                im = cv2.imread(p)
                if im is not None:
                    out.append(im)
    except Exception:                                   # noqa: BLE001
        pass
    finally:
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:                               # noqa: BLE001
            pass
    return out


def speak(text, voice="", rate=None, style="", engine="auto",
          blink=True, head=True, max_side=1600, motion=1.0,
          mode="auto", variants=False, review=True, amp=None):
    """文本 → TTS → 口型 → mp4

    motion:   头部动作强度 0（完全不动）~ 1（标准）~ 1.6（活跃）
    mode:     auto / w2l / warp
    variants: True 时一次生成多个版本（画风优先 + 口型优先），各自质检，
              让用户自己挑。卡通形象下两版差别很大，值得都看一眼。
    review:   是否做质量检查（客观像素指标 + 多模态 LLM）
    amp:      前端「口型幅度」滑块（0.6~2.0，默认 1.2）。语义是
              **最大开口相对源图的倍率**，映射到 open_gain。
              ⚠ 2026-11-15：这个滑块原来是个死控件 —— 前端一直在提交
              amp，但本函数签名里没有它、路由层也没取，值被直接丢掉，
              所以「拉高没效果」。现在接通，并且**用户手动值优先于 LLM**。
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    if not os.path.isfile(A2.render_source()):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图"}
    if not available():
        return {"ok": False, "error": "Wav2Lip 模型未就绪"}

    from . import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=style or "")
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    audio = a.get("path") or ""
    try:
        wav = A2._to_wav16k(audio)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "音频转码失败：%s: %s" % (type(e).__name__, e)}

    src = A2.render_source()
    # 多版本：卡通形象下「原图像素形变」和「贴 Wav2Lip 像素」差别很大
    # （实测综合分 7.0 vs 5.6，嘴部清晰度比 1.31 vs 0.54），值得都生成一遍
    # 让用户自己看。真人形象两版差别小，默认只出一版。
    modes = ["warp", "w2l"] if variants else [mode]
    try:
        from . import avatar_vision as AV
    except Exception:                                   # noqa: BLE001
        AV = None

    # 质检用的几何（objective_score 需要嘴部椭圆）
    try:
        from . import avatar_mouth as AM
        _img = cv2.imread(src)
        _cfg = A2.mouth()
        _geom = AM.analyze(_img, _cfg) if (_img is not None and _cfg) else None
    except Exception:                                   # noqa: BLE001
        _geom = None

    from urllib.parse import quote
    versions = []
    for md in modes:
        name = ("lipsync_" + time.strftime("%Y%m%d_%H%M%S") + "_"
                + uuid.uuid4().hex[:4] + ".mp4")
        out = os.path.join(A2._vdir(), name)
        r = render(src, wav, out, blink=blink, head=head,
                   max_side=max_side, motion=motion, mode=md, amp=amp)
        if not r.get("ok"):
            versions.append({"ok": False, "mode": md, "error": r.get("error")})
            continue
        qa = None
        if review and AV is not None:
            try:
                # 几何必须用 render **实际使用**的那一套：w2l 走闭嘴基准、
                # warp 走原图，两者的嘴部位置/清晰度基线不同，混用会让
                # 「清晰度比」失真（实测混用时 w2l 从 0.54 虚高到 0.98）。
                _g2 = r.get("mouth_geom") or _geom
                if _g2 and "cx" not in _g2 and _geom:
                    _g2 = dict(_geom, **{k: v for k, v in _g2.items() if v is not None})
                qa = AV.review_frames(_grab_frames(out, 2), n=1, geom=_g2)
            except Exception as e:                      # noqa: BLE001
                qa = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        label = ("画风优先（原图像素形变）" if r.get("mode") == "warp"
                 else "口型优先（Wav2Lip 生成像素）")
        versions.append({
            "ok": True, "name": name,
            "url": "/api/avatar2/video/" + quote(name),
            "path": out,
            "mode": r.get("mode"), "label": label,
            "duration": r["duration"], "size": r["size"],
            "frames": r["frames"], "bytes": r["bytes"],
            "infer_sec": r.get("infer_sec"),
            "qa": qa,
            "qa_score": (qa or {}).get("score"),
            "tune": r.get("tune"),
        })

    good = [v for v in versions if v.get("ok")]
    if not good:
        return versions[0] if versions else {"ok": False, "error": "没有生成成功"}
    # 按质检分选最优；都没质检就取第一个
    best = max(good, key=lambda v: (v.get("qa_score") if v.get("qa_score") is not None else -1))
    return {
        "ok": True,
        "name": best["name"], "url": best["url"], "path": best["path"],
        "mode": best["mode"], "label": best["label"],
        "duration": best["duration"], "size": best["size"],
        "frames": best["frames"], "bytes": best["bytes"],
        "infer_sec": best.get("infer_sec"),
        "qa": best.get("qa"),
        "qa_score": best.get("qa_score"),
        "tune": best.get("tune"),
        "versions": versions,
        "blink": bool(blink), "head": bool(head),
        "motion": best.get("motion"), "motion_sec": best.get("motion_sec"),
        # ⚠ 不能写 float(motion or 1.0)：motion=0（静止档）是 falsy，
        #   回显会被悄悄改成 1.0，前端看起来像没生效。
        "motion_level": float(motion) if motion is not None else 1.0,
        "max_side": max(480, min(1920, int(max_side or 1024))),
        "lipsync_label": ("原图像素形变" if best.get("mode") == "warp"
                          else "Wav2Lip 生成式口型"),
        "audio_url": a.get("url") or "",
        "audio_engine": a.get("engine") or "",
        "text": t[:200],
    }
