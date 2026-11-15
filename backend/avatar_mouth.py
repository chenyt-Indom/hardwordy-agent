"""原图像素口型形变引擎（卡通 / 3D 形象专用）

## 为什么需要它

Wav2Lip 的嘴部输出只有 **96x96**，贴回 1024 级原图要放大 5~14 倍。
实测（电信小翼 3D 卡通形象）：

    贴回区拉普拉斯方差    400 ~ 680     ← 糊
    原图像素形变           2772 ~ 4257  ← 清晰 4~6 倍
    原图本体               3751

也就是说「贴 Wav2Lip 像素」这条路，清晰度天花板由 96x96 决定，
换任何融合算法（均值色匹配 / 直方图匹配 / 拉普拉斯金字塔 / 超分）
都补不回来 —— 实测超分只从 45.5 提到 45.8，却慢 0.53 秒/帧。

更致命的是**画风**：Wav2Lip 训练在真人脸上，输出的是真人皮肤质感。
贴在 3D 卡通的大色块上，视觉上就是「一块粉色涂抹补丁」。
实测 Lab 色差 35~39，视觉复核判定「扁平、没有高光和阴影、像补丁」。

所以对卡通形象，正确做法是**不用 Wav2Lip 的像素，只用它的开合时序**，
然后用**原图自己的像素**做纵向形变 —— 画风 100% 一致，色差 ≈ 0，
清晰度保持原图水平。

## 形变模型

以源图为 k=1.0（最大张开），k 越小嘴越闭：

    y < 唇缝          → 不动
    唇缝 ~ 下唇下缘    → 整体上移 (1-k) * 开口高度
    下唇下缘以下       → 上移量线性衰减到 0（避免下巴/脖子被整体拖动）

横向用 cos 衰减窗限制在嘴部附近，避免整张脸变形。
全程一次 `cv2.remap`（CUBIC），不叠加多次插值 —— 多次插值就是「动态模糊」的根源。

## 实测范围

k ∈ [0.78, 1.0] 视觉自然（k<0.7 时牙齿会糊成白条、舌头边缘涂抹，
视觉复核判定「生硬」）。所以对外暴露 `K_MIN = 0.78`。
"""

from __future__ import annotations

import json
import os

import cv2
import numpy as np

# 形变下限：实测 k<0.78 时牙齿/舌头开始糊，视觉判定「生硬」
# （那是**只做几何压缩**时的结论；配合下面的口腔覆盖后，k 可以更低）
K_MIN = 0.32

# 口腔覆盖：k >= COVER_ON 完全不盖（嘴是张开的，盖了就是错的）；
# k <= COVER_FULL 全量覆盖。中间线性过渡，避免闭嘴瞬间颜色突变。
COVER_ON = 0.62
# 满覆盖阈值必须**明显高于** LLM 常用的 k_min，否则最低开合时仍残留一点牙齿，
# 视觉上就是唇缝里一条白色亮线。
# ★ 2026-11-15 实测踩到：原值 0.34，而 LLM 给 k_min=0.35 —— 差 0.01 就让满覆盖
#   不启动，只剩 96% 覆盖，4% 牙齿残留被 DeepSeek 判成「牙齿变成一条模糊的灰白
#   细线、有明显横向拉伸条纹」。抬到 0.48 后 k_min=0.35 能拿满覆盖。
COVER_FULL = 0.48
# 口腔与肤色的亮度差判据（占肤色亮度的比例）
_HOLE_DARK_RATIO = 0.72


def _img_gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)


