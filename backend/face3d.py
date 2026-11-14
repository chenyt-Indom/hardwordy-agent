# -*- coding: utf-8 -*-
"""3D 人脸重建 + 软渲染（纯 numpy/cv2/onnxruntime，不依赖任何 3D 库）。

流程：
  图 → YuNet 检测 → 5 点对齐裁 120² → mb1_120x120.onnx(62 维 3DMM)
     → 反归一化拆 R/offset/α_shp/α_exp → bfm_decoder.onnx → 38365 顶点
     → 纹理烘焙（正视图 UV）+ 三角形光栅化 → 任意头姿渲染

模型资产在 data/models/face3d/（由 _convert_bfm.py 离线导出 BFM decoder）。
"""
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
def render_mesh(tex, uv, pts, tri, size=256, bg=(0, 0, 0), shade=True,
                min_area=0.35, src_size=120.0):
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

    # 预取三角形顶点（避免循环内重复索引）
    dst_all = xy[F]                      # (F,3,2)
    src_all = np.stack([tx[F], ty[F]], axis=-1)  # (F,3,2)
    lo = np.floor(dst_all.min(axis=1)).astype(np.int32)
    hi = np.ceil(dst_all.max(axis=1)).astype(np.int32) + 1
    lo = np.clip(lo, 0, size)
    hi = np.clip(hi, 0, size)
    bw = hi[:, 0] - lo[:, 0]
    bh = hi[:, 1] - lo[:, 1]
    # 亚像素三角形（投影后不足 1px²）对画面几乎无贡献，跳过以提速
    area_px = np.abs((dst_all[:, 1, 0] - dst_all[:, 0, 0]) * (dst_all[:, 2, 1] - dst_all[:, 0, 1])
                     - (dst_all[:, 2, 0] - dst_all[:, 0, 0]) * (dst_all[:, 1, 1] - dst_all[:, 0, 1])) * 0.5
    ok = (bw >= 1) & (bh >= 1) & (area_px >= min_area)

    for i in np.nonzero(ok)[0]:
        x0, y0 = int(lo[i, 0]), int(lo[i, 1])
        w, h = int(bw[i]), int(bh[i])
        s3 = src_all[i]
        d3 = dst_all[i] - np.array([x0, y0], np.float32)
        M = cv2.getAffineTransform(s3, d3)
        warped = cv2.warpAffine(tex, M, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REPLICATE)
        mask = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(mask, np.round(d3).astype(np.int32), 255)
        m = mask > 0
        if not m.any():
            continue
        canvas[y0:y0 + h, x0:x0 + w][m] = np.clip(
            warped[m].astype(np.float32) * lam[i], 0, 255).astype(np.uint8)
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
