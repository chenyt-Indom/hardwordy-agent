"""数字人表情层：心情定义、语境自动适配、面部微表情。

设计原则（用户 2026-11-14 明确要求）：
  表情「不要太呆板，也不要太夸张」—— 所有幅度都压在肉眼刚好能察觉的量级：
    · 头部微动：旋转 <= 0.55 度，位移 <= 1.6 px，预放大 0.8% 防露边
    · 眉眼形变：纵向位移 <= 2 px，只作用于眉眼带，高斯羽化过渡
    · 语速只影响**频率**（说得快头动得稍快），不影响幅度，避免快语速甩头

能力边界（必须如实告知用户，不许吹）：
  这是**形变**，不是重绘。单张静态图做不出真实的眉毛抬起、眨眼、嘴角上扬
  ——那需要 LivePortrait / EMO 这类生成式模型，本机没有。
  所以这里只做「整头微动 + 眉眼带轻微纵向位移」，作用是**消掉呆板的静帧感**，
  不是真的做表情表演。

心情 -> 三处生效：
  1. TTS：style（情感预设）+ rate 偏移 + pitch 偏移
  2. 表情：头部微动幅度/频率、眉眼形变幅度
  3. 语速：由 rate 决定，同时反过来调制微动频率
"""

import math
import re

import numpy as np

# ---------------------------------------------------------------- 心情定义
# style 取值必须是 tts_speak 支持的：neutral/cheerful/gentle/serious/sad/news/excited
# 幅度全部是「保守档」，上限见模块 docstring
MOODS = {
    "happy": {
        "label": "开心", "emoji": "😊", "style": "cheerful",
        "rate": 8, "pitch": 10,
        "sway": 2.4, "nod": 2.2, "rot": 0.62, "brow": 2.0, "freq": 0.42,
        "desc": "语气轻快上扬，头部小幅摆动",
    },
    "calm": {
        "label": "平静", "emoji": "😌", "style": "neutral",
        "rate": 0, "pitch": 0,
        "sway": 1.6, "nod": 1.5, "rot": 0.40, "brow": 1.2, "freq": 0.30,
        "desc": "自然中性，最接近真人静态说话",
    },
    "serious": {
        "label": "认真", "emoji": "🧐", "style": "serious",
        "rate": -4, "pitch": -4,
        "sway": 1.2, "nod": 1.7, "rot": 0.32, "brow": 1.6, "freq": 0.26,
        "desc": "语速略缓、音调压低，头部动作克制",
    },
    "gentle": {
        "label": "温柔", "emoji": "🤍", "style": "gentle",
        "rate": -10, "pitch": 4,
        "sway": 1.9, "nod": 1.5, "rot": 0.46, "brow": 1.4, "freq": 0.28,
        "desc": "语速放慢、声音放软，头部轻晃",
    },
    "sad": {
        "label": "悲伤", "emoji": "😔", "style": "sad",
        "rate": -14, "pitch": -8,
        "sway": 1.0, "nod": 1.9, "rot": 0.26, "brow": 2.2, "freq": 0.20,
        "desc": "语速慢、音调低，头部轻微下垂感",
    },
    "excited": {
        "label": "兴奋", "emoji": "🤩", "style": "excited",
        "rate": 14, "pitch": 14,
        "sway": 3.0, "nod": 2.8, "rot": 0.78, "brow": 2.6, "freq": 0.55,
        "desc": "语速快、音调高，头部动作明显（仍在上限内）",
    },
    "surprise": {
        "label": "惊讶", "emoji": "😮", "style": "cheerful",
        "rate": 6, "pitch": 12,
        "sway": 2.3, "nod": 2.6, "rot": 0.58, "brow": 2.6, "freq": 0.48,
        "desc": "音调抬高、顿挫感强，眉眼带拉伸最大",
    },
    "worried": {
        "label": "担忧", "emoji": "😟", "style": "sad",
        "rate": -6, "pitch": -2,
        "sway": 0.7, "nod": 1.1, "rot": 0.25, "brow": 1.7, "freq": 0.24,
        "desc": "语速偏慢，眉眼带向下压",
    },
    "confident": {
        "label": "自信", "emoji": "😎", "style": "news",
        "rate": 4, "pitch": -2,
        "sway": 0.9, "nod": 1.0, "rot": 0.30, "brow": 1.2, "freq": 0.34,
        "desc": "吐字清楚、节奏稳，头部小幅点头",
    },
}

