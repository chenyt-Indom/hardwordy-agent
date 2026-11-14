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

# 整体位移 / 缩放。实测 ±6px（0.8% 画面）在成片里几乎看不出，
# 真人说话时头部摆动的量级是画面的 1.5~3%，所以给到 ±11px 左右。
TX_IDLE = 8.5
TY_IDLE = 6.0
TZ_IDLE = 0.010
TX_SPEECH = 3.4
TY_SPEECH = 4.2


def available() -> bool:
    return F3.available()


# ---------------------------------------------------------------- 预处理
def _prep3d(img_path: str, max_side: int = 0) -> dict:
    """一次性算好深度图 / 权重 / 人脸几何（每段视频只做一次）。

    max_side: 尺寸基准。0 = 沿用 A2.MAX_SIDE（伪3D 用）；Wav2Lip 链路会传
    proc_side，因为它的帧尺寸由自己决定，两边必须一致，否则 ROI 对不上会
    直接广播报错（踩过）。
    """
    from PIL import Image
    if not F3.available():
        raise RuntimeError("3D 模型文件不全（data/models/face3d/）")

    im = Image.open(img_path)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    if max_side:
        _ms = max(2, int(max_side))
        _sc = min(1.0, _ms / float(max(im.width, im.height)))
        W0 = max(2, int(round(im.width * _sc / 2.0)) * 2)
        H0 = max(2, int(round(im.height * _sc / 2.0)) * 2)
    else:
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

    # 头部区域 = 脸 + 头发。只用人脸网格做权重时，头发区实测 wgt 只有 0.025
    # 而脸内是 1.0 —— 头一转就是「脸动了、头发留在原地」地裂开。
    head = _head_region(img, mk, xs, ys, fw, fh)
    # 网格外的像素（头发/发际线）深度用「最近的脸边缘深度」填充，让它们跟着
    # 头做整体刚体位移；直接用 inpaint 的远距离扩散会被拉歪。
    if (head > 0).any() and (mk > 0).any():
        try:
            from scipy import ndimage as _ndi
            idx = _ndi.distance_transform_edt(
                mk == 0, return_distances=False, return_indices=True)
            zn_out = zn[tuple(idx)]
            # 最近邻填充是 Voronoi 分片：不同分片取到脸的不同部位（额头/脸颊）
            # 深度差好几个单位，分片边界是台阶。头发区每帧 warp 时这些台阶
            # 互相拉扯，就是「抽搐」的另一个来源。平滑后再合并。
            zn_out = cv2.GaussianBlur(np.ascontiguousarray(zn_out), (0, 0), 18.0)
            zn = np.where(mk > 0, zn, zn_out).astype(np.float32)
        except Exception:                               # noqa: BLE001
            pass
    # 位移权重：头部内饱和到 1（头发与脸同速，不会裂），边界羽化 14px
    wgt = cv2.GaussianBlur(head.astype(np.float32), (0, 0), 14.0)
    wgt = np.clip(wgt * 1.45, 0.0, 1.0)

    k_px = fw / 83.9
    scale = zspan * k_px * GAIN

    # ROI 覆盖整个头部（脸 + 头发）。原来按人脸框外扩，头发一旦超出 ROI
    # 就完全不参与 remap —— 边界上直接一条硬切线。
    hy, hx = np.where(head > 0)
    pad = int(max(fw, fh) * 0.10) + 28
    x0 = max(0, int(hx.min()) - pad)
    x1 = min(W, int(hx.max()) + pad)
    y0 = max(0, int(hy.min()) - pad)
    y1 = min(H, int(hy.max()) + pad)

    return {"img": img, "wh": (W, H), "zn": zn, "wgt": wgt,
            "mask": mk, "scale": scale,
            "cx": cx, "cy": cy, "fw": fw, "fh": fh,
            "eyes": [(float(lm[0][0]), float(lm[0][1])),
                     (float(lm[1][0]), float(lm[1][1]))],
            "eye_gap": float(np.linalg.norm(lm[0] - lm[1])),
            "roi": (x0, y0, x1, y1), "zspan": zspan}