def analyze(img, mouth_cfg, verbose=False):
    """从原图自动分析嘴部纵向几何。

    返回 dict:
        cx, cy      嘴中心（像素）
        mw, mh      嘴部标定宽高（像素）
        y_split     唇缝（口腔上界）
        y_hole_bot  口腔下界（下唇上缘）
        y_lip_bot   下唇下缘
        y_fade      位移衰减终点（下唇下方）
        open_h      开口高度 y_hole_bot - y_split
        source_open 源图是否张着嘴

    判据用**逐行暗像素占比**，不依赖固定比例：先取嘴部小框，算每行
    「明显暗于肤色」的像素占比，占比最高的连续区段就是口腔。
    这样闭嘴图（没有暗区）会得到 open_h≈0、source_open=False。
    """
    H, W = img.shape[:2]
    cx = float(mouth_cfg["x"]) * W
    cy = float(mouth_cfg["y"]) * H
    mw = max(8.0, float(mouth_cfg["w"]) * W)
    mh = max(8.0, float(mouth_cfg["h"]) * H)

    # 搜索框：比标定稍大，覆盖唇周。
    # ⚠ 必须**偏上**：mouth.json 的 y 标定的是下唇一带（实测标定 cy=854，
    # 而真实口腔在 799~841），如果上下对称取框，会一直搜到下巴下方的
    # 纯黑描边/衣服（实测那片 dark_ratio=100%），argmax 直接跑偏 96px。
    bx1 = int(max(0, cx - mw * 0.75))
    bx2 = int(min(W, cx + mw * 0.75))
    by1 = int(max(0, cy - mh * 1.55))
    by2 = int(min(H, cy + mh * 0.65))
    roi = img[by1:by2, bx1:bx2]
    if roi.size == 0:
        return None
    gray = _img_gray(roi)

    # 肤色参考：搜索框上下两条边带（避开嘴）
    band = max(3, int(mh * 0.18))
    ref = np.concatenate([gray[:band].ravel(), gray[-band:].ravel()])
    skin = float(np.median(ref))
    thr = skin * _HOLE_DARK_RATIO

    dark_mask = (gray < thr)
    dark_ratio = dark_mask.mean(axis=1)             # 每行暗像素占比
    # ★ 横向跨度：区分「口腔」和「护目镜/描边/阴影」的关键判据。
    #   实测这张 3D 卡通（787x1400）：护目镜下方到嘴巴之间是一片连续偏暗区，
    #   暗占比 40~72%，和口腔**无法用亮度区分**；但它横向撑满整个搜索框
    #   （跨度/框宽 = 1.00），而真正的口腔只有 47px（跨度比 0.50）。
    #   旧版只看暗占比，于是把这片区域整个当成口腔，向上扩展一路跑到框顶
    #   （实测 y_split=801，真值 842，偏 41px，open_h 37 vs 真值 18）。
    #   结果形变从**护目镜**开始往下压，护目镜/鼻子/上唇一起被压扁 ——
    #   这就是「割裂感、变形」的真正来源，不是融合参数问题。
    _ci = np.arange(gray.shape[1], dtype=np.int32)[None, :]
    _left = np.where(dark_mask, _ci, 10 ** 6).min(axis=1)
    _right = np.where(dark_mask, _ci, -1).max(axis=1)
    _span = np.where(_right >= _left, _right - _left + 1, 0).astype(np.float32)
    span_ratio = _span / float(max(1, gray.shape[1]))
    # 平滑一下再找峰，避免单行噪声
    dr = cv2.GaussianBlur(dark_ratio.reshape(-1, 1).astype(np.float32), (1, 5), 0).ravel()
    # 排除三类不是口腔的行：
    #   ① 整行全暗（背景/描边/衣服，暗占比 >90%）
    #   ② 横向撑满框（护目镜/脸部描边，跨度比 >0.85）← 本次新增，关键
    #   ③ 暗占比太低（根本不在暗区）
    _cand0 = np.where((dr >= 0.12) & (dr <= 0.90) & (span_ratio <= 0.85))[0]
    # ★ 2026-11-15 用户报「全乱了、位置偏离太过严重」的根治。
    #   旧逻辑：暗占比+跨度比筛选后，取**离预期中心最近的峰**。
    #   实测这张 3D 卡通（787x1400）在大框下会同时命中两条带：
    #     口腔     y=845~852（段宽 47→40px，段中心 x=390 ≈ 框中心）
    #     下唇阴影 y=878~881（段宽 124→58px）
    #   旧逻辑挑到了下唇阴影（y_split=877，真值 845，偏 32px），
    #   形变于是从下巴开始压 —— 用户看到的就是「位置偏离太过严重」。
    #   根因是 mouth.json 被 yunet 写错（cy=921，真值 853）：框一偏，
    #   「预期中心」跟着偏，最近峰必然选错。
    # 新增两条**与框尺寸无关**的判据：
    #   · 段宽绝对上限：口腔暗段是物理尺寸（实测 47px），护目镜/描边/衣服
    #     是 174~210px。用 max(60, 0.55*mw) 作上限，框变大也不受影响。
    #   · 段中心对齐：口腔暗段一定居中在嘴部框里（实测距框中心 1~2px），
    #     护目镜下沿偏左 63px（=0.56*mw）。
    # 再按**解剖约束**取最靠上的连续带：口腔一定在下唇阴影之上。
    _framec = cx - bx1
    _segc = np.where(_right >= _left, (_left + _right) / 2.0, -1e6)
    _span_lim = max(60.0, mw * 0.55)
    _okrow = ((dr >= 0.12) & (dr <= 0.90) & (span_ratio <= 0.85)
              & (_span <= _span_lim)
              & (np.abs(_segc - _framec) <= max(6.0, mw * 0.35)))
    cand = np.where(_okrow)[0]
    if cand.size:
        groups = []
        _cur = [int(cand[0])]
        for _i in cand[1:]:
            _i = int(_i)
            if _i == _cur[-1] + 1:
                _cur.append(_i)
            else:
                groups.append(_cur)
                _cur = [_i]
        groups.append(_cur)
        _g = ([x for x in groups if len(x) >= 4]
              or [x for x in groups if len(x) >= 2] or groups)
        _g.sort(key=lambda x: (x[0], -float(np.mean([dr[i] for i in x]))))
        _g0 = _g[0]
        peak = int(_g0[int(np.argmax([dr[i] for i in _g0]))])
    elif _cand0.size:
        # 兜底：没有合格带时，仍用「离预期中心最近」的老办法
        target = cy - mh * 0.35 - by1
        peaks = []
        for i in _cand0:
            if dr[i] >= dr[max(0, i - 1)] and dr[i] >= dr[min(len(dr) - 1, i + 1)]:
                peaks.append(i)
        pool = peaks if peaks else list(_cand0)
        peak = int(min(pool, key=lambda i: abs(i - target)))
    else:
        peak = int(np.argmax(dr))
    peak_val = float(dr[peak])
    if peak_val < 0.12:
        # 没有明显暗区 → 源图闭嘴
        open_h = 0.0
        y_split = cy - mh * 0.10
        y_hole_bot = cy + mh * 0.10
        y_lip_bot = cy + mh * 0.42
    else:
        # 扩展时同样受「横向跨度」约束：护目镜/描边那片区域虽然暗占比达标，
        # 但跨度比接近 1.0，必须在这里挡住 —— 否则峰值找对了、扩展又会跑偏。
        def _ok(i):
            # 2026-11-15：扩展同样要受「段宽上限 + 段中心对齐」约束。
            # 否则峰值找对了、扩展又会一路吃进下唇阴影（实测 hole_bot 881）
            # 甚至衣服（947），y_fade 跟着越过衣服边界。
            return (0 <= i < len(dr) and dr[i] >= peak_val * 0.45
                    and span_ratio[i] <= 0.85
                    and _span[i] <= _span_lim
                    and abs(_segc[i] - _framec) <= max(6.0, mw * 0.35))
        lo = peak
        while _ok(lo - 1):
            lo -= 1
        hi = peak
        max_span = max(4, int(mh * 1.25))
        while _ok(hi + 1) and (hi + 1 - lo) < max_span:
            hi += 1
        while _ok(lo - 1) and (hi - lo + 1) < max_span:
            lo -= 1
        y_split = by1 + lo
        y_hole_bot = by1 + hi
        open_h = float(max(1, y_hole_bot - y_split))
        # 下唇下缘：从口腔下界往下，暗占比回落到 1/3 以下的位置
        base = float(np.median(dark_ratio))
        y_lip_bot = y_hole_bot + int(open_h * 0.42) + 4
        for y in range(y_hole_bot, min(len(dark_ratio), y_hole_bot + int(open_h * 1.2) + 6)):
            if dark_ratio[y] <= max(base * 1.3, peak_val * 0.22):
                y_lip_bot = by1 + y
                break
        y_lip_bot = int(min(y_lip_bot, by2 - 2))

    # ---- 剖面精修：暗占比法在「亮牙齿 + 中亮舌头」的嘴上会提前收手 ----
    # 实测这张 3D 卡通（张嘴露齿）：真实开口 840~862（22px），暗占比法只判出
    # 840~852（12px）——因为牙齿灰度 237、舌头 135~155 都不算「暗」；下唇下缘
    # 真值 881 也被判成 861。后果不只是数字不准：
    #   · close_by_lip 的 move = lip_h*ratio 只有 7.65px，填不满 22px 开口；
    #   · warp2d 的形变范围也跟着偏小，嘴巴「张不开 / 闭不上」。
    # 修正用中心列灰度剖面的两个解剖特征（全图剖面，因为 ROI 只到 cy+0.65mh，
    # 看不到下唇）：
    #   · 开口下沿 = 「中暗→亮」的最大正跳变所在行的前一行（舌头→下唇）
    #   · 下唇下沿 = 开口以下第一处暗线（下唇下沿描边）
    try:
        _full = _img_gray(img)
        _c = int(round(cx))
        _c = max(2, min(_full.shape[1] - 3, _c))
        # 采样加宽到 ±5px：mouth 框的 x 只差 3px（LLM 框 cx=391 vs 真值 388）
        # 单点采样就会错过「舌头→下唇」那个亮跳变，精修整段失效。
        _prof = _full[:, max(0, _c - 5):_c + 6].mean(axis=1)
        # 2026-11-15：窗口起点只跟 y_split 走，不再用 cy+0.20*mh。
        # 框一大（mh=44/60）时 cy+0.20mh 会越过真实开口底（860），
        # 精修窗口整段跳过开口，y_hole_bot 于是停在 881 / 947，
        # open_h 被放大到 37 / 104（真值 19）—— 形变幅度全跟着错。
        _lo = int(y_split) + 2
        _hi = min(H - 1, _lo + max(24, int(mh * 1.2)))
        # ★ 窗口必须收紧到 y_split + 0.8*mh。原来用 mh*1.2，框一大就把
        #   下唇下沿描边（y=881 暗线、y=882 跳 +70）也圈进窗口：
        #   实测 A 框（mh=44）→ hole_bot=882、错误框（mh=60）→ 947。
        #   收紧后窗口内最大跳变就是「舌头→下唇」（y=862，+86），
        #   C/A/B/错误/LLM 五种框实测全部落到 862（真值 860）。
        _hi = min(H - 1, _lo + max(20, int(mh * 0.8)))
        # 肤色参考取嘴周 ±120 行的 p90（≈脸颊亮度）。
        _zone = _prof[max(0, _lo - 120):min(H, _lo + 120)]
        _lit = float(np.percentile(_zone, 90))
        _best, _bd = None, 0.0
        for _y in range(_lo, _hi):
            # ★ 下唇下沿有一条**单行描边暗线**（实测 y=881 灰度 82.8），
            #   它到 y=882 的跳变是 +101.6，比真正的「舌头→下唇」（y=862，
            #   +86.4）还大 —— 只按「最大跳变」挑，框一大就选到它，
            #   hole_bot 变成 881，形变范围跟着多吃 20 行。
            #   解剖上开口底的两侧必须是：跳变前=口腔内的中暗，跳变后=亮唇。
            if _prof[_y] < _lit * 0.45 or _prof[_y + 1] < _lit * 0.75:
                continue
            _d = float(_prof[_y + 1] - _prof[_y])
            if _d >= 35.0:
                # ★ 取**第一个**合格跳变，不按幅度挑。
                #   实测 862（舌头→下唇）与 882（下唇描边→皮肤）的跳变幅度
                #   非常接近（±5 采样 81 vs 70、±11 采样 50 vs 63，后者反而更大），
                #   谁大完全取决于采样位置 → 按大小挑必然抖。
                #   解剖上开口底一定在下唇描边之上，取最靠上的那个才稳。
                _best, _bd = _y, _d
                break
        if _best is None:
            # 兜底：图很暗/口腔很暗时上面的亮度约束可能全落空，
            # 退回「窗口内最大跳变」，至少不比旧版差。
            for _y in range(_lo, _hi):
                _d = float(_prof[_y + 1] - _prof[_y])
                if _d > _bd:
                    _bd, _best = _d, _y
        if _best is not None and _bd > 40.0:
            y_hole_bot = int(_best)
            open_h = float(max(1, y_hole_bot - y_split))
        # 下唇下缘的窗口同样只跟 y_hole_bot 走（原来用 cy+0.85mh，
        # 框大时起点直接越过下唇，y_lip_bot 落到 900/976）。
        _lo2 = int(y_hole_bot) + 2
        _hi2 = min(H - 1, _lo2 + max(24, int(mh * 1.2)))
        for _y in range(_lo2, _hi2):
            if _prof[_y] < 120.0:
                y_lip_bot = int(_y)
                break
    except Exception:                                   # noqa: BLE001
        pass

    # ★ 衰减终点不能越过「下巴底」：原式 y_lip_bot + max(24, 1.6*open_h)，
    #   修正几何后 open_h=22 → y_fade=916，而这张图衣服从 y=896 就开始（纯黑），
    #   位移会把衣服一起拖动 → 视觉判「下巴最底部白色边缘生硬、轮廓锯齿」。
    # 2026-11-15：+12 → +8。warp2d 的位移在衰减区还会再向下延伸约 5px
    # （ramp 里减了 sh），893 实测会拖到 898，而这张图衣服从 896 就是纯黑，
    # 拖动会在下巴底留下生硬边缘。
    y_fade = int(min(H - 1, y_lip_bot + max(8, open_h * 0.42)))
    out = {
        "cx": cx, "cy": cy, "mw": mw, "mh": mh,
        "y_split": int(y_split),
        "y_hole_bot": int(y_hole_bot),
        "y_lip_bot": int(y_lip_bot),
        "y_fade": y_fade,
        "open_h": float(open_h),
        # ⚠️ 2026-11-15：判据原来是 max(4.0, mh*0.12)，太松。
        #   实测一张**双唇紧闭**的真人照（334×225，mh=15.9）量出 open_h=5.0，
        #   5 >= 4.0 就判成「源图张嘴」→ 渲染走 warp 形变，
        #   可闭嘴图没有口腔纹理可压，嘴几乎张不开（实测暗行只有 1~3 行，
        #   而 w2l 版是 3~9 行），等于选了一版不会动的。
        #   改成「开口高度至少占嘴框高的 32%」才算真张嘴：
        #   闭嘴照 5/15.9=31% → False；大笑卡通 22/26=85% → True。
        #   拿不准时判 False 更安全 —— warp 要求源图**明显**张开，
        #   否则退回 w2l（由模型生成口腔），不会出黑块硬边。
        "source_open": bool(open_h >= max(5.5, mh * 0.32)),
        "skin_gray": skin,
        "hole_thr": thr,
        # ★ 记住几何是在**哪张图**上算出来的。objective_score 拿去量视频帧时，
        #   帧可能已经被 resize 过（实测形象 787 宽 → 成片 574 宽），
        #   不换算就会把椭圆量到头发上，两条路径分数几乎一样（8.4 vs 8.5），
        #   自动选优等于瞎猜。
        "img_w": int(W), "img_h": int(H),
    }
    if verbose:
        print("[avatar_mouth] analyze:", json.dumps(
            {k: (round(v, 1) if isinstance(v, float) else v) for k, v in out.items()},
            ensure_ascii=False))
    return out


