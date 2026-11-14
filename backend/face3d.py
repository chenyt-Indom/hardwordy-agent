# -*- coding: utf-8 -*-
"""3D 人脸重建 + 软渲染（纯 numpy/cv2/onnxruntime，不依赖任何 3D 库）。

流程：
  图 → YuNet 检测 → 5 点对齐裁 120² → mb1_120x120.onnx(62 维 3DMM)
     → 反归一化拆 R/offset/α_shp/α_exp → bfm_decoder.onnx → 38365 顶点
     → 纹理烘焙（正视图 UV）+ 三角形光栅化 → 任意头姿渲染

模型资产在 data/models/face3d/（由 _convert_bfm.py 离线导出 BFM decoder）。
"""
import math
import os
import pickle

import cv2
import numpy as np
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MODEL_DIR = os.path.join(ROOT, "data", "models", "face3d")

TEMPLATE5 = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                      [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)

_CACHE = {}


def available() -> bool:
    need = ["mb1_120x120.onnx", "bfm_decoder.onnx", "tri.pkl",
            "param_mean_std_62d_120x120.pkl"]
    return all(os.path.isfile(os.path.join(MODEL_DIR, n)) for n in need)


class Face3D:
    def __init__(self, model_dir=MODEL_DIR):
        self.dir = model_dir
        self._reg = ort.InferenceSession(os.path.join(model_dir, "mb1_120x120.onnx"),
                                         providers=["CPUExecutionProvider"])
        self._dec = ort.InferenceSession(os.path.join(model_dir, "bfm_decoder.onnx"),
                                         providers=["CPUExecutionProvider"])
        with open(os.path.join(model_dir, "param_mean_std_62d_120x120.pkl"), "rb") as f:
            ms = pickle.load(f, encoding="latin1")
        self._mean = ms["mean"].astype(np.float32)
        self._std = ms["std"].astype(np.float32)
        with open(os.path.join(model_dir, "tri.pkl"), "rb") as f:
            self.tri = pickle.load(f, encoding="latin1").astype(np.int32)
        self._yunet = None

    # ---------------------------------------------------------- 检测 + 裁剪
    def _detector(self, w, h):
        p = os.path.join(ROOT, "data", "models", "face", "face_detection_yunet.onnx")
        if self._yunet is None:
            self._yunet = cv2.FaceDetectorYN.create(p, "", (w, h), 0.6, 0.3, 5000)
        else:
            self._yunet.setInputSize((w, h))
        return self._yunet

    def crop_face(self, img):
        h, w = img.shape[:2]
        _n, faces = self._detector(w, h).detect(img)
        if faces is None or len(faces) == 0:
            return None, None
        faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
        lm = faces[0][4:14].reshape(5, 2).astype(np.float32)
        M, _ = cv2.estimateAffinePartial2D(lm, TEMPLATE5, method=cv2.LMEDS)
        crop = cv2.warpAffine(img, M, (120, 120), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
        return crop, lm

    # ---------------------------------------------------------- 62 维回归
    def regress(self, crop):
        inp = ((crop.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        raw = self._reg.run(None, {"input": inp})[0][0]
        p = raw * self._std + self._mean
        R4 = p[:12].reshape(3, 4)
        return {"R": R4[:, :3].astype(np.float32),
                "offset": R4[:, 3:4].astype(np.float32),
                "shp": p[12:52].reshape(40, 1).astype(np.float32),
                "exp": p[52:62].reshape(10, 1).astype(np.float32),
                "param": p}

    def decode(self, R, offset, shp, exp):
        return self._dec.run(None, {"R": R, "offset": offset,
                                    "alpha_shp": shp, "alpha_exp": exp})[0]

    def build(self, img):
        crop, lm = self.crop_face(img)
        if crop is None:
            return None
        prm = self.regress(crop)
        pts = self.decode(prm["R"], prm["offset"], prm["shp"], prm["exp"])
        # 正视图顶点：R 是含尺度的相似变换，对角线均值即尺度
        s = float(np.mean([prm["R"][0, 0], prm["R"][1, 1], prm["R"][2, 2]]))
        R_front = (np.eye(3, dtype=np.float32) * s)
        pts_front = self.decode(R_front, prm["offset"], prm["shp"], prm["exp"])
        return {"crop": crop, "prm": prm, "pts": pts, "pts_front": pts_front,
                "scale": s, "lm": lm,
                "uv": (pts_front[:2].T / 120.0).astype(np.float32)}


def get() -> "Face3D":
    if "f3" not in _CACHE:
        _CACHE["f3"] = Face3D()
    return _CACHE["f3"]


# ------------------------------------------------------------------ 软渲染
def render_mesh(tex, uv, pts, tri, size=256, bg=(0, 0, 0), shade=False,
                min_area=0.0, src_size=120.0):
    """把纹理 tex 按 uv 贴到 3D 顶点 pts 上并光栅化（画家算法）。"""
    V = pts.T.astype(np.float32)
    k = size / float(src_size)
    xy = V[:, :2] * k
    z = V[:, 2]
    th, tw = tex.shape[:2]
    tx = uv[:, 0] * (tw - 1)
    ty = uv[:, 1] * (th - 1)

    F = tri.T
    a, b, c = xy[F[:, 0]], xy[F[:, 1]], xy[F[:, 2]]
    area = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
            - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    keep = np.abs(area) > 1e-6
    F = F[keep]
    order = np.argsort(-z[F].mean(axis=1))
    F = F[order]

    canvas = np.full((size, size, 3), bg, np.uint8)
    if shade:
        n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
        n = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-9)
        light = np.array([0.25, -0.45, -0.86], np.float32)
        lam = 0.45 + 0.55 * np.abs(n @ light)
    else:
        lam = np.ones(len(F), np.float32)

    dst_all = xy[F]                              # (F,3,2)
    src_all = np.stack([tx[F], ty[F]], axis=-1)  # (F,3,2)
    lo = np.floor(dst_all.min(axis=1)).astype(np.int32)
    hi = np.ceil(dst_all.max(axis=1)).astype(np.int32) + 1
    lo = np.clip(lo, 0, size)
    hi = np.clip(hi, 0, size)
    bw = hi[:, 0] - lo[:, 0]
    bh = hi[:, 1] - lo[:, 1]
    area_px = np.abs((dst_all[:, 1, 0] - dst_all[:, 0, 0]) * (dst_all[:, 2, 1] - dst_all[:, 0, 1])
                     - (dst_all[:, 2, 0] - dst_all[:, 0, 0]) * (dst_all[:, 1, 1] - dst_all[:, 0, 1])) * 0.5
    ok = (bw >= 1) & (bh >= 1) & (area_px >= min_area)
    if not ok.any():
        return canvas

    F = F[ok]
    dst_all = dst_all[ok]
    src_all = src_all[ok]
    lo = lo[ok]
    bw = bw[ok]
    bh = bh[ok]
    lam = lam[ok]
    zf = z[F].mean(axis=1)

    # ---- 向量化 z-buffer 光栅化：把所有三角形覆盖的像素一次性展开 ----
    counts = (bw * bh).astype(np.int64)
    total = int(counts.sum())
    if total <= 0:
        return canvas
    tid = np.repeat(np.arange(len(F)), counts)
    start = np.repeat(np.cumsum(counts) - counts, counts)
    local = np.arange(total, dtype=np.int64) - start
    px = lo[tid, 0] + (local % bw[tid])
    py = lo[tid, 1] + (local // bw[tid])

    v0 = dst_all[tid, 0]
    v1 = dst_all[tid, 1]
    v2 = dst_all[tid, 2]
    den = ((v1[:, 1] - v2[:, 1]) * (v0[:, 0] - v2[:, 0])
           + (v2[:, 0] - v1[:, 0]) * (v0[:, 1] - v2[:, 1]))
    den = np.where(np.abs(den) < 1e-9, 1e-9, den)
    fx = px.astype(np.float32) + 0.5
    fy = py.astype(np.float32) + 0.5
    w0 = ((v1[:, 1] - v2[:, 1]) * (fx - v2[:, 0]) + (v2[:, 0] - v1[:, 0]) * (fy - v2[:, 1])) / den
    w1 = ((v2[:, 1] - v0[:, 1]) * (fx - v2[:, 0]) + (v0[:, 0] - v2[:, 0]) * (fy - v2[:, 1])) / den
    w2 = 1.0 - w0 - w1
    eps = -1.5 / np.maximum(np.abs(den), 1e-9)
    inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
    if not inside.any():
        return canvas

    tid_i = tid[inside]
    px_i = px[inside]
    py_i = py[inside]
    b0, b1, b2 = w0[inside], w1[inside], w2[inside]

    # 插值深度 + 纹理坐标
    dep = b0 * zf[tid_i] + b1 * zf[tid_i] + b2 * zf[tid_i]
    s0 = src_all[tid_i, 0]
    s1 = src_all[tid_i, 1]
    s2 = src_all[tid_i, 2]
    u = b0 * s0[:, 0] + b1 * s1[:, 0] + b2 * s2[:, 0]
    v = b0 * s0[:, 1] + b1 * s1[:, 1] + b2 * s2[:, 1]
    u = np.clip(u, 0, tw - 1.001)
    v = np.clip(v, 0, th - 1.001)

    # z-buffer：每个像素保留最近（z 最小）的片元
    flat = py_i * size + px_i
    order = np.lexsort((dep, flat))
    flat_s = flat[order]
    first = np.ones(len(flat_s), bool)
    first[1:] = flat_s[1:] != flat_s[:-1]
    sel = order[first]

    fx_i = flat[sel]
    cx = fx_i % size
    cy = fx_i // size
    uu = u[sel]
    vv = v[sel]
    ll = lam[tid_i[sel]]
    x0i = np.floor(uu).astype(np.int32)
    y0i = np.floor(vv).astype(np.int32)
    dx = uu - x0i
    dy = vv - y0i
    x1i = np.minimum(x0i + 1, tw - 1)
    y1i = np.minimum(y0i + 1, th - 1)
    c00 = tex[y0i, x0i].astype(np.float32)
    c10 = tex[y0i, x1i].astype(np.float32)
    c01 = tex[y1i, x0i].astype(np.float32)
    c11 = tex[y1i, x1i].astype(np.float32)
    w00 = (1 - dx) * (1 - dy)
    w10 = dx * (1 - dy)
    w01 = (1 - dx) * dy
    w11 = dx * dy
    col = (c00 * w00[:, None] + c10 * w10[:, None]
           + c01 * w01[:, None] + c11 * w11[:, None]) * ll[:, None]
    canvas[cy, cx] = np.clip(col, 0, 255).astype(np.uint8)
    return canvas


def rot_y(deg):
    r = np.deg2rad(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)


def rot_x(deg):
    r = np.deg2rad(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)


def rot_z(deg):
    r = np.deg2rad(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)


def pose_pts(pts, ry=0.0, rx=0.0, rz=0.0):
    """绕网格中心做欧拉旋转。"""
    c = pts.mean(axis=1, keepdims=True)
    R = rot_y(ry) @ rot_x(rx) @ rot_z(rz)
    return R @ (pts - c) + c


# ------------------------------------------------------- 表情 / 口型 / 眨眼
def mouth_region(uv, y0=0.60, y1=0.80, x0=0.32, x1=0.68):
    """用 UV 圈出嘴部顶点（UV 是正视图归一化坐标，嘴在中间偏下）。"""
    uy, ux = uv[:, 1], uv[:, 0]
    return (uy > y0) & (uy < y1) & (ux > x0) & (ux < x1)


def deform_mouth(pts, uv, openness, jaw=1.0):
    """按张口量变形：唇部分离 + 下颌下移。

    BFM 的 expression 只分离嘴唇、不动下颌，所以这里补一段程序化下颌下沉，
    否则张嘴看起来像"嘴唇被撕开"。
    """
    if openness <= 0.001:
        return pts
    o = float(np.clip(openness, 0.0, 1.0))
    out = pts.copy()
    m = mouth_region(uv)
    uy = uv[:, 1]
    upper = m & (uy < 0.70)
    lower = m & (uy >= 0.70)
    out[1][upper] -= o * 3.2          # 上唇上抬（120 坐标系）
    out[1][lower] += o * 5.6          # 下唇下压
    if jaw > 0:
        lo, hi = 0.70, 0.97
        w = np.clip((uy - lo) / (hi - lo), 0.0, 1.0)
        w = w * w * (3 - 2 * w)       # smoothstep，避免下巴断层
        w[uy <= lo] = 0.0
        out[1] += o * jaw * 9.0 * w
        out[2] += o * jaw * 1.6 * w   # 下颌略前移，侧面不显纸片
    return out


def eye_region(uv, side="both"):
    uy, ux = uv[:, 1], uv[:, 0]
    band = (uy > 0.34) & (uy < 0.50)
    if side == "left":
        return band & (ux > 0.55) & (ux < 0.78)
    if side == "right":
        return band & (ux > 0.22) & (ux < 0.45)
    return band & (((ux > 0.55) & (ux < 0.78)) | ((ux > 0.22) & (ux < 0.45)))


def deform_blink(pts, uv, amount):
    """眨眼：把上眼睑顶点向下压。比 2D 压缩自然，也不会出现填充方块。"""
    if amount <= 0.02:
        return pts
    a = float(np.clip(amount, 0.0, 1.0))
    m = eye_region(uv)
    uy = uv[:, 1]
    lid = m & (uy < 0.43)
    if not lid.any():
        return pts
    out = pts.copy()
    # 越靠上的眼睑压得越多，形成自然的闭合曲线
    w = np.clip((0.43 - uy) / 0.09, 0.0, 1.0)
    out[1][lid] += a * 3.6 * w[lid]
    return out


def blink_2d(img, uv, pts, amount, size=256, src_size=120.0):
    """眨眼后处理：眼部区域垂直压缩 + 压一条眼睑线。

    3DMM 网格没有眼睑自由度，用 2D 压缩模拟闭眼。amount: 0 睁 ~ 1 闭。
    """
    if amount <= 0.02:
        return img
    a = float(np.clip(amount, 0.0, 1.0))
    k = size / float(src_size)
    xy = pts.T[:, :2] * k
    out = img.copy()
    for side in ("left", "right"):
        sel = eye_region(uv, side)
        if not sel.any():
            continue
        ex, ey = xy[sel, 0], xy[sel, 1]
        x0, x1 = int(max(0, ex.min() - 2)), int(min(size, ex.max() + 3))
        y0, y1 = int(max(0, ey.min() - 2)), int(min(size, ey.max() + 3))
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        patch = out[y0:y1, x0:x1].copy()
        h = patch.shape[0]
        nh = max(2, int(round(h * (1.0 - 0.55 * a))))
        squeezed = cv2.resize(patch, (patch.shape[1], nh), interpolation=cv2.INTER_LINEAR)
        skin = np.median(patch.reshape(-1, 3), axis=0)
        canvas = np.empty_like(patch)
        canvas[:, :] = skin
        canvas[:nh] = squeezed
        out[y0:y1, x0:x1] = canvas
        ly = y0 + nh - 1
        if 0 <= ly < size:
            cv2.line(out, (x0 + 1, ly), (x1 - 2, ly),
                     tuple(int(c) for c in (skin * 0.45)), max(1, int(round(1 + a))))
    return out


def _draw_cavity(img, uv, pts, openness, size=256, src_size=120.0):
    """在上下唇之间画出张开的「口腔」。

    单图重建的嘴唇在纹理里是闭合的，无论网格怎么拉，都不会出现口腔内部。
    这里按张口量在唇缝处叠一个深色椭圆（上暗下亮的梯度 + 一点牙齿），
    让「张嘴」在视觉上真正成立。
    """
    if openness <= 0.02:
        return img
    o = float(np.clip(openness, 0.0, 1.0))
    k = size / float(src_size)
    m = mouth_region(uv)
    uy = uv[:, 1]
    up = m & (uy < 0.70)
    lo = m & (uy >= 0.70)
    if not (up.any() and lo.any()):
        return img
    V = pts.T[:, :2] * k
    cx = float(V[m, 0].mean())
    y_up = float(V[up, 1].mean())
    y_lo = float(V[lo, 1].mean())
    cy = (y_up + y_lo) * 0.5
    half_w = float(np.ptp(V[m, 0])) * 0.40
    gap = max(0.0, y_lo - y_up)
    half_h = gap * 0.42
    if half_w < 2 or half_h < 0.8:
        return img

    out = img.copy()
    # 取唇缝附近的真实像素作为「口腔色」，避免凭空造色导致贴纸感
    k2 = size / float(src_size)
    sample = []
    for j in np.nonzero(m)[0]:
        px, py = int(V[j, 0]), int(V[j, 1])
        if 1 <= px < size - 1 and 1 <= py < size - 1:
            sample.append(out[py, px])
    if not sample:
        return out
    sample = np.array(sample, np.float32)
    lum = sample.sum(axis=1)
    inner = sample[np.argsort(lum)[:max(1, len(sample) // 12)]].mean(axis=0)  # 最暗的一撮
    inner = np.clip(inner * 0.72, 0, 255)      # 再压暗，作为口腔深处

    ov = out.copy()
    # 纵向软渐变：中间最深，向上/下过渡回唇色
    steps = max(5, int(half_h * 2))
    for s in range(steps):
        yy = cy - half_h + (s + 0.5) * (2 * half_h / steps)
        tt = (yy - cy) / max(half_h, 1e-6)                    # -1 上 → +1 下
        w = math.sqrt(max(0.0, 1.0 - tt * tt))                # 椭圆横向宽度
        rr = half_w * w
        if rr < 0.6:
            continue
        mix = 0.55 + 0.45 * w                                  # 中间最深
        col = tuple(int(c) for c in (inner * mix))
        cv2.line(ov, (int(round(cx - rr)), int(round(yy))),
                 (int(round(cx + rr)), int(round(yy))), col, 1, lineType=cv2.LINE_AA)
    # 软边蒙版
    mask = np.zeros((size, size), np.uint8)
    cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                (int(round(half_w)), int(round(half_h))),
                0, 0, 360, 255, -1, lineType=cv2.LINE_AA)
    mask = cv2.GaussianBlur(mask, (9, 9), 0).astype(np.float32) / 255.0
    a = (mask * (0.45 + 0.40 * o))[..., None]
    return (ov.astype(np.float32) * a + out.astype(np.float32) * (1 - a)).astype(np.uint8)


def render_avatar(model, tex, uv, tri, ry=0.0, rx=0.0, rz=0.0,
                  openness=0.0, blink=0.0, size=256, pts_base=None,
                  min_area=0.0, bg=(0, 0, 0), feather=1.6):
    """渲染一帧：头姿 + 张嘴（含口腔）+ 眨眼。"""
    pts = pts_base if pts_base is not None else model["pts_front"]
    if openness > 0.001:
        pts = deform_mouth(pts, uv, openness)
    if blink > 0.02:
        pts = deform_blink(pts, uv, blink)
    if ry or rx or rz:
        pts = pose_pts(pts, ry=ry, rx=rx, rz=rz)
    img = render_mesh(tex, uv, pts, tri, size=size, bg=bg, min_area=min_area)
    # 边缘羽化：网格边界是硬切的，直接贴到背景上会有一圈灰白锯齿
    if feather > 0:
        cov = (img.sum(axis=2) > 6).astype(np.uint8) * 255
        cov = cv2.GaussianBlur(cov, (0, 0), feather)
        a = (cov.astype(np.float32) / 255.0)[..., None]
        bga = np.array(bg, np.float32)
        img = (img.astype(np.float32) * a + bga * (1 - a)).astype(np.uint8)
    return img