def _head_region(img, mk, xs, ys, fw, fh):
    """圈出「脸 + 头发」的整体区域（位移权重就用它）。

    做法：拿图像四角估背景色 → 颜色距离分出前景 → 把脸框上方/左右的前景
    并进头部 → 只保留与脸相连的连通域（避免画面别处的前景被拉进来）。
    背景不是纯色时会自然退化成「只扩脸框」，不会比原来更差。
    """
    H, W = mk.shape
    base = (mk > 0).astype(np.uint8)
    try:
        p = 24
        corners = [img[:p, :p], img[:p, -p:], img[-p:, :p], img[-p:, -p:]]
        bg = np.median(np.concatenate([c.reshape(-1, 3) for c in corners], 0), 0)
        diff = np.linalg.norm(img.astype(np.float32) - bg.astype(np.float32), axis=2)
        fg = (diff > 16.0).astype(np.uint8)

        x0 = max(0, int(xs.min() - fw * 0.45))
        x1 = min(W, int(xs.max() + fw * 0.45))
        y0 = max(0, int(ys.min() - fh * 0.75))      # 头发在脸框上方
        y1 = min(H, int(ys.max() + fh * 0.08))
        win = np.zeros((H, W), np.uint8)
        win[y0:y1, x0:x1] = 1

        head = ((mk > 0) | ((fg > 0) & (win > 0))).astype(np.uint8)
        head = cv2.morphologyEx(
            head, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21)))
        head = cv2.morphologyEx(
            head, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))

        n, lab = cv2.connectedComponents(head)
        if n > 1:
            keep = np.unique(lab[mk > 0])
            keep = keep[keep > 0]
            if keep.size:
                head = np.isin(lab, keep).astype(np.uint8)
        # 膨胀一点，把发丝和轮廓外沿也带上（否则边缘一圈还是不跟着动）
        head = cv2.dilate(
            head, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        if (head > 0).mean() < 0.02:
            head = base
    except Exception:                                   # noqa: BLE001
        head = base
    return head


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


# ---------------------------------------------------------------- 嘴部形变
def _mouth_frame(prep, op, wd, amp=1.0):
    """张口：让上下唇**真的分开**。

    ⚠ 为什么不用 avatar2._frame：
      它的形变场是 src = 唇缝行 + d*k（k<1），只有**纵向拉伸**、唇缝行固定不动，
      所以效果是「唇缝被拉宽变暗」，上下唇并没有分离。
      实测三档 op=0/0.5/1.0 的唇缝暗带行数 32/33/32（几乎不变），
      视觉复核也判定「三张几乎一模一样，像在抿嘴」——这就是 2D 版「僵硬」的根因。

    这里改成**分区位移**：
      上唇整体上移 up、下唇整体下移 dn，中间空出的区域用原图唇缝那一行拉伸填充
      并压暗（颜色来自原图，不是凭空画的色块，所以不会有贴纸感）。
      嘴角用横向高斯权重固定住，不会把两颊一起拉扯。
    """
    from PIL import Image
    from PIL import ImageFilter
    base = prep["base"]
    patch = prep["patch"]
    mask = prep["mask"]
    cx, cy, mw, mh = prep["geom"]
    x0, ytop, x1, ybot = prep["box"]

    op = float(np.clip(op, 0.0, 1.0))
    wd = float(np.clip(wd, 0.70, 1.35))
    if op < 0.03 and abs(wd - 1.0) < 0.03:
        return base

    pw, ph = patch.size
    cyi0 = int(np.clip(round(cy - ytop), 8, ph - 9))

    # ⚠ patch 的垂直范围是唇高 ×1.45/×1.55，上边界已经碰到鼻子。
    #   整块重采样 + USM 会把鼻翼糊掉（视觉复核明确指出「鼻子崩坏」）。
    #   所以这里只保留唇部 ±1.3 唇高的窄带，其余原样不动。
    half = max(10, int(round(mh * 1.30)))
    p_top = max(0, cyi0 - half)
    p_bot = min(ph, cyi0 + half)
    patch = patch.crop((0, p_top, pw, p_bot))
    parr = np.asarray(patch, np.float32)
    ph = p_bot - p_top
    cyi = int(np.clip(cyi0 - p_top, 6, ph - 7))
    paste_y = ytop + p_top
    # 蒙版重新生成（不用裁剪来的那份）：窄带的上下边界离唇很近，
    # 只给 3px 羽化会留下一条亮度跳变，视觉上像「嘴唇上方糊了一块」。
    mask = A2._h_mask(pw, ph, feather=max(4, int(mh * 0.22)))

    yy, xx = np.mgrid[0:ph, 0:pw].astype(np.float32)
    # 横向权重：嘴角固定（高斯，覆盖唇宽）
    wx = np.exp(-((xx - (cx - x0)) / max(0.52 * mw, 1.0)) ** 2)
    # 纵向权重：**必须在唇的边界精确归零**。
    #   ⚠ 原来用高斯 exp(-(|d|/0.78mh)^2)：在 d=-mh*0.55（上唇上缘）处还有 0.61 的权重，
    #     于是上唇被整体推上去、上唇内容顶到人中位置，视觉上就是
    #     「嘴唇上方长了一块假胡子」。视觉复核连续两次指出这一点。
    #   改成 smoothstep：上唇上缘(-0.58mh) 和下唇下缘(+0.98mh) 处权重严格为 0。
    dd = yy - cyi
    _u = np.clip((dd + mh * 0.58) / (mh * 0.58), 0.0, 1.0)
    _d = np.clip((mh * 0.98 - dd) / (mh * 0.98), 0.0, 1.0)
    wy = np.where(dd < 0, _u * _u * (3.0 - 2.0 * _u),
                  _d * _d * (3.0 - 2.0 * _d))
    w = wx * wy

    a = float(np.clip(amp, 0.60, 2.00))
    # 张口总位移：单图没有口腔/牙齿信息，张口越大越像贴色块。
    #   实测 mh*1.05 时视觉判定「深棕色色块、边缘笔直」，
    #   收到 mh*0.62（最大约 26px 的一半）才是单图能自然做到的上限。
    total = op * mh * 0.62 * (1.0 + 0.22 * (a - 1.0))
    up = total * 0.40        # 上唇上抬（少）
    dn = total * 0.60        # 下唇下压（多，符合真人）

    d = yy - cyi
    up_w = up * w
    dn_w = dn * w
    # 分区反向映射：上唇区、下唇区、中间缝隙区
    # 缝隙用 0.34 的斜率（≈3 倍拉伸）：保留原图唇缝本身的明暗渐变，
    #   写 0.18（5.5 倍）时渐变被抹平，视觉上就是一条均匀色带。
    src = np.where(d < -up_w, yy + up_w,
                   np.where(d > dn_w, yy - dn_w,
                            cyi + d * 0.16))
    src = np.clip(src, 0, ph - 1)

    i0 = np.floor(src).astype(np.int32)
    i1 = np.minimum(i0 + 1, ph - 1)
    frac = (src - i0)[..., None]
    cols = np.arange(pw)[None, :]
    outp = parr[i0, cols] * (1.0 - frac) + parr[i1, cols] * frac

    # 横向形变：圆唇 / 扁唇（方向已实测校正）
    if abs(wd - 1.0) > 0.02:
        kx = 1.0 - 1.40 * (wd - 1.0)
        f = (kx - 1.0) * wx * wy * op
        sx = np.clip((cx - x0) + (xx - (cx - x0)) * (1.0 + f), 0, pw - 1)
        j0 = np.floor(sx).astype(np.int32)
        j1 = np.minimum(j0 + 1, pw - 1)
        f2 = (sx - j0)[..., None]
        rows = np.arange(ph)[:, None]
        outp = outp[rows, j0] * (1.0 - f2) + outp[rows, j1] * f2

    # 口腔压暗：只在缝隙区，越靠中间越暗（模拟口腔深度）。
    # 0.46 太重会变成黑带，0.26 是「有纵深但不脏」的平衡点。
    # ⚠ 压暗必须**严格限制在缝隙内**，不能用高斯往外扩散：
    #   sigma 给 0.62*total（±17px）或 0.40*total（±11px）时，暗区都会盖到上唇，
    #   视觉复核两次判定「嘴唇上方长了一块假胡子」——因为上唇本身只有 ~17px 高。
    #   这里按缝隙的实际上下边界（-up_w ~ +dn_w）算权重，边缘 35% 做渐变。
    span = np.maximum(up_w + dn_w, 1.0)
    g = np.clip((d + up_w) / span, 0.0, 1.0)      # 0=缝隙上缘 1=缝隙下缘
    edge = np.clip(np.minimum(g, 1.0 - g) / 0.35, 0.0, 1.0)
    edge = edge * edge * (3.0 - 2.0 * edge)
    dark = 1.0 - (0.34 * op) * edge * wx
    outp = outp * dark[..., None]

    img = Image.fromarray(np.clip(outp, 0, 255).astype(np.uint8), mode="RGB")
    # 拉伸会损失锐度，按 op 连续加权做 USM（阈值开关会造成闪烁）
    amt = 70.0 * float(A2._smoothstep(min(max(op / 0.25, 0.0), 1.0)))
    if amt >= 1.0:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0,
                                                 percent=int(round(amt)),
                                                 threshold=2))
    out = base.copy()
    out.paste(img, (x0, paste_y), mask)
    return out