# 自动适配用：关键词 -> 心情（分值累加，取最高）
_KEYWORDS = {
    "happy": ["太好了", "好棒", "真棒", "喜欢", "开心", "高兴", "恭喜", "哈哈",
              "成功", "顺利", "美好", "幸福", "赞", "优秀", "惊喜", "感谢"],
    "sad": ["遗憾", "可惜", "难过", "失去", "失败", "悲伤", "痛苦", "离开",
            "告别", "不幸", "沉重", "哀悼", "伤心", "无奈"],
    "excited": ["重大", "突破", "历史性", "终于", "首个", "第一", "创纪录",
                "爆发", "震撼", "沸腾", "狂热", "激动", "冲刺", "夺冠"],
    "serious": ["因此", "必须", "关键", "注意", "重要", "原则", "规定",
                "数据", "分析", "报告", "结论", "风险提示", "要求", "务必"],
    "gentle": ["亲爱的", "谢谢", "希望", "陪伴", "温暖", "轻轻", "慢慢",
               "亲爱的你", "祝福", "愿你", "安心", "照顾"],
    "surprise": ["竟然", "没想到", "居然", "意外", "出乎意料", "万万没想到",
                 "惊奇", "不可思议", "反转"],
    "worried": ["风险", "危险", "担心", "警告", "问题", "危机", "隐患",
                "警惕", "困难", "压力", "挑战", "下跌", "亏损"],
    "confident": ["一定", "肯定", "相信", "我们能做到", "确定", "保证",
                  "毫无疑问", "必将", "领先", "优势"],
}


def list_moods() -> list:
    """给前端下拉用的清单（保持声明顺序）。"""
    out = []
    for k, v in MOODS.items():
        out.append({"key": k, "label": v["label"], "emoji": v["emoji"],
                    "style": v["style"], "desc": v["desc"]})
    return out


def get_mood(key: str) -> dict:
    """取心情配置，未知 key 退回 calm。"""
    return dict(MOODS.get((key or "").strip().lower(), MOODS["calm"]))


# ---------------------------------------------------------------- 自动适配
# 句长只按**真正的句末标点**切：逗号是分句不是句。
# （2026-11-14 实测：按逗号切时中文段落平均只有 8 字，导致几乎每段都被判
#  「短句为主、语速偏快」，语速整体被推高。）
_PUNCT = re.compile(r"[。！？；!?;\n]+")


