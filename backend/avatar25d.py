"""伪3D 数字人（2.5D）：深度图位移 + 音频驱动头姿 + 眨眼。

和另两条路的区别（都是实测踩出来的）：
  · 纯 2D 形变（avatar2）：只有嘴在动，整张脸静止 → 僵硬
  · 真 3D 网格（avatar3d）：纹理贴到 38365 顶点上重画，侧面没纹理、边缘硬切、
    口腔靠程序化绘制一律「贴纸感」→ 用户判定「太拉胯」
  · 伪3D（本模块）：用 3DMM 拟合出真实深度图，对**原图像素**做位移。
    原图的眼睛、皮肤纹理、光照全部原样保留，只是位置按深度挪动 →
    既有体积感，又不会出现重绘痕迹。

核心：位移(px) = Δ深度 × k_px × sin(角度)
  k_px = 脸宽 / 83.9（BFM 正视图 UV 的 x 跨度 0.699×120=83.9 模型单位铺满脸宽）
"""
import os
import time
import math
import uuid
import subprocess

import numpy as np
import cv2

try:                                        # 包内（服务从项目根启动）
    from . import avatar2 as A2
    from . import face3d as F3
except ImportError:                         # 脚本直跑（backend 目录内）
    import avatar2 as A2
    import face3d as F3

FPS = A2.FPS
_CACHE = {}

# ---------------------------------------------------------------- 头姿参数
# ⚠ 实测教训：只靠旋转做不出「活」的感觉。yaw=1.6° 在 720 画面上只有 3px 位移，
#   测量结果「头部平移量全程 0 种」——肉眼完全看不出在动。
#   所以自然感来自三层叠加：
#     ① 整体平移（安全，不会露馅）② 小角度旋转（有体积感）③ 极轻微缩放（前后感）
#   单图没有侧面纹理，旋转超过 ~5° 就开始露馅，所以旋转幅度必须小，
#   靠平移把「在动」这件事撑起来。
YAW_IDLE = 2.6          # 常态微摆（度）
PITCH_IDLE = 1.8
ROLL_IDLE = 1.2
YAW_SPEECH = 4.4        # 说话时额外的左右幅度
PITCH_SPEECH = 3.6      # 说话时点头幅度
ROLL_SPEECH = 2.0
GAIN = 0.35             # 视差增益（1.0 = 物理正确，但单图会拉扯，0.35 最自然）

# 整体位移 / 缩放（720 画面上约 ±5px、±0.8%）
TX_IDLE = 5.0
TY_IDLE = 3.6
TZ_IDLE = 0.008
TX_SPEECH = 2.6
TY_SPEECH = 3.0


def available() -> bool:
    return F3.available()