# ---------------------------------------------------------------- 伪3D 变换
def _warp(img, prep, yaw=0.0, pitch=0.0, roll=0.0,
          tx=0.0, ty=0.0, scl=1.0, iters=2):
    """按头姿做反向映射。正向：源 → 旋转 → 缩放 → 平移 → 目标。

    iters: 深度视差的反向求解迭代次数。实测 1 次与 2 次的像素差仅 0.037
    （0~255 尺度，肉眼不可见），但省 19% 时间 —— Wav2Lip 每帧都要做一次，
    累积起来很可观，所以那条链路传 1；伪3D 保持 2 次的精度。
    """
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

    # 0) 撤销平移 + 缩放（绕脸中心）
    _s = max(0.90, min(1.10, float(scl)))
    if abs(tx) > 1e-4 or abs(ty) > 1e-4 or abs(_s - 1.0) > 1e-6:
        u = cx + (u - tx - cx) / _s
        v = cy + (v - ty - cy) / _s

    # 1) 撤销 roll（绕脸中心反向旋转）
    if abs(rz) > 1e-6:
        cr, sr = math.cos(-rz), math.sin(-rz)
        du, dv = u - cx, v - cy
        u, v = cx + du * cr - dv * sr, cy + du * sr + dv * cr

    # 2) 迭代求源坐标：透视压缩 + 深度视差（两次足够收敛）
    zn = prep["zn"]
    wg = prep["wgt"]
    su, sv = u.copy(), v.copy()
    for _ in range(max(1, int(iters))):
        zs = cv2.remap(zn, su, sv, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        ws = cv2.remap(wg, su, sv, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        # ⚠️ 透视压缩 (u-cx)/cyw 必须也按 ws 加权！
        # 旧版写成 cx + (u-cx)/cyw - ...，压缩项与权重无关 → 背景（ws≈0）
        # 照样被按 yaw 缩放。实测 yaw=7.5° 时 ROI 内 89% 的背景像素被改动、
        # 最大差 68 灰度级，而 ROI 外背景纹丝不动 → ROI 边界上一条横贯全画面
        # 的硬切线，这就是「割裂」的主因。
        # 加权后：ws=0 → su=u（完全不动）；ws=1 → 与旧式等价。
        kx = 1.0 + (1.0 / cyw - 1.0) * ws
        ky = 1.0 + (1.0 / cxp - 1.0) * ws
        su = cx + (u - cx) * kx - scale * zs * syw * ws / cyw
        sv = cy + (v - cy) * ky - scale * zs * sxp * ws / cxp

    sub = img[y0:y1, x0:x1]
    out = img.copy()
    out[y0:y1, x0:x1] = cv2.remap(sub, su - x0, sv - y0, cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
    return out


def _shift_of(prep, x, y, yaw=0.0, pitch=0.0, tx=0.0, ty=0.0,
              roll=0.0, scl=1.0):
    """某点在新姿态下被挪到哪（用来让眼睛跟着头走）。

    ⚠️ 必须用**正向**变换。旧版照抄了 _warp 里的反向映射公式
    （`cx + (x-cx)/cyw - ...`），方向正好反了：yaw=7.5°、z=±1 时偏差能到
    20px，眨眼就画在错的位置 —— 这是「眨眼位置偏移」的直接原因。
    旧版还完全没算 roll 和缩放，roll=4° 时脸缘又能差 14px。
    """
    if (abs(yaw) < 0.01 and abs(pitch) < 0.01 and abs(roll) < 0.01
            and abs(tx) < 0.05 and abs(ty) < 0.05 and abs(scl - 1.0) < 0.0005):
        return 0.0, 0.0
    ry, rx = math.radians(yaw), math.radians(pitch)
    cyw = max(math.cos(ry), 0.55)
    cxp = max(math.cos(rx), 0.55)
    cx, cy = prep["cx"], prep["cy"]
    xi = int(np.clip(x, 0, prep["wh"][0] - 1))
    yi = int(np.clip(y, 0, prep["wh"][1] - 1))
    z = float(prep["zn"][yi, xi])
    w = float(prep["wgt"][yi, xi])
    # 1) 正向视差（_warp 反向映射的逆过程）
    u = cx + (x - cx) * cyw + prep["scale"] * z * math.sin(ry) * w
    v = cy + (y - cy) * cxp + prep["scale"] * z * math.sin(rx) * w
    # 2) roll（与 _warp 的 roll 同向）
    if abs(roll) > 1e-6:
        rz = math.radians(roll)
        cr, sr = math.cos(rz), math.sin(rz)
        du, dv = u - cx, v - cy
        u, v = cx + du * cr - dv * sr, cy + du * sr + dv * cr
    # 3) 缩放 + 平移
    _s = max(0.90, min(1.10, float(scl)))
    u = cx + (u - cx) * _s + tx
    v = cy + (v - cy) * _s + ty
    return u - x, v - y


def _blink(img, prep, amount, yaw=0.0, pitch=0.0, tx=0.0, ty=0.0,
           roll=0.0, scl=1.0):
    """眨眼：上眼皮**弧形下落**覆盖眼球（不是把眼睛压扁）。

    迭代记录：
      · 第一版 ROI 取 eye_gap*0.42 当半宽 → 单眼 ROI 147px = 眼距 84%、
        脸宽 36%，眉毛鼻梁全被框进去，压缩后底部填「最后一行」→ 整片脸扭。
      · 第二版收窄到真实眼裂（0.17/0.085 眼距）→ 框外像素 0，但视觉复核
        判定「水平硬边切割、像贴了遮罩」——因为纵向压缩出来的边缘是直线。
      · 现在：对每一列，把该列**上方**的皮肤按弧形下拉量搬到当前位置，
        中间拉得多、眼角拉得少 → 形成真实的上眼睑弧线，且采样源是皮肤
        本身，颜色连续，不会出现色带或拉伸。
    """
    if amount <= 0.02:
        return img
    a = float(np.clip(amount, 0.0, 1.0))
    gap = float(prep.get("eye_gap", 0.0))
    if gap < 4.0:
        return img
    out = img.copy()
    H, W = prep["wh"]
    ew = max(6.0, gap * 0.17)          # 眼裂半宽
    eh = max(5.0, gap * 0.085)         # 眼裂半高
    for (ex, ey) in prep["eyes"]:
        dx, dy = _shift_of(prep, ex, ey, yaw, pitch, tx, ty, roll, scl)
        ex, ey = ex + dx, ey + dy
        # ROI 上方要多留皮肤当「上眼皮料」：最多下拉 2.2*eh，再留 1 个 eh 余量
        rx0 = int(max(0, ex - ew * 1.18)); rx1 = int(min(W, ex + ew * 1.18))
        ry0 = int(max(0, ey - eh * 3.4));  ry1 = int(min(H, ey + eh * 1.45))
        if rx1 - rx0 < 10 or ry1 - ry0 < 10:
            continue
        roi = out[ry0:ry1, rx0:rx1].astype(np.float32)
        h, w = roi.shape[:2]
        cy, cx = ey - ry0, ex - rx0
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

        # 采样源：眼窝那条皮肤（cy-2.62eh → cy-1.72eh）。
        # 关键是要**整条都在睫毛线以上** —— 早先取到 cy-1.0eh 时把睫毛/眼线
        # 一起拉了下来，视觉复核就判定「闭合缝隙里透出深色眼珠」。
        t = np.clip((yy - (cy - eh)) / (2.0 * eh), -0.45, 1.25)
        src_y = (cy - 2.58 * eh) + t * (1.05 * eh)

        # 覆盖量：a=1 时眼裂整块被眼窝皮肤替代（不露眼白），a 小时按比例
        # 混回原图 → 半闭。不按 cos 衰减，否则眼角完全不闭会露出巩膜。
        map_y = (yy + (src_y - yy) * a).astype(np.float32)
        moved = cv2.remap(roi, xx.astype(np.float32), map_y, cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)

        # 纹理回注：源带被拉伸 ~1.9 倍后必然变糊，视觉复核会看到「一块平滑
        # 肉色补丁」。用 unsharp 把高频提回来，闭眼处仍有皮肤颗粒感。
        _blur = cv2.GaussianBlur(moved, (0, 0), 1.5)
        moved = moved + (moved - _blur) * 0.6

        # 闭合线的眼睑厚度阴影：沿闭合位置压暗，避免「一块平铺的肉色」
        lid = np.clip(1.0 - np.abs(yy - (cy + eh * 0.05)) / (eh * 0.70), 0.0, 1.0)
        shade = 1.0 - 0.18 * a * lid
        moved = moved * shade[:, :, None]

        # 蒙版：垂直上界羽化在眼窝（cy-1.95eh），下界严格卡在眼裂下缘
        # （cy+eh）—— 越过这条线就会把上眼睑内容复制到下眼睑，视觉复核
        # 会看到「眼下硬边 / 肉色补丁」。水平方向椭圆收口。
        top = np.clip((yy - (cy - 1.95 * eh)) / (eh * 0.45), 0.0, 1.0)
        bot = np.clip(((cy + eh * 1.00) - yy) / (eh * 0.40), 0.0, 1.0)
        side = np.clip((ew * 1.18 - np.abs(xx - cx)) / (ew * 0.32), 0.0, 1.0)
        m = (top * bot * side)[:, :, None]

        out[ry0:ry1, rx0:rx1] = np.clip(moved * m + roi * (1.0 - m),
                                        0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------- 头姿曲线
def _pose_curve(n, fps, energy):
    """生成每帧的头姿与眨眼。能量来自音频，让头跟着说话节奏动。"""
    out = []
    t = np.arange(n, dtype=np.float32) / float(fps)
    # 说话能量：平滑后的张口度，再做一点滞后（头比嘴慢半拍，像真人）
    e = np.asarray(energy, np.float32) if len(energy) == n else np.zeros(n, np.float32)
    if n > 1:
        # 能量平滑：语音能量在音节边界能从 0 跳到 0.9，原来 0.12s（3 帧）压不住
        # —— 实测 p95 帧间跳 0.60、228 帧里 116 帧跳超 0.15，头就跟着抖。
        # 放宽到 0.15s 并做两次（近似高斯），把音节级突跳磨成「语气起伏」。
        for _ in range(2):
            k = max(1, int(fps * 0.15))
            ker = np.hanning(k * 2 + 1)
            ker = ker / max(ker.sum(), 1e-6)
            pad = len(ker) // 2
            e = np.convolve(np.pad(e, pad, mode="edge"), ker, mode="same")[pad:pad + n]
    e = np.clip(e, 0.0, 1.0)

    # 常态微摆：两个不同频率叠加，避免机械的单一正弦
    yaw_i = (YAW_IDLE * np.sin(2 * math.pi * 0.23 * t)
             + YAW_IDLE * 0.42 * np.sin(2 * math.pi * 0.61 * t + 1.3))
    pit_i = (PITCH_IDLE * np.sin(2 * math.pi * 0.31 * t + 0.7)
             + PITCH_IDLE * 0.35 * np.sin(2 * math.pi * 0.77 * t + 2.4))
    rol_i = ROLL_IDLE * np.sin(2 * math.pi * 0.17 * t + 2.1)

    # 说话时的律动：能量大 → 左右小幅摆动 + 轻微点头
    # 注意：pitch 原来直接吃 (e-0.30)，能量逐帧跳 → 点头逐帧跳 → 整脸纵向抽搐。
    # 现在也乘一条慢正弦，即使能量有残留抖动，输出也保持连续。
    yaw_s = YAW_SPEECH * (e - 0.35) * np.sin(2 * math.pi * 0.42 * t + 0.4)
    pit_s = -PITCH_SPEECH * (e - 0.30) * np.sin(2 * math.pi * 0.33 * t + 0.9)
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
          + TY_SPEECH * (e - 0.40) * np.sin(2 * math.pi * 0.29 * t + 0.3))
    # 极轻微缩放：产生「前后」的空间感，0.8% 就够，再多会像镜头推拉
    scl = 1.0 + TZ_IDLE * np.sin(2 * math.pi * 0.19 * t + 0.5) \
        + 0.005 * (e - 0.30)
    tx = np.clip(tx, -13.0, 13.0)
    ty = np.clip(ty, -11.0, 11.0)
    scl = np.clip(scl, 0.982, 1.020)

    # 眨眼：2.8~4.8 秒一次，单次 0.26 秒
    # 原来 0.13s 在 25fps 下只有 3 帧，曲线是 0→1→0 的三角波，实测帧间
    # Δmax=1.00 —— 一帧之内从睁到全闭，视觉上就是「抽一下」。放宽到
    # 0.26s（6 帧）并用 sin（首尾导数为 0），闭合 40% / 睁开 60%
    # （真人闭眼比睁眼快），帧间 Δ 降到 0.7 以下，再经下面短窗低通到 0.4。
    rng = np.random.RandomState(int(n * 7919 + 13) & 0xFFFF)
    bl = np.zeros(n, np.float32)
    ti = int(fps * rng.uniform(0.8, 2.0))
    while ti < n:
        span = max(6, int(round(fps * 0.26)))
        for j in range(span):
            if ti + j < n:
                p = j / float(span - 1)
                if p < 0.4:
                    bl[ti + j] = 0.75 * math.sin(math.pi * 0.5 * (p / 0.4))
                else:
                    bl[ti + j] = 0.75 * math.cos(math.pi * 0.5 * ((p - 0.4) / 0.6))
        ti += int(fps * rng.uniform(2.8, 4.8))

    # 统一时间低通：不管上游怎么算，送进渲染的曲线必须连续 ——
    # 25fps 下逐帧 1° 的跳变就是肉眼可见的抽搐（实测 yaw 最大跳 1.09°、
    # pitch 1.09°、ty 1.65px）。这里做一道 0.20s 汉宁低通，低频（0.2~0.7Hz
    # 的摆动）几乎不衰减，音节级的突跳被抹平。眨眼曲线不参与。
    def _lp(v, sec=0.26):
        if n <= 2:
            return v
        k = max(1, int(fps * sec))
        ker = np.hanning(k * 2 + 1)
        ker = ker / max(ker.sum(), 1e-6)
        pad = len(ker) // 2
        return np.convolve(np.pad(v, pad, mode="edge"), ker, mode="same")[pad:pad + n]

    yaw, pit, rol = _lp(yaw), _lp(pit), _lp(rol)
    tx, ty, scl = _lp(tx), _lp(ty), _lp(scl)
    # 眨眼也过一道短窗低通：sin 曲线在 6 帧内从 0 到 1，帧间仍跳 0.7。
    # 0.09s 窗口只削掉 3% 的峰值，但帧间 Δ 压到 0.4 以下，不再「抽」。
    bl = _lp(bl, sec=0.09)
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
            yaw, pit, rol, tx, ty, scl, bl = poses[i]
            fr = _mouth_frame(prep2d, op, wd, amp=_amp)       # 先做嘴（原图坐标最准）
            fr = np.asarray(fr.convert("RGB"))
            fr = _warp(fr, prep3d, yaw * _mot, pit * _mot, rol * _mot,
                       tx * _mot, ty * _mot, 1.0 + (scl - 1.0) * _mot)
            if blink and bl > 0.02:
                fr = _blink(fr, prep3d, bl, yaw * _mot, pit * _mot,
                            tx * _mot, ty * _mot)
            try:
                proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            except (BrokenPipeError, OSError):
                # -shortest 让 ffmpeg 在音频结束时先退出，此时视频帧可能还多出半帧。
                # 这是正常收尾，不是错误（实测 186 帧 vs 7.42 秒音频会触发）。
                break
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