def detect_mood(text: str) -> dict:
    """纯规则判断语境 -> (心情, 建议语速)。秒出、免费、可解释。

    打分来源（每条都记进 why，方便前端显示「为什么判成这个」）：
      · 关键词命中（权重 1.0，长词优先）
      · 感叹号密度 -> excited / happy
      · 问号 -> surprise
      · 省略号 -> gentle / worried
      · 平均句长 -> 短句偏快、长句偏慢

    拿不准（最高分 < 2 或 第一名与第二名差 < 1）时返回 confident=False，
    调用方可以决定是否改用大模型复核。
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "文本是空的"}

    scores = {}
    why = []
    low = t.lower()
    for mood, words in _KEYWORDS.items():
        s = 0.0
        hit = []
        for w in words:
            c = low.count(w.lower())
            if c:
                # 长词更可信：4 字以上给 1.4 倍权重
                s += c * (1.4 if len(w) >= 4 else 1.0)
                hit.append("%s×%d" % (w, c))
        if s > 0:
            scores[mood] = scores.get(mood, 0.0) + s
            why.append("%s：%s" % (MOODS[mood]["label"], "、".join(hit[:3])))

    n = max(1, len(t))
    bang = t.count("！") + t.count("!")
    ques = t.count("？") + t.count("?")
    ell = t.count("…") + t.count("...")

    if bang / n > 0.02 and bang >= 2:
        scores["excited"] = scores.get("excited", 0) + min(2.0, bang * 0.5)
        why.append("感叹号 %d 个，语气强烈" % bang)
    if ques / n > 0.015 and ques >= 2:
        scores["surprise"] = scores.get("surprise", 0) + min(1.5, ques * 0.4)
        why.append("问号 %d 个，带疑问/惊讶" % ques)
    if ell >= 2:
        scores["gentle"] = scores.get("gentle", 0) + 0.8
        why.append("省略号 %d 处，语气放缓" % ell)

    # 平均句长 -> 语速微调（只影响 rate，不参与心情打分）
    parts = [p for p in _PUNCT.split(t) if p.strip()]
    avg_len = (sum(len(p) for p in parts) / len(parts)) if parts else len(t)
    if avg_len <= 15:
        rate_adj, len_note = 5, "短句为主（平均 %.0f 字），语速略快" % avg_len
    elif avg_len >= 50:
        rate_adj, len_note = -5, "长句为主（平均 %.0f 字），语速放慢" % avg_len
    else:
        rate_adj, len_note = 0, "句长适中（平均 %.0f 字）" % avg_len

    if not scores:
        return {"ok": True, "mood": "calm", "label": MOODS["calm"]["label"],
                "score": 0.0, "confident": False, "rate": rate_adj,
                "why": ["没有明显情绪词，按平静处理", len_note],
                "avg_len": round(avg_len, 1), "runner_up": ""}

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    best, best_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0.0
    confident = bool(best_s >= 2.0 and (best_s - second_s) >= 1.0)
    why.append(len_note)
    return {
        "ok": True,
        "mood": best,
        "label": MOODS[best]["label"],
        "score": round(best_s, 2),
        "confident": confident,
        "rate": rate_adj + MOODS[best]["rate"],
        "why": why,
        "avg_len": round(avg_len, 1),
        "runner_up": ranked[1][0] if len(ranked) > 1 else "",
    }


def resolve(mood: str, rate=None, text: str = "") -> dict:
    """把「用户选的心情 + 语速」解析成最终生效参数。

    mood="auto"  -> 走 detect_mood(text)
    rate=None    -> 用心情的建议语速
    rate=数字    -> 用户明确指定，覆盖心情建议
    """
    auto = False
    det = {}
    m = (mood or "").strip().lower()
    if m in ("", "auto"):
        det = detect_mood(text)
        if not det.get("ok"):
            det = {"mood": "calm", "rate": 0, "why": [], "confident": False}
        m = det.get("mood", "calm")
        auto = True
    if m not in MOODS:
        m = "calm"
    cfg = dict(MOODS[m])
    cfg["key"] = m

    if rate is None or rate == "":
        final_rate = int(det.get("rate", cfg["rate"]) if auto else cfg["rate"])
    else:
        try:
            final_rate = int(rate)
        except (TypeError, ValueError):
            final_rate = cfg["rate"]
    final_rate = int(max(-50, min(50, final_rate)))

    # 语速反过来调制微动频率：说得快 -> 头动得稍快（幅度不动，防甩头）
    speed_k = 1.0 + (final_rate / 100.0) * 0.45
    speed_k = float(max(0.72, min(1.35, speed_k)))
    cfg["freq"] = float(cfg["freq"]) * speed_k
    cfg["rate"] = final_rate
    cfg["auto"] = auto
    cfg["auto_why"] = det.get("why", [])
    cfg["auto_confident"] = det.get("confident", False)
    cfg["auto_runner_up"] = det.get("runner_up", "")
    return cfg


# ---------------------------------------------------------------- 微表情
def _affine(frame, t: float, cfg: dict):
    """整头微动：绕脸上方一点旋转 + 轻微位移 + 预放大防露边。"""
    import cv2

    h, w = frame.shape[:2]
    f = float(cfg.get("freq", 0.30))
    sway = float(cfg.get("sway", 0.8))
    nod = float(cfg.get("nod", 0.8))
    rot = float(cfg.get("rot", 0.3))

    # 两个不同频率叠加，避免机械的单一正弦
    a1 = math.sin(2 * math.pi * f * t)
    a2 = math.sin(2 * math.pi * f * 0.61 * t + 1.7)
    dx = sway * (0.7 * a1 + 0.3 * a2)
    dy = nod * (0.6 * a1 + 0.4 * a2)
    th = rot * (0.5 * a1 + 0.5 * a2)

    # 预放大：覆盖最大位移 1.6px + 旋转带来的边角偏移
    zoom = 1.0 + max(0.004, (abs(dx) + abs(dy) + 2.0) / max(1.0, min(w, h)) * 2.2)
    cx, cy = w * 0.5, h * 0.42          # 转轴放在脸上方，像脖子带动的头
    M = cv2.getRotationMatrix2D((cx, cy), th, zoom)
    M[0, 2] += dx
    M[1, 2] += dy
    return cv2.warpAffine(frame, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REFLECT_101)


def _brow_band(frame, face, amt: float, t: float):
    """眉眼带极轻纵向位移。

    face = (x, y, w, h) 归一化的人脸框。眉眼带取脸高的 0.16~0.44。
    位移用高斯场，中心最强、两端衰减到 0，避免出现一条硬边。
    """
    import cv2

    if abs(amt) < 0.05 or not face:
        return frame
    h, w = frame.shape[:2]
    fx, fy, fw, fh = face
    x0 = int(max(0, (fx - fw * 0.05) * w))
    x1 = int(min(w, (fx + fw * 1.05) * w))
    y0 = int(max(0, (fy + fh * 0.16) * h))
    y1 = int(min(h, (fy + fh * 0.44) * h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return frame

    roi = frame[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    yy, xx = np.mgrid[0:rh, 0:rw].astype(np.float32)
    cy = rh * 0.5
    g = np.exp(-((yy - cy) ** 2) / (2 * (rh * 0.40) ** 2))
    mapy = yy - (amt * g)
    out = cv2.remap(roi, xx, mapy, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT_101)
    frame[y0:y1, x0:x1] = out
    return frame


def apply(frame, t: float, cfg: dict, face=None, energy: float = 0.5):
    """对一帧应用微表情。frame 是 BGR uint8（会被就地修改并返回）。

    energy: 0~1，当前帧的说话强度（可选）。说话时眉眼位移略大一点点，
            静音段几乎不动 —— 避免「不说话还在挤眉弄眼」。
    """
    if frame is None or not cfg:
        return frame
    if cfg.get("sway", 0) <= 0 and cfg.get("nod", 0) <= 0 and cfg.get("rot", 0) <= 0:
        return frame
    try:
        out = _affine(frame, t, cfg)
        amt = float(cfg.get("brow", 0)) * (0.35 + 0.65 * max(0.0, min(1.0, energy)))
        # 眉眼位移本身也是缓慢起伏，不是一直往上顶
        wave = math.sin(2 * math.pi * float(cfg.get("freq", 0.3)) * 0.8 * t + 0.6)
        out = _brow_band(out, face, amt * wave, t)
        # cv2.warpAffine / remap 都返回**新数组**，必须写回原 buffer，
        # 否则调用方 `apply(f, ...)` 不接返回值时等于什么都没做（实测踩过）。
        frame[...] = out
        return frame
    except Exception:                                   # noqa: BLE001
        return frame