# ---------------------------------------------------------------- 预处理
def _prep3d(img_path: str) -> dict:
    """一次性算好深度图 / 权重 / 人脸几何（每段视频只做一次）。"""
    from PIL import Image
    if not F3.available():
        raise RuntimeError("3D 模型文件不全（data/models/face3d/）")

    # ⚠ 必须和 A2._prep 用同一个尺寸基准（MAX_SIDE=720），否则 _frame 输出的
    #   720×720 和这里按原图 1024 算的 ROI 对不上，warp 会直接广播报错。
    im = Image.open(img_path)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    W0, H0 = A2._fit_size(im.width, im.height)
    if (W0, H0) != im.size:
        im = im.resize((W0, H0), Image.LANCZOS)
    img = np.asarray(im.convert("RGB"))
    H, W = img.shape[:2]
    f3 = F3.get()

    det = f3._detector(W, H)
    _n, faces = det.detect(img)
    if faces is None or len(faces) == 0:
        raise RuntimeError("形象图里没检测到人脸")
    faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
    lm = faces[0][4:14].reshape(5, 2).astype(np.float32)
    M, _ = cv2.estimateAffinePartial2D(lm, F3.TEMPLATE5, method=cv2.LMEDS)
    Minv = cv2.invertAffineTransform(M)

    r = f3.build(img)
    if r is None:
        raise RuntimeError("3D 拟合失败")
    uv = r["uv"]
    z = r["pts_front"][2]
    tri = f3.tri

    # 120 归一化坐标 → 原图像素
    p120 = (uv * 120.0).astype(np.float32)
    ones = np.ones((len(p120), 1), np.float32)
    P = (np.hstack([p120, ones]) @ Minv.T).astype(np.float32)

    depth, mask = _raster_depth(P, z, tri, W, H, DS=512)
    if (mask > 0).mean() < 0.01:
        raise RuntimeError("深度图覆盖太小，拟合不可靠")

    # 补洞 + 平滑（深度图有亚像素三角形造成的针孔）
    dep = cv2.inpaint(np.where(mask > 0, depth, 0).astype(np.float32),
                      (mask == 0).astype(np.uint8), 3, cv2.INPAINT_NS)
    dep = cv2.GaussianBlur(dep, (0, 0), 3.0)
    dep = cv2.resize(dep, (W, H), interpolation=cv2.INTER_LINEAR)
    mk = cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST)

    ys, xs = np.where(mk > 0)
    cx, cy = float(xs.mean()), float(ys.mean())
    fw = float(xs.max() - xs.min())
    fh = float(ys.max() - ys.min())
    zin = dep[mk > 0]
    z0 = float(np.median(zin))
    zspan = max(1e-6, float(np.percentile(zin, 98) - np.percentile(zin, 2)))

    zn = np.clip((dep - z0) / zspan, -1.0, 1.0).astype(np.float32)
    # 位移权重：脸内 1，向外渐变到 0，否则脖子/肩膀会被撕开
    wgt = cv2.GaussianBlur((mk > 0).astype(np.float32), (0, 0), 34.0)
    wgt = np.clip(wgt / max(float(wgt.max()), 1e-6), 0.0, 1.0)

    k_px = fw / 83.9
    scale = zspan * k_px * GAIN

    # 只对脸部 ROI 做 remap，省 80% 的计算
    pad = int(max(fw, fh) * 0.18) + 40
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(W, int(xs.max()) + pad)
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(H, int(ys.max()) + pad)

    return {"img": img, "wh": (W, H), "zn": zn, "wgt": wgt,
            "mask": mk, "scale": scale,
            "cx": cx, "cy": cy, "fw": fw, "fh": fh,
            "eyes": [(float(lm[0][0]), float(lm[0][1])),
                     (float(lm[1][0]), float(lm[1][1]))],
            "eye_gap": float(np.linalg.norm(lm[0] - lm[1])),
            "roi": (x0, y0, x1, y1), "zspan": zspan}


