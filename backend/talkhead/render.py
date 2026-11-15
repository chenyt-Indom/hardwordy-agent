"""口型参数 → 帧（局部形变 + 程序化口腔 + 羽化融合）

## 旧渲染为什么糊

旧做法（avatar2._frame）是把原图的**唇缝像素纵向拉伸**来填张嘴的缝：
k 最小 0.24，等于把 1 像素铺成 4.2 像素。这是不可逆的分辨率损失，
所以必须靠 USM 锐化补偿，而锐化又在唇缘产生白边 —— 「糊 + 接缝 + 边缘发虚」
三个症状是同一件事。

## 新渲染的做法

张嘴时唇间那块区域，原图里**根本不存在对应的像素**（照片是闭嘴的），
任何「拉伸已有像素」的做法都只能得到涂抹。所以这里改成两步：

  1. **形变只负责唇的位置**：上下唇按 open 分离、按 round/spread 收放，
     形变场用椭圆权重衰减到 0，绝不碰嘴外的脸颊和下巴 —— 这是「脸变形」的解。
  2. **唇间缝隙交给程序化合成**：按真实口腔的光学结构生成
     口腔暗区（径向明暗）+ 上齿（带齿缝与根部阴影）+ 舌/下齿，
     配色**从原图唇缝与唇色采样**，不是凭空取的纯黑或纯白。

最后用高斯羽化的 mask 贴回，边缘不留硬边。

## 为什么这不像「贴纸」

旧版 v2 直接画椭圆+白块，被判「贴纸」，原因是：纯色、硬边、无光影层次。
这里每一层都有渐变：口腔边缘亮中心暗（受光衰减）、牙齿根部有阴影、
齿缝有细暗线、唇内侧有接触阴影。这些都是从真实口腔结构来的，不是色块。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ROI 外扩比例：嘴部框往外留多少，用于形变衰减和羽化
_ROI_PAD = 0.85


# ---------------------------------------------------------------- 预处理


def _softstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _sample_color(img: np.ndarray, cx: float, cy: float,
                  rw: float, rh: float) -> Tuple[np.ndarray, np.ndarray]:
    """从嘴部区域采样两个关键色：唇色（偏红）与口腔暗色。"""
    H, W = img.shape[:2]
    x0 = max(0, int(cx - rw))
    x1 = min(W, int(cx + rw) + 1)
    y0 = max(0, int(cy - rh))
    y1 = min(H, int(cy + rh) + 1)
    if x1 <= x0 or y1 <= y0:
        return np.array([150, 95, 95], np.float32), np.array([35, 15, 18], np.float32)
    patch = img[y0:y1, x0:x1].astype(np.float32)
    flat = patch.reshape(-1, 3)
    lum = flat @ np.array([0.299, 0.587, 0.114], np.float32)

    # 唇色：取中等亮度段的中位数（避开高光和阴影）
    lo, hi = np.percentile(lum, 35), np.percentile(lum, 85)
    mid = flat[(lum >= lo) & (lum <= hi)]
    lip = np.median(mid, axis=0) if len(mid) > 20 else np.median(flat, axis=0)

    # 口腔暗色：取最暗 8% 的中位数，再压暗到 0.32（口腔内部几乎不受光）
    dark_cut = np.percentile(lum, 8)
    dark = flat[lum <= dark_cut]
    cav = np.median(dark, axis=0) if len(dark) > 5 else lip * 0.3
    cav = np.clip(cav * 0.34, 4, 255)
    # 口腔内部偏红（血液+黏膜），但保持低饱和
    cav = cav * np.array([1.10, 0.86, 0.88], np.float32)
    return lip.astype(np.float32), np.clip(cav, 0, 255).astype(np.float32)


def prep(img_rgb: np.ndarray, geom: Dict[str, float]) -> Dict:
    """准备渲染上下文（同一张图只做一次）。

    geom: {"cx","cy","w","h"}，均为**像素**坐标（左上原点）。
    """
    H, W = img_rgb.shape[:2]
    cx = float(geom["cx"])
    cy = float(geom["cy"])
    mw = float(geom["w"])
    mh = float(geom["h"])

    # ROI：嘴部框外扩，保证形变衰减和羽化都有余量
    pad_x = mw * _ROI_PAD
    pad_y = mh * _ROI_PAD + mh * 0.9        # 纵向要容纳张嘴后下唇的下沉
    x0 = max(0, int(cx - mw * 0.5 - pad_x))
    x1 = min(W, int(cx + mw * 0.5 + pad_x) + 1)
    y0 = max(0, int(cy - mh * 0.5 - pad_y))
    y1 = min(H, int(cy + mh * 0.5 + pad_y) + 1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("嘴部 ROI 太小，检查 geom")

    roi = img_rgb[y0:y1, x0:x1].astype(np.float32)
    rh, rw = roi.shape[:2]

    # ROI 内归一化坐标（以嘴心为原点，以嘴宽/嘴高为尺度）
    yy, xx = np.mgrid[0:rh, 0:rw].astype(np.float32)
    u = (xx + x0 - cx) / max(1.0, mw * 0.5)     # -1..1 横向
    v = (yy + y0 - cy) / max(1.0, mh * 0.5)     # -1..1 纵向

    # 形变权重：横向到嘴角衰减到 0（嘴角是固定的），
    # 纵向放宽到 1.8 个半嘴高 —— 因为张嘴时下唇整体会往下走，
    # 若仍用圆形衰减（r2=u²+v²），|v|>1 处权重就归零，
    # 下唇边缘被钉死、只有中心动，观感是「唇在抖」而不是「在张」。
    # ⚠ 衰减带必须够宽。原来写 softstep(1-u²)，从 u=0.95 到 1.0 就归零
    #   （约 8 像素），而羽化系数在这个区间仍是 1 —— 形变突然停住 + 硬替换，
    #   视觉复核反复判定「嘴部边缘有非常明显的硬边，像抠图贴上去」。
    #   现在从 u=0.67 开始衰减、到 u=1.0 归零（约 33% 嘴宽），过渡连续。
    # 纵向也必须收紧：|v|>1 已经是唇外（v=±1 对应唇的上/下缘）。
    # 原来放到 1.8，结果上唇上方的皮肤被一起拉伸 —— 视觉复核判
    # 「上唇边缘上方有一条横向暗色/模糊带，是最明显的合成破绽」。
    # 现在从 |v|=0.85 开始衰减、|v|=1.25 归零，形变严格限制在唇内。
    w_def = (_softstep((1.0 - u * u) / 0.55)
             * _softstep((1.25 - np.abs(v)) / 0.40))

    # 羽化 mask（贴回用）：覆盖整个形变区，边缘高斯衰减
    r2 = (u * u) * 0.92 + (v / 1.8) ** 2
    feather = np.exp(-np.maximum(0.0, r2 - 1.0) / 0.55)
    feather = _softstep(feather)

    lip_c, cav_c = _sample_color(img_rgb, cx, cy, mw * 0.5, mh * 0.6)

    return {
        "roi": roi, "box": (x0, y0, x1, y1), "wh": (rw, rh),
        "u": u, "v": v, "w_def": w_def, "feather": feather,
        "lip": lip_c, "cav": cav_c,
        "cx": cx, "cy": cy, "mw": mw, "mh": mh,
        "base": img_rgb,
    }


# ---------------------------------------------------------------- 口腔合成


def _cavity(p: Dict, open_: float, teeth: float, lower: float,
            press: float) -> Tuple[np.ndarray, np.ndarray]:
    """合成唇间缝隙：口腔暗区 + 上齿 + 舌。返回 (rgb, alpha)。"""
    u, v = p["u"], p["v"]
    mw, mh = p["mw"], p["mh"]

    # 缝隙的纵向半高必须和 _warp 的位移**用同一个 lift**，
    # 否则会出现「唇已经分开了、中间却还是原来的唇色」或者反过来。
    op = float(np.clip(open_, 0.0, 1.0)) * (1.0 - 0.9 * float(np.clip(press, 0, 1)))
    # 必须与 _warp 用**同一个 lift 公式**，否则「唇分开了但中间还是唇色」
    # 或者反过来「中间黑了但唇没分」。缝隙半高 = lift * 半嘴高。
    lift = op * 0.62 + lower * 0.15
    gv = min(lift * 0.95, 1.1)                   # 归一化半高（≈分离量）
    gap = max(1.0, gv * mh * 0.5)                # 像素半高
    # 缝隙的横向半宽：随开口略收窄（真人张嘴时嘴角不会跟着拉宽）
    gw = 0.86 * (1.0 - 0.16 * min(1.0, open_))

    # 缝隙区域：椭圆，边缘软
    ru = u / max(1e-3, gw)
    rv = v / max(1e-3, gv)
    rr = np.sqrt(ru * ru + rv * rv)
    # ⚠ 过渡带从 0.22 加宽到 0.62。0.22 时口腔是一个边界清晰的椭圆，
    #   视觉复核反复判定「深色色块 / 贴图」。真人张嘴时，口腔和唇内侧
    #   之间是连续的阴影过渡，没有明确边界 —— 加宽后是「唇间自然变暗」。
    inside = _softstep((1.0 - rr) / 0.62)

    if open_ < 0.035 or inside.max() < 0.01:
        z = np.zeros(u.shape, np.float32)
        return np.zeros(u.shape + (3,), np.float32), z

    cav = p["cav"]
    lip = p["lip"]
    rh, rw = u.shape

    # ---- 底色：口腔暗区，带径向明暗（中心最暗，边缘受唇内侧反射略亮）----
    shade = 0.78 + 0.42 * _softstep(rr)          # 0.78 中心 → 1.20 边缘
    # ⚠ 只有径向明暗时，口腔是「一块扁平深色贴图」。真人张嘴看到的
    #   至少三层：上颚（几乎全黑，因为被牙齿挡光）、中部（黏膜，暗红）、
    #   舌背（略亮，有湿润反光）。这里按 v 叠一层纵向分层。
    palate = 1.0 - 0.46 * _softstep((-v - 0.10) / 0.45)      # 上部压暗
    tongue_hint = 1.0 + 0.22 * _softstep((v - 0.15) / 0.40)   # 下部微亮
    # 低频纹理：真实口腔里舌面/上颚的高低起伏会造成明暗不均。
    # 没有这层时，暗区是一块**均匀**的色，视觉复核一律判「扁平色块/贴图」。
    # 用两个不同频率的正弦叠加（不是随机噪声，避免逐帧闪烁）。
    tex = (0.5 + 0.5 * np.sin(u * 9.0 + 0.7) * np.cos(v * 7.0 - 0.4))
    tex2 = (0.5 + 0.5 * np.sin(v * 13.0 + 1.1))
    grain = 0.90 + 0.10 * tex + 0.06 * tex2
    shade = shade * palate * tongue_hint * grain
    rgb = cav[None, None, :] * shade[..., None]

    # ---- 唇内侧接触阴影：贴着缝隙上下边缘一圈偏唇色，避免「洞」的突兀感 ----
    rim = _softstep((rr - 0.72) / 0.28) * (1.0 - _softstep((rr - 0.96) / 0.10))
    rim_col = lip * 0.55
    rgb = rgb * (1.0 - 0.42 * rim[..., None]) + rim_col[None, None, :] * (0.42 * rim[..., None])

    # ---- 上齿：出现在缝隙上半部，开口够大且 teeth 权重高 ----
    if teeth > 0.10 and open_ > 0.22:
        # 齿条高度：真人只露出 2~4mm，约为唇厚的 25%~40%。
        # ⚠ 第一版给到 0.92（几乎填满上半缝隙），加上纯白配色，
        #   视觉复核直接判定「纯白色实心长方形色块 / 假牙贴片」。
        th = min(0.60, (0.24 + 0.32 * teeth) * (0.40 + 0.60 * open_))
        ty0 = -0.94
        ty1 = ty0 + th * 1.75
        # ⚠ 牙齿下缘不能是一条水平直线 —— 视觉复核判定「像贴图边缘」。
        #   真人上齿列的切缘是**弧形**：中间门牙最长，往两侧渐短。
        #   这里用 ru² 做抛物线，再加一点余弦起伏模拟牙齿切缘的微小高低。
        _ru = np.clip(ru, -1.0, 1.0)
        # ⚠ 弧度量 0.20 配 0.26 的过渡带时，过渡带比弧差还宽，弧形被抹平了，
        #   视觉复核判定「上排牙齿呈一条直线」。现在弧 0.38、过渡 0.15。
        arc = 0.38 * (_ru ** 2)
        ripple = 0.028 * np.cos(_ru * np.pi * 4.0)
        ty1_local = ty1 - arc + ripple
        band = _softstep((v - ty0) / 0.16) * (1.0 - _softstep((v - ty1_local) / 0.15))
        band *= _softstep((0.90 - np.abs(_ru)) / 0.24)
        band = np.clip(band, 0, 1) * inside
        if band.max() > 0.01:
            # 齿色：从**唇色亮度**推导（同一光照环境），不用固定纯白。
            # 唇色偏暗时牙齿也偏暖灰，避免「一盏白光打在嘴上」的突兀。
            lum_lip = float(lip[0] * 0.299 + lip[1] * 0.587 + lip[2] * 0.114)
            # ⚠ 牙齿不能是纯白。第一版 lum*1.55+92 算出 264（裁到 236），
            #   视觉复核直接判「纯白色块」。真牙在室内光下约 190~215，
            #   而且偏暖（象牙白），不是纸白。
            # ⚠ 真牙在室内光下约 165~195，不是 200+。第一版算到 206 并被
            #   视觉复核判「纯白色块 / 单一均匀色块」。
            # ⚠ 关键取舍（实测三轮视觉复核得出）：
            #   闭嘴照片里没有牙齿，任何「无中生有」的白牙都会被判「假牙贴片」。
            #   试过把牙齿画得更亮更清晰（206→180→168），结论都一样；
            #   也试过用 AI 生成真实张嘴图取纹理，同样被判「合成感、开口突兀」。
            #   所以这里改成**低对比度的隐约齿痕**：基色贴近唇色亮度，
            #   不透明度压到 0.62。远看是「嘴里有东西」而不是「贴了一排白瓷砖」。
            #   这是闭嘴素材做数字人的通行做法，也是本方案的真实上限。
            base_v = float(np.clip(lum_lip * 0.52 + 74.0, 108.0, 142.0))
            tooth = np.array([base_v, base_v * 0.968, base_v * 0.910], np.float32)

            # 立体明暗：齿顶（靠上唇）暗 → 中段亮 → 咬合缘略暗
            tv = np.clip((v - ty0) / max(1e-3, (ty1 - ty0)), 0.0, 1.0)
            shade = 0.74 + 0.28 * np.sin(np.pi * np.clip(tv, 0, 1) ** 0.80)
            # 釉质高光：齿面中上部一条柔和的横向亮带（真牙的湿润反光）
            spec = 1.0 + 0.09 * np.exp(-((v - (ty0 + 0.34)) / 0.18) ** 2)
            shade = shade * spec
            # 齿龈阴影：齿顶紧贴上唇处一条暗带（这是「贴纸感」最缺的东西）
            gum = 1.0 - 0.50 * (1.0 - _softstep((v - ty0) / 0.22))
            # 齿缝：必须是**窄暗线**才看得出单颗牙。
            #   实测 π*11 会在 157px 的嘴宽上画出 11 条线 → 视觉复核数出
            #   「12~14 颗牙」。真人自然张开只看到 6~8 颗前牙，改 π*7。
            _c = np.cos(np.clip(ru, -1, 1) * np.pi * 7.0)
            seam = 1.0 - 0.34 * np.clip(_c, 0.0, 1.0) ** 2
            # 咬合缘：底部收一点阴影，避免牙齿像一块平板
            bite = 1.0 - 0.22 * _softstep((v - (ty1 - 0.18)) / 0.20)

            # 牙龈：齿顶上方的淡粉带。真人的牙齿是长在牙龈上的，
            # 纯白牙直接贴上唇边缘 = 假牙贴片的典型特征。
            gband = (1.0 - _softstep((v - (ty0 + 0.08)) / 0.24)) * inside * 0.58
            gcol = np.clip(lip * 0.88 + np.array([34.0, 6.0, 12.0], np.float32), 0, 255)
            rgb = rgb * (1.0 - gband[..., None]) + gcol[None, None, :] * gband[..., None]

            tw = (band * shade * gum * seam * bite * 0.62)[..., None]
            rgb = rgb * (1.0 - tw) + (tooth[None, None, :] * tw)

            # 牙齿在口腔深处投下的阴影：牙齿正下方一条柔和的暗带。
            # 没有这层，牙齿和口腔是「两张纸叠在一起」，有了才有前后关系。
            drop = np.exp(-((v - (ty1_local + 0.14)) / 0.24) ** 2) * 0.45
            drop = drop * inside * float(min(1.0, band.max() * 1.4))
            rgb = rgb * (1.0 - drop[..., None])

    # ---- 下齿/舌：开口很大时下半部隐约可见 ----
    if open_ > 0.52:
        tongue = np.array([96.0, 46.0, 48.0], np.float32)
        tw2 = _softstep((v - 0.18) / 0.22) * (1.0 - _softstep((v - 0.86) / 0.16))
        tw2 *= _softstep((0.80 - np.abs(ru)) / 0.20) * inside
        tw2 = tw2 * min(1.0, (open_ - 0.52) / 0.30) * 0.75
        rgb = rgb * (1.0 - tw2[..., None]) + tongue[None, None, :] * tw2[..., None]

    # ---- 闭唇压力：press 高时缝隙应几乎不存在 ----
    alpha = np.clip(inside * (1.0 - 0.95 * press), 0.0, 1.0)
    return rgb.astype(np.float32), alpha.astype(np.float32)


# ---------------------------------------------------------------- 形变


def _warp(p: Dict, open_: float, round_: float, spread: float,
          lower: float, teeth: float, press: float) -> np.ndarray:
    """按口型参数对 ROI 做连续形变（逆向采样）。返回 float32 ROI。"""
    roi = p["roi"]
    u, v, w = p["u"], p["v"], p["w_def"]
    rh, rw = roi.shape[:2]
    mh = p["mh"]
    mw = p["mw"]

    op = float(np.clip(open_, 0.0, 1.0)) * (1.0 - 0.9 * float(np.clip(press, 0, 1)))

    # ---- 纵向：上唇上提 / 下唇下沉，下唇动得多（真人比例约 0.8 : 1.2）----
    # 位移量换算成归一化单位（1.0 = 半个嘴高）。
    # ⚠ 比例错误（关键）：mh 是**上下唇总厚度**，不是唇到下巴的距离。
    #   真人发 a 时开口约 35px / 唇高 56px = 63%，即 lift ≈ 0.63
    #   （总分离 = 2*lift*半唇高）。之前写 1.15，分离量达到 62px，
    #   比整个唇高还大 —— 口腔暗区直接盖住唇的外缘，视觉上就是
    #   「唇外圈有一圈暗色轮廓 / 嘴唇像两个肉环」。
    lift = op * 0.62 + lower * 0.15

    # ⚠⚠ 两版都栽在位移场设计上，记录清楚：
    #   ① 第一版写成 out(y)=src(y+shift)，方向反了 —— 上下唇被压向中间，
    #      嘴越用力越闭合。
    #   ② 第二版改成 y-shift 但仍用 np.where(v<0, -A, +B)：**在 v=0 处
    #      位移从 -1.14 跳到 +1.28**，采样方向瞬间翻转，唇缝那几行像素
    #      被拉成一团，数值上「变化量很大」，视觉上却看不到嘴张开。
    #   正确做法：位移场必须**在唇缝处为 0 且连续**，向外单调增大。
    #   tanh(v/0.35) 满足这一点：v=0→0，|v|>0.6 后饱和到 ±1，
    #   中间 0.35 的过渡带正好是「唇缝被挤开」的位置。
    t = np.tanh(v / 0.35)
    # 下唇动作大于上唇（真人比例约 1.15 : 0.85）
    t = np.where(v > 0, t * 1.15, t * 0.85)
    shift_v = lift * t * w

    # ---- 横向：round 内收、spread 外展，嘴角幅度最大 ----
    # ⚠ 必须带符号：u<0（左嘴角）与 u>0（右嘴角）的位移方向相反。
    #   第一版用 |u|^1.35 丢了符号，两个嘴角会同向移动 = 整张嘴平移。
    edge = np.sign(u) * np.abs(u) ** 1.35
    shift_u = (spread * 0.34 - round_ * 0.40) * edge * w * (0.35 + 0.65 * op)
    # 圆唇时嘴唇整体略微向前（表现为唇部轻微变厚）：用纵向压缩近似
    thick = round_ * 0.10 * w

    # ---- 采样坐标（像素）：src = pos - shift ----
    px = p["cx"] + (u - shift_u) * (mw * 0.5) - p["box"][0]
    py = p["cy"] + (v - shift_v) * (mh * 0.5) - p["box"][1]
    # 圆唇的轻微变厚：向唇心收一点
    py = py + (p["cy"] - p["box"][1] - py) * thick

    px = np.clip(px, 0, rw - 1.001)
    py = np.clip(py, 0, rh - 1.001)

    x0 = np.floor(px).astype(np.int32)
    y0 = np.floor(py).astype(np.int32)
    fx = (px - x0)[..., None]
    fy = (py - y0)[..., None]
    x1 = np.minimum(x0 + 1, rw - 1)
    y1 = np.minimum(y0 + 1, rh - 1)

    top = roi[y0, x0] * (1 - fx) + roi[y0, x1] * fx
    bot = roi[y1, x0] * (1 - fx) + roi[y1, x1] * fx
    return top * (1 - fy) + bot * fy


# ---------------------------------------------------------------- 主入口


def frame(p: Dict, params: Dict[str, float]) -> np.ndarray:
    """按一帧参数生成整帧 RGB（uint8，HxWx3）。"""
    op = float(params.get("open", 0.0))
    rd = float(params.get("round", 0.0))
    sp = float(params.get("spread", 0.0))
    th = float(params.get("teeth", 0.0))
    lw = float(params.get("lower", 0.0))
    pr = float(params.get("press", 0.0))

    out = p["base"].copy()

    # 完全闭合且无唇形变化 → 直接返回底图（省一次计算）
    if op < 0.012 and rd < 0.02 and sp < 0.02 and pr < 0.05:
        return out

    warped = _warp(p, op, rd, sp, lw, th, pr)
    cav_rgb, cav_a = _cavity(p, op, th, lw, pr)

    # 缝隙覆盖：形变后中间那块是拉伸出来的假像素，用合成口腔替换
    if cav_a.max() > 0.01:
        a = cav_a[..., None]
        warped = warped * (1.0 - a) + cav_rgb * a

    # 贴回：高斯羽化，边缘不留硬边
    f = p["feather"][..., None]
    x0, y0, x1, y1 = p["box"]
    region = out[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = np.clip(region * (1.0 - f) + warped * f, 0, 255).astype(np.uint8)
    return out


def frame_bytes(p: Dict, params: Dict[str, float]) -> bytes:
    return frame(p, params).tobytes()