def _displace_map(H, W, geom, shift, fade=None, diff_report=None):
    """构造纵向映射场：唇缝以下按比例**压缩**，压缩量 = shift。

    ⚠ 这里必须是「压缩」而不是「平移」。第一版写成整体上移
    `map_y = y + shift`，结果口腔只是位置上移、尺寸完全没变 ——
    实测暗区面积只从 1736 降到 1577（-9%），形变几乎看不出来。
    正确做法是把 [y_split, y_lip_bot] 这段内容按比例映射进
    [y_split, y_lip_bot - shift]，内容真的被压矮。

    压缩区之外（y > y_lip_bot）用线性 ramp 把剩余位移衰减到 0，
    避免下巴和脖子被整体拖动（那就是「脖子割裂」）。
    """
    cx = geom["cx"]; mw = geom["mw"]
    y_split = float(geom["y_split"]); y_lip = float(geom["y_lip_bot"])
    y_fade = float(geom.get("y_fade", y_lip + 40) if fade is None else fade)
    ys = np.arange(H, dtype=np.float32)[:, None]
    xs = np.arange(W, dtype=np.float32)[None, :]

    y_hole = float(geom["y_hole_bot"])
    open_h = max(1.0, y_hole - y_split)
    lip_h = max(1.0, y_lip - y_hole)
    sh = float(shift)

    # 分段映射（remap 语义：dst(y) 取 src(map_y)）：
    #   A 唇缝以上            → 原样
    #   B 口腔 [split, hole]  → 压缩到 [split, hole-sh]（只有口腔变矮）
    #   C 下唇 [hole, lip]    → 保持厚度，整体上移 sh
    #   D 下巴及以下          → 上移量按 ramp 衰减到 0
    #
    # ⚠ 不能把口腔和下唇用同一个比例压缩：那样下唇会一起变薄，
    # 卡通形象的嘴唇是硬边色块，变薄后唇线直接消失。
    # ⚠ 2026-11-15 修「嘴周长出皱纹」：B 段的定义域原来写成 [y_split, y_hole]，
    # 但 B 段映射会把 open_h 压成 open_h-sh，所以它的**目标**区间只有
    # [y_split, y_hole-sh] 这么长。用 y_hole 当上界会让多出来的那 sh 行
    # 继续按 1/b_ratio 外推 —— 目标 y=852 去取源 y=877.5（下巴/脖子），
    # 而下一行 y=853 又跳回源 861（下唇），源坐标倒退 16.3px。
    # remap 遇到非单调的源坐标就是「折叠」：同一批源像素被重复采样成
    # 横向条纹，也就是肉眼看到的皱纹。C/D 段的上界同样要减 sh 才连续。
    a = ys < y_split
    b = (ys >= y_split) & (ys <= y_hole - sh)
    c = (ys > y_hole - sh) & (ys <= y_lip - sh)
    d = ys > y_lip - sh

    map_y = ys.copy()
    # B：目标 [split, hole-sh] → 源 [split, hole]
    # 下限 0.22：sh 接近 open_h 时 1/b_ratio 会到 20 倍，重度下采样会出摩尔纹。
    # 压不动的那部分交给 _cover_cavity 覆盖，而不是硬拉。
    b_ratio = max(0.22, (open_h - sh) / open_h)
    map_y = np.where(b, y_split + (ys - y_split) / b_ratio, map_y)
    # C：下唇保持厚度，整体平移 sh（sh<0 时是外推下移）
    map_y = np.where(c, ys + sh, map_y)
    # D：ramp 衰减。起点跟着 C 段上界一起移，否则接缝处又断一次
    ramp = np.clip((y_fade - sh - ys) / max(1.0, y_fade - y_lip), 0.0, 1.0)
    map_y = np.where(d, ys + sh * ramp, map_y)
    map_y = np.where(a, ys, map_y)

    # 横向 cos 窗：只在嘴部附近生效（否则整张脸跟着变形）
    wx = np.clip(1.0 - (np.abs(xs - cx) / max(1.0, mw * 1.08)) ** 2, 0.0, 1.0)
    # 纵向位移量随横向权重收缩：map_y 从「原样」过渡到「压缩后」
    out = ys + (map_y - ys) * wx
    return out.astype(np.float32)