def _raster_depth(P, z, tri, W, H, DS=512):
    """把三角面光栅化成深度图（向量化 z-buffer，最近片元胜出）。"""
    k = DS / float(max(W, H))
    xy = (P * k).astype(np.float32)
    T = tri.T
    a, b, c = xy[T[:, 0]], xy[T[:, 1]], xy[T[:, 2]]
    area = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
            - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    keep = np.abs(area) > 1e-6
    T, a, b, c = T[keep], a[keep], b[keep], c[keep]

    lo = np.floor(np.minimum(np.minimum(a, b), c)).astype(np.int32)
    hi = np.ceil(np.maximum(np.maximum(a, b), c)).astype(np.int32) + 1
    lo = np.clip(lo, 0, DS)
    hi = np.clip(hi, 0, DS)
    bw = hi[:, 0] - lo[:, 0]
    bh = hi[:, 1] - lo[:, 1]
    ok = (bw >= 1) & (bh >= 1)
    T, a, b, c, lo, bw, bh = T[ok], a[ok], b[ok], c[ok], lo[ok], bw[ok], bh[ok]
    if len(T) == 0:
        return np.zeros((DS, DS), np.float32), np.zeros((DS, DS), np.uint8)

    counts = (bw * bh).astype(np.int64)
    total = int(counts.sum())
    if total <= 0:
        return np.zeros((DS, DS), np.float32), np.zeros((DS, DS), np.uint8)
    tid = np.repeat(np.arange(len(T)), counts)
    start = np.repeat(np.cumsum(counts) - counts, counts)
    local = np.arange(total, dtype=np.int64) - start
    px = lo[tid, 0] + (local % bw[tid])
    py = lo[tid, 1] + (local // bw[tid])

    v0, v1, v2 = a[tid], b[tid], c[tid]
    den = ((v1[:, 1] - v2[:, 1]) * (v0[:, 0] - v2[:, 0])
           + (v2[:, 0] - v1[:, 0]) * (v0[:, 1] - v2[:, 1]))
    # 退化三角形（den≈0）会让重心坐标炸掉，插值出 -10 万这种荒谬深度
    good = np.abs(den) > 0.02
    den = np.where(good, den, 1.0)
    fx = px.astype(np.float32) + 0.5
    fy = py.astype(np.float32) + 0.5
    w0 = ((v1[:, 1] - v2[:, 1]) * (fx - v2[:, 0]) + (v2[:, 0] - v1[:, 0]) * (fy - v2[:, 1])) / den
    w1 = ((v2[:, 1] - v0[:, 1]) * (fx - v2[:, 0]) + (v0[:, 0] - v2[:, 0]) * (fy - v2[:, 1])) / den
    w2 = 1.0 - w0 - w1
    inside = good & (w0 >= -0.02) & (w1 >= -0.02) & (w2 >= -0.02)
    if not inside.any():
        return np.zeros((DS, DS), np.float32), np.zeros((DS, DS), np.uint8)

    tid_i, px_i, py_i = tid[inside], px[inside], py[inside]
    b0, b1, b2 = w0[inside], w1[inside], w2[inside]
    zi = (z[T[:, 0]][tid_i] * b0 + z[T[:, 1]][tid_i] * b1
          + z[T[:, 2]][tid_i] * b2)

    flat = py_i * DS + px_i
    order = np.lexsort((zi, flat))
    fs = flat[order]
    first = np.ones(len(fs), bool)
    first[1:] = fs[1:] != fs[:-1]
    sel = order[first]
    fi = flat[sel]

    depth = np.zeros((DS, DS), np.float32)
    mask = np.zeros((DS, DS), np.uint8)
    depth[fi // DS, fi % DS] = zi[sel]
    mask[fi // DS, fi % DS] = 255
    return depth, mask


# ---------------------------------------------------------------- 伪3D 变换
def _warp(img, prep, yaw=0.0, pitch=0.0, roll=0.0,
          tx=0.0, ty=0.0, scl=1.0):
    """按头姿做反向映射。正向：源 → 旋转 → 缩放 → 平移 → 目标。"""
    if (abs(yaw) < 0.01 and abs(pitch) < 0.01 and abs(roll) < 0.01
            and abs(tx) < 0.05 and abs(ty) < 0.05 and abs(scl - 1.0) < 0.0005):
        return img
    W, H = prep["wh"]
    cx, cy = prep["cx"], prep["cy"]
    scale = prep["scale"]
    x0, y0, x1, y1 = prep["roi"]
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img

    ry, rx, rz = math.radians(yaw), math.radians(pitch), math.radians(roll)
    cyw, syw = math.cos(ry), math.sin(ry)
    cxp, sxp = math.cos(rx), math.sin(rx)
    cyw = max(cyw, 0.55)
    cxp = max(cxp, 0.55)

    u, v = np.meshgrid(np.arange(x0, x1, dtype=np.float32),
                       np.arange(y0, y1, dtype=np.float32))

    # 1) 撤销 roll（绕脸中心反向旋转）
    if abs(rz) > 1e-6:
        cr, sr = math.cos(-rz), math.sin(-rz)
        du, dv = u - cx, v - cy
        u, v = cx + du * cr - dv * sr, cy + du * sr + dv * cr

    # 2) 迭代求源坐标：透视压缩 + 深度视差（两次足够收敛）
    zn = prep["zn"]
    wg = prep["wgt"]
    su, sv = u.copy(), v.copy()
    for _ in range(2):
        zs = cv2.remap(zn, su, sv, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        ws = cv2.remap(wg, su, sv, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        su = cx + (u - cx) / cyw - scale * zs * syw * ws / cyw
        sv = cy + (v - cy) / cxp - scale * zs * sxp * ws / cxp

    sub = img[y0:y1, x0:x1]
    out = img.copy()
    out[y0:y1, x0:x1] = cv2.remap(sub, su - x0, sv - y0, cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
    return out


def _shift_of(prep, x, y, yaw=0.0, pitch=0.0):
    """某点在新姿态下被挪到哪（用来让嘴/眼跟着头走）。"""
    if abs(yaw) < 0.01 and abs(pitch) < 0.01:
        return 0.0, 0.0
    ry, rx = math.radians(yaw), math.radians(pitch)
    cyw = max(math.cos(ry), 0.55)
    cxp = max(math.cos(rx), 0.55)
    cx, cy = prep["cx"], prep["cy"]
    xi = int(np.clip(x, 0, prep["wh"][0] - 1))
    yi = int(np.clip(y, 0, prep["wh"][1] - 1))
    z = float(prep["zn"][yi, xi])
    w = float(prep["wgt"][yi, xi])
    dx = (cx + (x - cx) / cyw - prep["scale"] * z * math.sin(ry) * w / cyw) - x
    dy = (cy + (y - cy) / cxp - prep["scale"] * z * math.sin(rx) * w / cxp) - y
    return dx, dy


def _blink(img, prep, amount, yaw=0.0, pitch=0.0):
    """眨眼：眼部区域纵向压缩 + 压一条眼睑线（不填色块，避免棕色方块）。"""
    if amount <= 0.02:
        return img
    a = float(np.clip(amount, 0.0, 1.0))
    out = img.copy()
    H, W = prep["wh"]
    ew = prep["eye_gap"] * 0.42
    eh = ew * 0.62
    for (ex, ey) in prep["eyes"]:
        dx, dy = _shift_of(prep, ex, ey, yaw, pitch)
        ex, ey = ex + dx, ey + dy
        x0 = int(max(0, ex - ew)); x1 = int(min(W, ex + ew))
        y0 = int(max(0, ey - eh)); y1 = int(min(H, ey + eh))
        if x1 - x0 < 6 or y1 - y0 < 6:
            continue
        roi = out[y0:y1, x0:x1]
        nh = max(2, int(round((y1 - y0) * (1.0 - 0.42 * a))))
        small = cv2.resize(roi, (x1 - x0, nh), interpolation=cv2.INTER_AREA)
        canvas = np.empty_like(roi)
        canvas[:nh] = small
        canvas[nh:] = small[-1:]
        # 边缘羽化，避免出现一条硬边
        m = np.ones((y1 - y0, 1), np.float32)
        m[:2] = 0.4
        m[-2:] = 0.4
        out[y0:y1, x0:x1] = (canvas * m[:, :, None]
                             + roi * (1.0 - m[:, :, None])).astype(np.uint8)
    return out


# ---------------------------------------------------------------- 头姿曲线
def _pose_curve(n, fps, energy):
    """生成每帧的头姿与眨眼。能量来自音频，让头跟着说话节奏动。"""
    out = []
    t = np.arange(n, dtype=np.float32) / float(fps)
    # 说话能量：平滑后的张口度，再做一点滞后（头比嘴慢半拍，像真人）
    e = np.asarray(energy, np.float32) if len(energy) == n else np.zeros(n, np.float32)
    if n > 1:
        k = max(1, int(fps * 0.12))
        ker = np.hanning(k * 2 + 1)
        ker = ker / max(ker.sum(), 1e-6)
        e = np.convolve(e, ker, mode="same")
    e = np.clip(e, 0.0, 1.0)

    # 常态微摆：两个不同频率叠加，避免机械的单一正弦
    yaw_i = (YAW_IDLE * np.sin(2 * math.pi * 0.23 * t)
             + YAW_IDLE * 0.42 * np.sin(2 * math.pi * 0.61 * t + 1.3))
    pit_i = (PITCH_IDLE * np.sin(2 * math.pi * 0.31 * t + 0.7)
             + PITCH_IDLE * 0.35 * np.sin(2 * math.pi * 0.77 * t + 2.4))
    rol_i = ROLL_IDLE * np.sin(2 * math.pi * 0.17 * t + 2.1)

    # 说话时的律动：能量大 → 左右小幅摆动 + 轻微点头
    yaw_s = YAW_SPEECH * (e - 0.35) * np.sin(2 * math.pi * 0.42 * t + 0.4)
    pit_s = -PITCH_SPEECH * (e - 0.30)          # 说话时略低头（真人常态）
    rol_s = ROLL_SPEECH * (e - 0.4) * np.sin(2 * math.pi * 0.29 * t + 1.9)

    yaw = np.clip(yaw_i + yaw_s, -7.0, 7.0)
    pit = np.clip(pit_i + pit_s, -6.0, 5.0)
    rol = np.clip(rol_i + rol_s, -4.0, 4.0)

    # 整体位移：这才是「在动」的主要来源（旋转超过 5° 会露馅，平移不会）
    tx = (TX_IDLE * np.sin(2 * math.pi * 0.21 * t)
          + TX_IDLE * 0.40 * np.sin(2 * math.pi * 0.53 * t + 0.9)
          + TX_SPEECH * (e - 0.40) * np.sin(2 * math.pi * 0.37 * t + 1.7))
    ty = (TY_IDLE * np.sin(2 * math.pi * 0.27 * t + 1.1)
          + TY_IDLE * 0.35 * np.sin(2 * math.pi * 0.67 * t + 2.3)
          + TY_SPEECH * (e - 0.40))          # 说话时整体略微下沉（点头感）
    # 极轻微缩放：产生「前后」的空间感，0.8% 就够，再多会像镜头推拉
    scl = 1.0 + TZ_IDLE * np.sin(2 * math.pi * 0.19 * t + 0.5) \
        + 0.005 * (e - 0.30)
    tx = np.clip(tx, -8.0, 8.0)
    ty = np.clip(ty, -7.0, 7.0)
    scl = np.clip(scl, 0.985, 1.018)

    # 眨眼：2.6~4.6 秒一次，单次 0.13 秒
    rng = np.random.RandomState(int(n * 7919 + 13) & 0xFFFF)
    bl = np.zeros(n, np.float32)
    ti = int(fps * rng.uniform(0.8, 2.0))
    while ti < n:
        span = max(2, int(fps * 0.13))
        for j in range(span):
            if ti + j < n:
                p = j / float(span - 1)
                bl[ti + j] = 1.0 - abs(2.0 * p - 1.0)     # 闭-开
        ti += int(fps * rng.uniform(2.6, 4.6))
    return [(float(yaw[i]), float(pit[i]), float(rol[i]),
             float(tx[i]), float(ty[i]), float(scl[i]), float(bl[i]))
            for i in range(n)]


# ---------------------------------------------------------------- 渲染
def render(img_path: str, wav_path: str, out_mp4: str, words=None,
           fps: int = FPS, on_progress=None, amp: float = 1.2,
           motion: float = 1.0, blink: bool = True) -> dict:
    """音频 → 伪3D 视频。motion 是头部动作强度（0 = 完全不动，1 = 默认）。"""
    exe = A2.ffmpeg()
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "还没有形象图"}

    sr, y = A2._read_wav(wav_path)
    dur = len(y) / float(sr)
    if dur > A2.MAX_SECONDS:
        return {"ok": False, "error": "音频 %.1f 秒，超过 %d 秒上限"
                % (dur, A2.MAX_SECONDS)}
    if dur < 0.2:
        return {"ok": False, "error": "音频太短（%.2f 秒）" % dur}

    _amp = float(np.clip(amp, 0.60, 2.00))
    _mot = float(np.clip(motion, 0.0, 2.0))

    prep2d = A2._prep(img_path)            # 嘴部 patch / 蒙版
    prep3d = _prep3d(img_path)             # 深度图 / 权重
    params = A2._build_params(y, sr, words or [], fps, amp=_amp)
    W, H = prep3d["wh"]
    n = len(params)
    energy = [p[0] for p in params]
    poses = _pose_curve(n, fps, energy)

    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(fps), "-i", "-", "-i", wav_path,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-shortest", "-movflags", "+faststart", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, (op, wd) in enumerate(params):
            yaw, pit, rol, bl = poses[i]
            fr = A2._frame(prep2d, op, wd, amp=_amp)          # 先做嘴（原图坐标最准）
            fr = np.asarray(fr.convert("RGB"))
            fr = _warp(fr, prep3d, yaw * _mot, pit * _mot, rol * _mot)
            if blink and bl > 0.02:
                fr = _blink(fr, prep3d, bl, yaw * _mot, pit * _mot)
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            if on_progress and (i % 10 == 0 or i == n - 1):
                try:
                    on_progress(i + 1, n)
                except Exception:                              # noqa: BLE001
                    pass
    except Exception as e:                                     # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                      # noqa: BLE001
            pass
        return {"ok": False, "error": "渲染中断：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            proc.stdin.close()
        except Exception:                                      # noqa: BLE001
            pass
    err = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0 or not os.path.isfile(out_mp4) or os.path.getsize(out_mp4) < 1000:
        return {"ok": False, "error": "ffmpeg 合成失败：%s"
                % (err or b"")[:400].decode("utf-8", "ignore")}
    return {"ok": True, "frames": n, "duration": round(dur, 2),
            "size": [W, H], "path": out_mp4, "bytes": os.path.getsize(out_mp4),
            "mode": "pseudo3d"}


def speak(text: str, voice: str = "", rate=None, style: str = "",
          engine: str = "auto", amp: float = 1.2, motion: float = 1.0,
          blink: bool = True) -> dict:
    """文本 → TTS → 伪3D 口型/头姿 → mp4。"""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}
    img = A2._img_path()
    if not os.path.isfile(img):
        return {"ok": False, "error": "还没有数字人形象，请先上传一张图"}

    try:
        from . import tts as tts_mod
    except ImportError:
        import tts as tts_mod
    a = tts_mod.synth(t, voice=voice or "", rate=int(rate or 0),
                      engine=engine or "auto", style=style or "")
    if not a.get("ok"):
        return {"ok": False, "error": "配音失败：%s" % a.get("error")}
    try:
        wav = A2._to_wav16k(a.get("path") or "")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": "音频转码失败：%s: %s" % (type(e).__name__, e)}

    words = A2._asr_words(wav)
    name = "avatar25d_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4] + ".mp4"
    out = os.path.join(A2._vdir(), name)
    r = render(img, wav, out, words=words, amp=amp, motion=motion, blink=blink)
    if not r.get("ok"):
        return r
    from urllib.parse import quote
    return {"ok": True, "name": name,
            "url": "/api/avatar2/video/" + quote(name),
            "path": out, "duration": r["duration"], "size": r["size"],
            "frames": r["frames"], "bytes": r["bytes"],
            "mode": "pseudo3d", "amp": amp, "motion": motion,
            "lipsync_label": "伪3D（深度位移 + 音频驱动头姿）",
            "align": ("字级对齐 %d 字" % len(words)) if words else "声学驱动",
            "audio_url": a.get("url") or "", "text": t[:200]}


def info() -> dict:
    return {"ok": True, "available": available(),
            "mode": "pseudo3d",
            "features": ["深度图位移（保留原图细节）", "音频驱动头姿",
                         "眨眼", "口型形变"],
            "limits": {"yaw": "±7°", "pitch": "±6°", "roll": "±4°"},
            "model_dir": F3.MODEL_DIR}