def warp(img, geom, k, k_min=None):
    """按开合量 k（1=源图最大张开，越小越闭）做纵向形变。

    只用一次 remap，不叠加插值 —— 这是「不产生动态模糊」的关键。

    k_min：能压到的最小开合比例。默认取模块常量 K_MIN=0.78（照真人调的）。
    卡通形象嘴张得大、可压空间多，压到 0.74 说话起伏更自然；源图本身
    就闭着嘴的则不该硬压（上层会把它抬到 0.85~0.92）。所以这个值必须
    能由外部（LLM 调参）传进来，不能再写死。
    """
    H, W = img.shape[:2]
    open_h = float(geom.get("open_h") or 0.0)
    if open_h <= 0.5:
        return img.copy()
    try:
        lo = float(k_min) if k_min is not None else K_MIN
    except (TypeError, ValueError):
        lo = K_MIN
    lo = float(np.clip(lo, 0.55, 0.98))
    k = float(np.clip(k, lo, 1.0))
    shift = (1.0 - k) * open_h
    if shift < 0.35:
        return img.copy()
    map_y = _displace_map(H, W, geom, shift)
    map_x = np.broadcast_to(np.arange(W, dtype=np.float32)[None, :], map_y.shape)
    return cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_CUBIC,
                     borderMode=cv2.BORDER_REFLECT_101)


def warp2d(img, geom, k, k_min=None, squeeze=0.30, round_=0.0, round_gain=0.22,
           cover=None, seam_depth=1.00, seam_curve=0.45, lip_tint=0.85,
           cover_feather=0.30, k_max=1.0):
    """二维形变：纵向压缩 + **横向收拢**。

    ## 为什么需要横向

    旧版只做纵向压缩（squash）。实测这张 3D 卡通的 5 档对比，视觉复核
    明确指出：「真实的闭唇动作通常伴随嘴唇的聚拢和突出，而这张图采用了
    简单的垂直缩放，导致唇形变得极度扁平且**向两侧拉扯**」—— 也就是
    越压越扁、看起来像贴图被强行挤压，而不是嘴巴在闭合。

    真实的口轮匝肌收缩是**两个方向同时发生**的：
        纵向：上下唇靠近（口腔变矮）
        横向：嘴角向内收（嘴变窄）

    加上横向收拢后，同样 5 档的视觉复核变成：「嘴角也微微向中心靠拢，
    符合真实闭嘴时口轮匝肌收缩的物理规律…… k=0.85/0.72 比单纯的垂直
    压扁更自然，因为横向收拢保留了嘴唇的立体感」。

    ## 两个容易搞反的地方（都实测踩过）

    1. **remap 方向**：`dst(x) = src(map_x(x))`。要让嘴巴变窄，dst 的嘴角
       必须去取**更靠外**的源像素，所以 `scale > 1`。第一版写成
       `scale = 1 - ...`（<1），结果嘴巴反而变宽了。
    2. **权重形状**：要「嘴角收得最多、中心不动」，权重得用 `u²`
       （u 为到中心的归一化距离）；用 `1-u²` 会让中心收得最多、嘴角几乎
       不收 —— 实测嘴宽只降 5%，等于没做。

    squeeze：横向收拢强度（LLM 按画风给，圆脸/宽嘴要更大）。
    """
    H, W = img.shape[:2]
    open_h = float(geom.get("open_h") or 0.0)
    if open_h <= 0.5:
        return img.copy()
    try:
        lo = float(k_min) if k_min is not None else K_MIN
    except (TypeError, ValueError):
        lo = K_MIN
    lo = float(np.clip(lo, 0.10, 0.98))
    # ★ 2026-11-15：k 的上限原来是 1.0，而 k=1.0 就是**原图本身** ——
    # 所以「张嘴最大」永远等于源图开度，open_gain 调多高都突破不了。
    # 这正是「口型幅度拉高但效果不明显」的根因。放开到 k_max 后，
    # k>1 表示 shift<0（外推）：口腔被纵向拉伸，嘴真的能张得比源图更大。
    try:
        # 上限对齐前端滑块的 2.0；下限 0.10 让 k_max<1（用户想更含蓄）也能生效。
        # 之后再用 max(lo, ...) 保证 k_max 不低于 k_min，否则 clip 区间会反转。
        km = float(np.clip(k_max, 0.10, 2.00))
    except (TypeError, ValueError):
        km = 1.0
    km = max(lo, km)
    k = float(np.clip(k, lo, km))
    shift = (1.0 - k) * open_h
    # 压缩别超过 open_h*0.78（1/b_ratio 会到 4.5 倍以上，出摩尔纹）；
    # 压不动的那部分交给下面的口腔覆盖。
    if shift > open_h * 0.78:
        shift = open_h * 0.78
    map_y = _displace_map(H, W, geom, shift)
    if abs(shift) < 0.35:
        return img.copy()
    try:
        sq = float(np.clip(squeeze, 0.0, 0.60))
    except (TypeError, ValueError):
        sq = 0.30
    # 圆唇：**独立于开口度**的横向收拢。
    # 为什么要单独一项：闭嘴时的收拢（squeeze 项）由 (1-k) 驱动，
    # 而圆唇（u/ü/o）是「嘴不一定闭，但嘴唇要撮圆」—— 用 (1-k) 驱动
    # 的话，说「乌」时 k 本来就小（0.30），收拢会过强；说「啊」时 k=1，
    # 收拢又是 0。必须解耦。
    try:
        rg = float(np.clip(round_gain, 0.0, 0.45))
        rd = float(np.clip(round_, 0.0, 1.0))
    except (TypeError, ValueError):
        rg, rd = 0.22, 0.0
    if sq > 0.01 or (rg > 0.01 and rd > 0.01):
        xs = np.arange(W, dtype=np.float32)[None, :]
        ys = np.arange(H, dtype=np.float32)[:, None]
        # 纵向权重：只覆盖**真实唇部高度带**（唇缝 → 唇下沿）+ 少量羽化。
        # ⚠ 2026-11-15 修「整个脸都在动」：原来以 y_mid 为中心、半径 mh*1.05，
        #   而 mh 是用户标定的嘴框高（这张图 41.7px，把下巴也圈进去了），
        #   实测收拢带扩到 y 819~906 —— 唇缝上方 25 行、下巴下方 17 行都跟着变窄。
        pad_y = max(3.0, float(geom["mh"]) * 0.18)
        y0 = float(geom["y_split"]) - pad_y
        y1 = float(geom["y_lip_bot"]) + pad_y
        wy = np.clip(np.minimum((ys - y0) / max(1.0, pad_y),
                                (y1 - ys) / max(1.0, pad_y)), 0.0, 1.0)
        u = (xs - float(geom["cx"])) / max(1.0, float(geom["mw"]) * 0.58)
        # 横向权重：嘴角最高，**嘴宽之外快速衰减到 0**。
        # ⚠ 2026-11-15 修「脸颊跟着变形」：原来 wx = clip(u*u, 0, 1)，|u|>1 之后
        #   饱和成 1.0 —— 也就是脸颊外侧一直按**最大**权重缩放。实测改动横向铺到
        #   cx±180px（382 列）、右脸颊 4641 个像素在动，这就是「整个脸都在动」。
        #   core 负责「嘴角最大」，fade 负责「超出嘴宽归零」。
        au = np.abs(u)
        core = np.clip(u * u, 0.0, 1.0)                  # 嘴角处=1
        fade = np.clip((1.45 - au) / 0.45, 0.0, 1.0)     # |u|>=1.45 完全不动
        wx = core * fade
        # ⚠ 下限原来是 1.0 —— 而 k>1（外推）时 (1-k)<0，这一项本来是**变宽**的，
        # 被 clip 到 1.0 就全削掉了，结果外推只剩纵向拉长，嘴变成细长条
        # （视觉复核：「纵向拉长，圆形感减弱」）。放开到 0.70 后，外推时
        # 嘴巴纵横同步变大，保持半圆的饱满轮廓。k<1 时 (1-k)>0，不受影响。
        scale = np.clip(1.0 + sq * (1.0 - k) * wx * wy + rg * rd * wx * wy,
                        0.70, 1.65)
        map_x = (float(geom["cx"]) + (xs - float(geom["cx"])) * scale).astype(np.float32)
        map_x = np.broadcast_to(map_x, map_y.shape)
    else:
        map_x = np.broadcast_to(np.arange(W, dtype=np.float32)[None, :], map_y.shape)
    out = cv2.remap(img, map_x, map_y.astype(np.float32),
                    interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
    # 口腔覆盖：几何只把开口压扁，牙齿/舌头/暗腔仍在（压到极限牙齿占比反升），
    # 所以低开合度时还要用唇色把口腔内容盖掉。
    try:
        cv_on = float(cover) if cover is not None else COVER_ON
    except (TypeError, ValueError):
        cv_on = COVER_ON
    if cv_on > 0.01 and k < cv_on:
        amt = float(np.clip((cv_on - k) / max(1e-3, cv_on - COVER_FULL), 0.0, 1.0))
        if amt > 0.01:
            out = _cover_cavity(out, img, geom, amt,
                                seam_depth=seam_depth, seam_curve=seam_curve,
                                lip_tint=lip_tint, feather=cover_feather)
    return out


def _lip_colors(img, geom):
    """取上唇 / 下唇的代表色。

    纯色块卡通里嘴唇是平涂色，取「偏亮的那 45%」能避开唇缝阴影和描边，
    拿到的是干净的唇色；取中位数会被阴影拉暗，填进嘴里会发灰。
    """
    H, W = img.shape[:2]
    cx = int(round(float(geom["cx"])))
    half = max(3, int(float(geom["mw"]) * 0.28))

    def _dom(y0, y1):
        y0 = max(0, min(H - 1, int(y0)))
        y1 = max(y0 + 1, min(H, int(y1)))
        xa = max(0, cx - half)
        xb = min(W, cx + half)
        p = img[y0:y1, xa:xb].reshape(-1, 3).astype(np.float32)
        if not len(p):
            return np.array([200.0, 180.0, 180.0], np.float32)
        s = p.sum(1)
        return p[np.argsort(s)[int(len(s) * 0.55):]].mean(0)

    up = _dom(float(geom["y_split"]) - 4, float(geom["y_split"]))
    dn = _dom(float(geom["y_hole_bot"]), float(geom["y_hole_bot"]) + 4)
    return up, dn


def _lip_palette(img, geom):
    """从原图采样**真实唇部四层色**：上唇 / 唇线 / 下唇 / 下巴皮肤。

    ## 为什么必须采样，不能只用皮肤色

    旧版闭嘴覆盖用 `_skin_colors`（皮肤色 L≈203）单色平涂 + 一道压暗 30%
    的淡线。实测这张 3D 卡通：真实下唇是 RGB(246,127,126)、**L≈158**，
    而皮肤是 L≈203 —— 相差 45 个亮度级。用皮肤色盖住嘴唇，等于把嘴涂成
    了脸颊，所以视觉复核判「像一块平涂色块，没有立体感」。

    真实嘴唇的明暗结构是**四段**的：
        上唇（亮，L≈214）→ 唇线（暗，L≈122）→ 下唇（中，L≈158）→ 皮肤（亮，L≈203）
    把这四段照原图采出来，覆盖后才有真正的体积感（实测中心列极差
    76 → 96，且出现清晰唇缝谷）。

    采样用**中位数**而非「偏亮的 55%」：这里要的是真实层次，不是单一干净色。
    """
    H, W = img.shape[:2]
    cx = int(round(float(geom["cx"])))
    half = max(3, int(float(geom["mw"]) * 0.25))
    ys = float(geom["y_split"])
    yl = float(geom.get("y_lip_bot") or geom["y_hole_bot"])

    def _med(y0, y1):
        y0 = max(0, min(H - 1, int(round(y0))))
        y1 = max(y0 + 1, min(H, int(round(y1))))
        xa = max(0, cx - half)
        xb = min(W, cx + half)
        p = img[y0:y1, xa:xb].reshape(-1, 3).astype(np.float32)
        if not len(p):
            return np.array([190.0, 180.0, 240.0], np.float32)  # 浅粉兜底(BGR)
        return np.median(p, axis=0)

    # ⚠ 2026-11-15：上唇采样要**避开** y_split-1 那条唇线（实测 L=109），
    #   原来取到 ys-6 正好把它算进中位数，上唇色被拉暗到 L=192（真值 215）。
    up = _med(ys - 14, ys - 7)      # 上唇（亮粉白）
    # ⚠ 2026-11-15 修「闭不上、像涂抹」：唇线原来取 ys-3~ys-1，
    #   实测那里是**上唇体**（L=234，比上唇 210 还亮），真正的唇缝在
    #   ys~ys+3（L=20，深黑）。取错位置 → 覆盖后整片是亮色、唇缝消失，
    #   视觉上就是「一块平涂色」。改取真实唇缝带。
    seam = _med(ys + 1, ys + 3)     # 唇缝（最暗）
    dn = _med(yl - 9, yl - 1)       # 下唇（粉红）
    sk = _med(yl + 3, yl + 8)       # 下巴皮肤
    return up, seam, dn, sk


def _skin_colors(img, geom):
    """取嘴**下方**的皮肤色（闭嘴填充的基色）。

    为什么不用 `_lip_colors`：它取「上唇色」的区间（y_split-4..y_split）在这张
    3D 卡通上落在**牙齿**上（实测 RGB 252,203,192），盖出来是亮白补丁。
    嘴下方 12~26px 是纯肤色带，取偏亮的 55% 避开唇下阴影，得到干净基色。
    """
    H, W = img.shape[:2]
    cx = int(round(float(geom["cx"])))
    half = max(3, int(float(geom["mw"]) * 0.25))
    y0 = int(float(geom["y_hole_bot"]) + 14)
    y1 = int(float(geom["y_hole_bot"]) + 26)
    y0 = max(0, min(H - 1, y0)); y1 = max(y0 + 1, min(H, y1))
    xa = max(0, cx - half); xb = min(W, cx + half)
    p = img[y0:y1, xa:xb].reshape(-1, 3).astype(np.float32)
    if not len(p):
        return np.array([200.0, 180.0, 180.0], np.float32)
    s = p.sum(1)
    return p[np.argsort(s)[int(len(s) * 0.55):]].mean(0)


def _cover_cavity(out, src, geom, cover, seam_depth=1.00, seam_curve=0.45,
                  lip_tint=0.85, feather=0.30):
    """把压缩后仍然可见的口腔内容（牙齿 / 舌头 / 暗腔）用唇色盖掉。

    ## 为什么只做几何永远闭不上

    纵向压缩只是把开口「压扁」，牙齿和舌头仍然留在里面。实测这张卡通：
    压到 k=0（极限）时，开口区的暗像素占比从 0.71 反而升到 0.42、
    牙齿占比 0.22 → 0.40 —— 压扁的牙齿变成了横向条纹，面积更大。
    所以「闭嘴」必须**同时**做颜色替换：几何负责唇形靠近，颜色负责
    把口腔内容盖住。加上这一层之后，k=0.35 时暗占比降到 0.016、
    k=0.15 时降到 0.000，才是真正的闭合。

    ## 为什么从「单色平涂」改成「四层立体」

    旧版用皮肤色单色平涂 + 一道压暗 30% 的淡线。视觉复核判「像一块平涂
    色块，没有立体感，缺少高光和阴影」。根因是**把嘴唇涂成了皮肤**：
    实测原图真实下唇 L≈158，皮肤 L≈203，差 45 个亮度级。

    现在按原图真实结构铺四层（亮度依次 214 → 122 → 158 → 203）：
        上唇（亮）→ 唇线（暗）→ 下唇（中）→ 下巴皮肤（亮）
    中心列灰度极差从 76 提到 96，并出现清晰的唇缝谷。

    seam_depth / seam_curve / lip_tint / feather 由 LLM 按画风给。
    """
    H, W = out.shape[:2]
    cx = float(geom["cx"])
    mw = float(geom["mw"])
    # ⚠ 2026-11-15 修「上唇被涂改」：上界原来取 y_split-5，把**完好的上唇**
    #   （839~843）也纳入覆盖区 —— 上唇本来没有口腔内容要盖，涂它等于把好的
    #   唇色换成采样色，实测上唇 840~843 的均值被从原图 217~228 拉到 141~201。
    #   上界收到 y_split-1，覆盖只从唇缝开始。
    y0 = float(geom["y_split"]) - 1.0
    # 下界必须到 y_lip_bot（含舌头段）。原来只到 y_hole_bot，实测开口真实范围
    # 是 840~865（牙齿+口腔+舌头），852 以下没盖住 → 视觉上仍「露牙露舌」。
    y1 = float(geom.get("y_lip_bot") or geom["y_hole_bot"]) + 3.0
    if y1 <= y0:
        return out
    y_mid = (y0 + y1) * 0.5
    ry = max(1.5, (y1 - y0) * 0.5)

    # 四层真实唇色（实测这张卡通：上唇 L214 / 唇线 L122 / 下唇 L158 / 皮肤 L203）
    up, seam_c, dn, sk = _lip_palette(src, geom)
    # lip_tint：下唇饱和度。0=退化成皮肤色（嘴唇看不出来），1=用原图下唇色
    lt = float(np.clip(lip_tint, 0.0, 1.0))
    dn = sk * (1.0 - lt) + dn * lt
    # seam_depth：唇线深浅。1=用原图唇线色，0=不画唇缝
    sd = float(np.clip(seam_depth, 0.0, 1.0))
    seam_c = sk * (1.0 - sd) + seam_c * sd

    X = np.arange(W, dtype=np.float32)[None, :]
    Y = np.arange(H, dtype=np.float32)[:, None]
    # ⚠ 2026-11-15 修「上唇/下唇边缘没被覆盖到」：椭圆蒙版在 y=841（上唇体）
    #   横向只剩 ±21px（实测 42px 宽），而真实嘴宽 64px —— 上唇有 1/3 露在
    #   覆盖之外，剖面显示上唇区均值 128 ≈ 原图 127，等于根本没盖上。
    #   换成 n=4 的超椭圆（近似矩形带圆角），同一行覆盖 70px，贴合唇形。
    # 蒙版用「矩形 + 横向圆角窗」，不用椭圆：
    #   椭圆在 y=841（上唇下缘）横向只剩 ±21px，而嘴宽 64px，盖不住；
    #   超椭圆在**上下边缘覆盖为 0**，唇缝那一行反而没盖住。
    #   嘴唇本质是「横向拉长的矩形 + 圆嘴角」——矩形保满覆盖，横向窗做圆角。
    m = np.zeros((H, W), np.float32)
    xa = int(round(cx - mw * 0.60))
    xb = int(round(cx + mw * 0.60))
    m[int(y0):int(y1), max(0, xa):min(W, xb)] = 1.0
    xwin = np.clip(1.0 - (np.abs(X - cx) / max(1.0, mw * 0.72)) ** 6, 0.0, 1.0)
    m = m * xwin
    fw = float(np.clip(feather, 0.05, 1.0))
    # ⚠ 2026-11-15 修「嘴周围一圈颜色都在变」：羽化半径原来直接取 ry*feather
    #   （ry=22.5、feather=0.30 → sigma=6.8），高斯外溢约 20px，覆盖从 839~884
    #   扩散到 **829~898**。封顶 2.4px 后既能软化边缘，又不会外溢到上唇和下巴。
    m = cv2.GaussianBlur(m, (0, 0), float(np.clip(ry * fw * 0.55, 0.9, 2.4)))
    u = np.clip((X - cx) / max(1.0, mw * 0.5), -1.0, 1.0)
    # ⚠ 2026-11-15 修「覆盖后没有唇形、像一块涂抹」：唇缝位置原来从**椭圆中心**
    #   推算（y_mid + sc*ry*(u²-0.5)），实测落在 y=856.4，而真实唇缝在 y=844 ——
    #   偏 12.4px。结果唇缝被画到口腔中部，上唇整片变成亮色、看不出唇形。
    #   改成**锚定解剖位置**：唇缝固定对齐 y_split，下唇下沿对齐 y_lip_bot。
    ys_f = float(geom["y_split"])
    yl = float(geom.get("y_lip_bot") or geom["y_hole_bot"])
    sc = float(np.clip(seam_curve, 0.0, 1.0))
    # 唇缝中心：真实唇缝占 y_split~y_split+3（实测 844~846），中心取 +1.5。
    # ⚠ 2026-11-15 修「唇缝不够暗」：原来基准直接落在 ys_f 再叠加 (u²-0.5) 的
    #   -0.5625 偏移，实际中心在 ys_f-0.56 —— y=845 处 w_seam 只剩 0.725，
    #   混进 27% 下唇色，唇缝 L 只有 65（原图 17），视觉上「几乎看不到唇缝线」。
    # 微笑弧：嘴角相对中心上翘，幅度限制在 2.5px 内，避免把唇缝拉弯成弧线
    seam_y = ys_f + 1.5 + sc * 2.5 * (u * u - 0.5)
    d = Y - seam_y
    # 唇缝带宽：真实唇缝 2~3px，按嘴高自适应但封顶
    sw = float(np.clip((yl - ys_f) * 0.12, 1.6, 4.5))

    # 四段权重：上唇(上) / 唇缝(中，最暗) / 下唇(下) / 皮肤(最下)
    # 唇缝权重用**平顶窗**而不是高斯：高斯只有中心那一行够暗（实测 y=845
    # L=21 达标，但 844 是 50、846 是 65），而真实唇缝 844~846 三行都是 19~23。
    # 平顶 1.8px + 外侧线性衰减，才能画出一条 3~4px 的实心唇线。
    _ad = np.abs(d)
    w_seam = np.clip(1.0 - np.maximum(0.0, _ad - 1.8) / max(0.9, sw * 0.40),
                     0.0, 1.0)
    # 上唇体在椭圆里只有 ~5px（y_split-5 ~ 唇缝），过渡带必须比它短，
    # 否则整段上唇只混到 23~71% 的唇色，看起来发暗。
    w_up = np.clip(-d / max(1.0, sw * 0.75), 0.0, 1.0)
    w_dn = np.clip(d / max(1.0, sw * 1.4), 0.0, 1.0)
    # 下唇下沿 → 皮肤：从唇底（yl）开始过渡，避免把下唇整片涂成皮肤色
    w_sk = np.clip((d - (yl - ys_f)) / max(1.0, sw * 1.5), 0.0, 1.0)

    # 下唇纵向渐变：直接采原图下唇的**逐行**中位色。
    # ⚠ 2026-11-15 修「下唇是一整条平涂」：原来整段下唇用同一个中位色
    #   （实测 L=191），剖面 y848~880 全是 191 —— 一条死平线，这就是
    #   「像贴纸 / 像涂抹」的来源。原图下唇是 211→183 的真实渐变
    #   （高光→阴影），照它铺开才有体积感。
    # ⚠ 2026-11-15：剖面两端要**避开**口腔底（y_hole_bot 那行 L=172，偏暗）
    #   和下唇描边（y_lip_bot 那行 L=52，极暗）。含进去会把渐变压平
    #   （实测下唇 std 只有 4.6，而原图下唇本身有 10+ 的层次）。
    lip_top = float(geom["y_hole_bot"]) + 1.0
    lip_bot = yl - 1.0
    nrow = int(max(2, round(lip_bot - lip_top)))
    xa = max(0, int(round(cx - mw * 0.25)))
    xb = min(W, max(xa + 1, int(round(cx + mw * 0.25))))
    prof = np.zeros((nrow, 3), np.float32)
    for i in range(nrow):
        yy = int(np.clip(round(lip_top + i), 0, H - 1))
        rowp = src[yy, xa:xb].reshape(-1, 3).astype(np.float32)
        prof[i] = np.median(rowp, 0) if len(rowp) else dn
    # 按「离唇缝的归一化距离」查表，让填充区自带原图的明暗走向
    tt = np.clip((Y - seam_y) / max(1.0, lip_bot - ys_f), 0.0, 1.0)
    ii = np.clip((tt * (nrow - 1)).astype(np.int32), 0, nrow - 1)
    dn_map = prof[ii]                      # (H,1,3)
    # lip_tint 同样作用到渐变上，保持「下唇饱和度」这一个旋钮的语义
    dn_map = sk[None, None, :] * (1.0 - lt) + dn_map * lt

    fill = np.broadcast_to(dn_map, (H, W, 3)).astype(np.float32).copy()
    fill = fill * (1.0 - w_up[..., None]) + \
        np.broadcast_to(up[None, None, :], (H, W, 3)) * w_up[..., None]
    fill = fill * (1.0 - w_seam[..., None]) + \
        np.broadcast_to(seam_c[None, None, :], (H, W, 3)) * w_seam[..., None]
    fill = fill * (1.0 - w_sk[..., None]) + \
        np.broadcast_to(sk[None, None, :], (H, W, 3)) * w_sk[..., None]

    a = (m * float(np.clip(cover, 0.0, 1.0)))[..., None]
    return np.clip(out.astype(np.float32) * (1.0 - a) + fill * a,
                   0, 255).astype(np.uint8)


def warp_sequence(img, geom, k_seq):
    """批量形变。k_seq 为 0..1 序列（越大越张）。"""
    return [warp(img, geom, k) for k in k_seq]


def open_from_closed(img, geom, k):
    """源图是闭嘴时，向「张开」方向做外推。

    做法：把下唇区域**下移**，中间露出的带用原图口腔色（唇内侧最暗处的
    中位色）填充，并加一道由深到浅的纵向渐变模拟口腔深度。

    这条路径天生不如「张嘴源图 + 压缩」自然（没有真实的牙齿/舌头纹理），
    所以只在源图闭嘴时使用；上层会据此给出版本标注。
    """
    H, W = img.shape[:2]
    k = float(np.clip(k, 0.0, 1.0))
    if k <= 0.02:
        return img.copy()
    y_split = geom["y_split"]; y_lip = geom["y_lip_bot"]
    cx = geom["cx"]; mw = geom["mw"]
    max_open = max(3.0, geom["mh"] * 0.42)
    shift = k * max_open

    # 1) 下唇及以下整体下移
    map_y = _displace_map(H, W, geom, -shift)
    out = cv2.remap(img, np.broadcast_to(np.arange(W, dtype=np.float32)[None, :], map_y.shape),
                    map_y, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)

    # 2) 露出的带：填口腔色 + 纵向深度渐变
    y0 = int(y_split); y1 = int(min(H - 1, y_split + shift))
    if y1 <= y0:
        return out
    xa = int(max(0, cx - mw * 0.42)); xb = int(min(W, cx + mw * 0.42))
    patch = img[max(0, y_split - 10):y_split, xa:xb].reshape(-1, 3)
    if patch.size:
        idx = np.argsort(patch.sum(1))[:max(1, len(patch) // 12)]
        cav = patch[idx].mean(0)
    else:
        cav = np.array([18.0, 8.0, 12.0], np.float32)
    hh = y1 - y0
    ramp = np.linspace(0.55, 1.0, max(1, hh))[:, None, None]     # 越深越暗
    out[y0:y1, xa:xb] = np.clip(cav[None, None, :] * (1.0 - ramp) + cav[None, None, :] * ramp * 0.35,
                                0, 255).astype(np.uint8)
    # 3) 上下缘柔化，避免硬切
    soft = cv2.GaussianBlur(out[y0:y1, xa:xb].astype(np.float32), (0, 0), 1.2)
    out[y0:y1, xa:xb] = np.clip(soft, 0, 255).astype(np.uint8)
    return out


def close_by_mirror(img, geom, seam=0.0):
    """用**下唇镜像**填掉口腔开口：只替换开口那几行，其余像素一个都不动。

    与 close_by_lip 的根本区别：
      · close_by_lip 是**位移映射** —— 把下唇整体搬到开口处，原下唇位置由
        下巴顶上来，所以下巴/脖子会被整体拉伸；
      · 本函数是**逐行替换** —— 开口区每一行取「下唇对应行的镜像」，开口
        以下（下唇、下巴、衣服）保持原样，实测开口以下零改动。

    为什么镜像能自然：这个卡通的上唇极薄（只有 2px 唇线），开口正下方就是
    20px 厚的真实下唇。镜像取源时，上半段自然取到下巴的亮部、下半段取到
    下唇的暗部，恰好形成「上唇（亮）— 唇缝— 下唇（暗）」的层次，而且纹理、
    高光、明暗全部来自原图，画风天然一致。

    实测（同一张 3D 卡通，6 张候选并排交给 DeepSeek 复核）：
      本函数 vs 现有 lip_shift —— 复核原话是 lip_shift「中间有细小白色线框/
      硬边，有涂抹痕迹」，本函数「唇线弧度自然、边缘干净、无涂抹色块硬边
      重影条纹、画风一致」，被判为最自然的一张。
    """
    H, W = img.shape[:2]
    ys = int(round(float(geom["y_split"])))
    cx = float(geom["cx"])
    cy = float(geom.get("cy") or geom["y_split"])
    mw = float(geom["mw"])
    mh = float(geom.get("mh") or 0.0)
    if mh <= 1.0:                       # geom 没带 mh 时从宽度估
        mh = max(6.0, mw * 0.46)

    # ★ 定位开口底与下唇底：不能直接用 analyze 的 y_hole_bot / y_lip_bot。
    #   它靠「暗像素占比」找口腔，而这张卡通张嘴露齿——牙齿（灰度 237）和舌头
    #   （灰度 135~155）都不算暗，于是它把开口判成 12px（真实 23px），
    #   下唇底判成 861（真实 881）。照它填会漏掉下半截舌头。
    #   改用两个解剖比例做初值，再用剖面「中暗→亮」的最大正跳变校正开口底：
    #     开口底 ≈ cy + 0.51*mh   下唇底 ≈ cy + 1.17*mh
    g = img.astype(np.float32) @ np.array([0.114, 0.587, 0.299], dtype=np.float32)
    col = int(round(cx))
    x0, x1 = max(0, col - 2), min(W, col + 3)
    prof_all = g[:, x0:x1].mean(axis=1)

    yh = int(round(cy + 0.51 * mh))
    lo = max(ys + 2, int(round(cy + 0.20 * mh)))
    hi = min(H - 1, int(round(cy + 0.95 * mh)))
    best, best_d = None, 0.0
    for y in range(lo, hi):
        d = float(prof_all[y + 1] - prof_all[y])
        if d > best_d:
            best_d, best = d, y
    if best is not None and best_d > 40:
        yh = best                       # 跳变前那一行 = 开口最后一行

    yl = int(round(cy + 1.17 * mh))
    lo2 = max(yh + 2, int(round(cy + 0.85 * mh)))
    hi2 = min(H - 1, int(round(cy + 1.60 * mh)))
    for y in range(lo2, hi2):           # 下唇下沿 = 开口以下第一处暗线
        if prof_all[y] < 120:
            yl = y
            break

    ys = max(0, min(H - 2, ys))
    yh = max(ys + 1, min(H - 2, yh))
    yl = max(yh + 1, min(H - 1, yl))

    out = img.astype(np.float32).copy()
    src = img.astype(np.float32)
    xs = np.arange(W, dtype=np.float32)
    # 横向 cos 窗：嘴中心全权重，到 1.45*mw 处降为 0，避免把脸轮廓一起改
    t = np.clip((np.abs(xs - cx) - mw * 1.05) / max(1e-6, mw * 0.40), 0, 1)
    hw = (0.5 + 0.5 * np.cos(t * np.pi))[:, None]

    for y in range(ys + 1, yh + 1):
        sy = 2 * yh + 1 - y              # y=yh -> yh+1（下唇第一行）
        sy = min(max(sy, 0), H - 1)
        out[y] = out[y] * (1 - hw) + src[sy] * hw

    # 可选唇缝暗线（seam>0 才画）：默认 0，实测加了反而「缝合感」变重
    if seam > 0:
        y0 = ys + 1
        xsn = (xs - cx) / max(1.0, mw * 1.05)
        prof_w = np.clip(1 - xsn ** 2, 0, 1)[:, None]
        k = float(seam) * prof_w * hw
        for dy in range(2):
            y = y0 + dy
            if y > yh:
                break
            out[y] = out[y] * (1 - k * (1.0 - dy * 0.35))

    return np.clip(out, 0, 255).astype(np.uint8)


def close_by_lip(img, geom, ratio=0.85, sharpen=0.9):
    """生成闭嘴基准：把**下唇整体上移**覆盖口腔，再补一道唇缝。

    为什么不用前两种做法：
      · inpaint 平涂 → 视觉复核判「一块平贴的肤色补丁，没有上下唇结构」；
      · 上唇镜像填充 → 判「矩形切割线、上下唇色差明显、带入背景色块」。
    下唇上移用的是**原图下唇的真实纹理**（唇纹、高光、明暗都在），
    所以画风天然一致；ratio 取 0.85 时视觉复核判「最自然，保留唇峰和圆润感」
    （1.0 会过度拉伸、唇峰被拉平，0.7 又闭不严）。

    残留的唇缝暗色用上唇下缘的颜色压一道柔和暗线盖掉。
    """
    H, W = img.shape[:2]
    ys = float(geom["y_split"]); yh = float(geom["y_hole_bot"])
    yl = float(geom["y_lip_bot"]); yfade = float(geom.get("y_fade", yl + 40))
    cx = float(geom["cx"]); mw = float(geom["mw"])
    lip_h = max(1.0, yl - yh)
    # ★ ratio 上限不能是 1.0：本图开口 22px、下唇只有 19px，ratio=1.0 时
    #   move 封顶 19px，永远差 3px —— 这就是「闭不严、缝里露白」的代码根因。
    #   ratio > 1 的物理含义是「下唇连同下巴上沿一起上移」，映射会取到
    #   y_lip_bot 之后的内容，所以必须保证 y_fade 还在下巴范围内（已保证）。
    move = lip_h * float(np.clip(ratio, 0.0, 1.6))

    ys_ = np.arange(H, dtype=np.float32)[:, None]
    xs_ = np.arange(W, dtype=np.float32)[None, :]
    map_y = ys_.copy()
    a = ys_ < ys
    b = (ys_ >= ys) & (ys_ < ys + move)
    c = ys_ >= ys + move
    # B 段：目标 [ys, ys+move] ← 源 [yh, yh+move]（下唇搬上来）
    map_y = np.where(b, yh + (ys_ - ys), map_y)
    # C 段：源 = yl + (y - (ys+move))，保证 y=ys+move 处与 B 段接上
    map_y = np.where(c, yl + (ys_ - (ys + move)), map_y)
    # 下方 ramp 衰减，避免下巴/脖子被整体拖动
    ramp = np.clip((yfade - ys_) / max(1.0, (yfade - yl)), 0.0, 1.0)
    map_y = np.where(c, ys_ + (map_y - ys_) * ramp, map_y)
    map_y = np.where(a, ys_, map_y)
    # 横向 cos 窗：只在嘴部附近生效
    wx = np.clip(1.0 - (np.abs(xs_ - cx) / max(1.0, mw * 1.12)) ** 2, 0.0, 1.0)
    map_y = ys_ + (map_y - ys_) * wx
    mx = np.broadcast_to(xs_, map_y.shape)
    out = cv2.remap(img, mx.astype(np.float32), map_y.astype(np.float32),
                    interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)

    # 唇缝补色：下唇搬上来后，唇缝处仍会残留原口腔的暗色（视觉复核
    # 「有一条深色甚至黑色的空洞残留」）。用上唇下缘的唇色压一道柔和暗线，
    # 既盖掉残留，又给出「闭上的嘴」该有的那条缝。
    band = max(2, int(lip_h * 0.18))
    y0 = int(max(0, ys - band)); y1 = int(min(H, ys + move * 0.55 + band))
    xa = int(max(0, cx - mw * 0.46)); xb = int(min(W, cx + mw * 0.46))
    if y1 > y0 and xb > xa:
        ref = img[int(max(0, ys - band * 2)):int(max(1, ys)), xa:xb].reshape(-1, 3)
        if ref.size:
            idx = np.argsort(ref.sum(1))[:max(1, len(ref) // 6)]
            lip_col = ref[idx].mean(0)
            seam = np.zeros((H, W), np.float32)
            cv2.ellipse(seam, (int(cx), int(ys + move * 0.28)),
                        (int(mw * 0.46), max(1, int(lip_h * 0.16))),
                        0, 0, 360, 1.0, -1)
            seam = cv2.GaussianBlur(seam, (0, 0), max(0.8, lip_h * 0.06))[..., None]
            out = np.clip(out.astype(np.float32) * (1 - seam * 0.75)
                          + lip_col[None, None, :] * (seam * 0.75), 0, 255).astype(np.uint8)

    # 位移映射必然丢高频：实测唇部区拉普拉斯方差只有 2224（原图 3738），
    # 视觉判「唇缝模糊、涂抹感」。轻锐化能拉回 4822，且不产生横向撕裂
    # （对比 detail_restore 把原图高频硬叠回去，被判「撕裂感、边缘发毛」）。
    if sharpen > 0:
        _blur = cv2.GaussianBlur(out, (0, 0), 1.1)
        _sharp = cv2.addWeighted(out, 1.0 + float(sharpen),
                                 _blur, -float(sharpen), 0)
        # ⚠ 必须只作用在嘴部：全局 addWeighted 会把整张图都锐化
        # （实测改动行从 53 涨到 213、衣服区差异 0 → 0.71、视觉评分 6.5 → 4）
        _m = mouth_mask(img, geom, feather=max(2.0, float(geom["mw"]) * 0.06))[..., None]
        out = np.clip(out.astype(np.float32) * (1 - _m)
                      + _sharp.astype(np.float32) * _m, 0, 255).astype(np.uint8)
    return out


def mouth_mask(img, geom, feather=2.0):
    """嘴部解剖蒙版（唇 + 口腔），用于「只替换嘴、皮肤不动」。"""
    H, W = img.shape[:2]
    cx = geom["cx"]; mw = geom["mw"]; mh = geom["mh"]
    cy = (geom["y_split"] + geom["y_lip_bot"]) / 2.0
    half_h = max(3.0, (geom["y_lip_bot"] - geom["y_split"]) * 0.72)
    m = np.zeros((H, W), np.float32)
    cv2.ellipse(m, (int(cx), int(cy)), (int(mw * 0.52), int(half_h)), 0, 0, 360, 1.0, -1)
    if feather > 0:
        m = cv2.GaussianBlur(m, (0, 0), feather)
    return np.clip(m, 0, 1)


def save_geom(geom, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(geom, f, ensure_ascii=False, indent=1)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def load_geom(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                   # noqa: BLE001
        return None
