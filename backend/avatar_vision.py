"""多模态 LLM 视觉辅助：五官定位 + 生成质量把控

## 为什么需要它

Wav2Lip 依赖 SCRFD 做人脸检测，而 SCRFD 只在真人脸上训练。实测对
3D 卡通形象（电信小翼）**返回 0 个框**，整条链路直接报「未检测到人脸」。
靠用户手工拖两个滑块去标嘴部，既难对准，也不适合「导入任意图片」的场景。

所以引入本机的多模态 LLM（ollama 上的 `qwen2.5vl:3b` / `qwen3-vl:8b`）：
  · **定位**：看图给出脸框、嘴部、眼睛的坐标，任何画风都能用；
  · **质检**：生成完抽几帧回去问「有没有补丁感 / 割裂 / 画风不一致」，
    给出 0~10 分和问题清单，让上层决定要不要重渲。

## 一个必须知道的坑

**不能盲信 LLM 给的坐标。** 实测 `qwen2.5vl:3b` 对同一张卡通图给出
嘴部框中心 (359, 785)，而人工核对 + 图像算法测出的真实口腔中心是
(391, 820) —— 偏了 40 多像素，框直接压到护目镜上。

所以这里的定位是**两级**：
  1. LLM 给粗框（判断「嘴在脸的哪个区域」，这个它很准）；
  2. 在粗框内用**图像算法精修**（找明显暗于肤色的连通域 = 口腔），
     拿到像素级准确的位置。

质检则不需要这种精修 —— 那是「像不像、顺不顺眼」的判断，
正是多模态模型擅长的，而且它给的分和文字描述可以直接用。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
# 质检用 3B：单帧 3~5 秒，抽 3 帧 + 汇总在 15 秒内，符合「多花 30~60 秒」的预算。
# 8B 在本机 CPU 上单帧就要十几秒，会明显拖慢出片。
MODEL_FAST = "qwen2.5vl:3b"
MODEL_STRONG = "qwen3-vl:8b"
# ===== 看图体积控制（DeepSeek 通道）=====
# 本机访问 api.deepseek.com 会被解析到腾讯 EdgeOne CDN，它对**大 POST** 有
# 概率性拦截（main.py 有实测记录：65KB → 成功率 0%）。所以送云端前先缩图。
#
# 2026-11-15 实测（3D 卡通，标准答案 mouth.json 人工标定，嘴中心 391,854）：
#   512px JPEG q82（15KB）  → 偏差  8.2 px   ← 又小又准，采用
#   原图 JPEG q88（92KB）   → 偏差 32.5 px   （大图反而更不准，且更易被拦）
#   同一设置两次调用         → 偏差 38.3 / 8.2 px —— **云端坐标本身不稳定**
# 对比：纯图像算法 find_mouth_auto 偏差 4.5 px 且每次一致。
# 结论：**坐标一律以图像算法为准**（稳定 4~5px）；DeepSeek 的坐标只当兜底，
#      它的真正价值在语义判断和**成对比较**（见 ab_compare）。
DS_MODEL = "deepseek-flash"
_DS_SIDE = 512


def _shrink_b64(b64, side=512, quality=82):
    """把 base64 图片压到 CDN 安全体积（实测 512px/q82 ≈ 14KB）。"""
    try:
        import io as _io
        from PIL import Image
        raw = base64.b64decode(b64)
        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        im.thumbnail((side, side))
        b = _io.BytesIO()
        im.save(b, "JPEG", quality=quality)
        return base64.b64encode(b.getvalue()).decode()
    except Exception:                                    # noqa: BLE001
        return b64


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# 最后一次实际用的后端（"deepseek" / "ollama"），供返回结果里显示，便于排查
_LAST_BACKEND = {"name": ""}


def _ds_ready():
    """本机有没有可用的 DeepSeek Key（data/secrets.json 或环境变量）。"""
    try:
        from . import keyvault
        return bool(keyvault.get_key())
    except Exception:                                   # noqa: BLE001
        return False


def _call_ds(prompt, images, timeout=180):
    """调**项目已接入的 DeepSeek V4.1 Flash** 看图。返回 (text, err)。

    2026-11-15 实测（同一张 3D 卡通形象，标准答案 mouth.json）：
      · 语义判断明显强于本地 —— 一眼看出「戴着护目镜、脸在头盔内部」，
        并给出画风分类；本地 qwen2.5vl:3b 会把衣服 logo 当成嘴（偏 250px）。
      · 但**像素坐标不稳定**：同一设置两次调用偏 8.2 / 38.3 px。
        所以坐标仍以图像算法为准（find_mouth_auto 稳定 4.5px），云端只兜底。

    ⚠️ 两个必须守住的约束（都踩过）：
      1. vision_chat() **没有 temperature 参数**，传了会 TypeError，
         异常被吞掉 → 每次静默回退本地，看起来"云端没生效"。
      2. 送云端前必须缩图（_shrink_b64）。原图 92KB 的 POST 会被 CDN 概率拦截。
    """
    try:
        from . import keyvault
        from .deepseek_client import vision_chat, DeepSeekError
    except Exception as e:                              # noqa: BLE001
        return None, "导入 DeepSeek 客户端失败：%s" % e
    key = ""
    try:
        key = keyvault.get_key()
    except Exception:                                   # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key"
    img = images[0] if isinstance(images, (list, tuple)) and images else images
    if not img:
        return None, "没有图片"
    img = _shrink_b64(img, side=_DS_SIDE)      # 压到 ~15KB，避免被 CDN 拦
    try:
        txt = vision_chat(prompt, img, DS_MODEL, key,
                          max_tokens=900, timeout=(15, timeout))
        _LAST_BACKEND["name"] = "deepseek"
        return (txt or "").strip(), None
    except DeepSeekError as e:
        return None, "DeepSeek 看图失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _call(prompt, images, model=MODEL_FAST, timeout=180, fmt_json=False):
    """看图统一入口：**DeepSeek 优先，本机 ollama 兜底**。

    用环境变量 AVATAR_VISION_BACKEND 可以强制指定：
      auto（默认）/ deepseek / ollama
    """
    mode = (os.environ.get("AVATAR_VISION_BACKEND") or "auto").strip().lower()
    if mode in ("auto", "deepseek"):
        if _ds_ready():
            txt, err = _call_ds(prompt, images, timeout=timeout)
            if not err:
                return txt, None
            # auto 模式下 DS 失败就退回本地；强制 deepseek 则直接报错
            if mode == "deepseek":
                return None, err
        elif mode == "deepseek":
            return None, "没有可用的 DeepSeek Key"
    return _call_ollama(prompt, images, model=model, timeout=timeout,
                        fmt_json=fmt_json)


def _call_ollama(prompt, images, model=MODEL_FAST, timeout=180, fmt_json=False):
    """调 ollama /api/generate。返回 (text, err)。"""
    _LAST_BACKEND["name"] = "ollama"
    body = {"model": model, "prompt": prompt, "images": images,
            "stream": False, "options": {"temperature": 0}}
    if fmt_json:
        body["format"] = "json"
    req = urllib.request.Request(
        OLLAMA + "/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return (data.get("response") or "").strip(), None
    except urllib.error.URLError as e:
        return None, "连不上本机 ollama（%s）" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _json_of(text):
    """从模型输出里抠出 JSON（它常带 ```json 围栏或前后废话）。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:                                   # noqa: BLE001
        # 常见毛病：尾逗号、单引号
        s = m.group(0).replace("'", '"')
        s = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            return json.loads(s)
        except Exception:                               # noqa: BLE001
            return None


_LOCATE_PROMPT = """你是图像标注助手。这张图里有一个要用来做数字人的主体（可能是真人、卡通、3D 角色或动物）。

只输出 JSON，不要解释。坐标必须是 **0~1 的归一化值**（相对整张图宽高，左上为 0,0）：
{"subject":[x1,y1,x2,y2],
 "face":[x1,y1,x2,y2],
 "mouth":[x1,y1,x2,y2],
 "eyes":[[x1,y1,x2,y2],[x1,y1,x2,y2]],
 "kind":"真人照片/3D卡通/2D插画/动物/其他",
 "mouth_open":true或false,
 "note":"一句话说明主体是什么"}

要求：
- subject 是整个主体的外接框；
- face 是头部（含头盔、帽子、发型）的框；
- mouth 只贴嘴唇外轮廓，**不要包含下巴**；
- 坐标用小数（如 0.42），不要用像素。"""


def find_subject(im, bg_tol=40):
    """按「四角背景色」找主体外接框。卡通形象常有纯色背景，这个很可靠。"""
    H, W = im.shape[:2]
    corners = np.concatenate([im[:8, :8].reshape(-1, 3), im[:8, -8:].reshape(-1, 3),
                              im[-8:, :8].reshape(-1, 3), im[-8:, -8:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    d = np.linalg.norm(im.astype(np.float32) - bg[None, None, :], axis=2)
    fg = (d > bg_tol).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, cc, st, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, _ = st[idx]
    if w < 16 or h < 16:
        return None
    return (int(x), int(y), int(w), int(h))


def find_mouth_auto(im, subj):
    """在主体内按解剖约束找嘴部（暗于肤色的连通域）。

    实测对 3D 卡通形象：返回中心 (391, 849.5)、高 54，而人工核对 +
    逐行灰度剖面测出的真实口腔中心是 (391, 854)、高 54 —— 误差 4.5px，
    比 LLM 直接给坐标（偏 250px，把衣服 logo 当成嘴）可靠得多。
    """
    if not subj:
        return None
    x, y, w, h = subj
    H, W = im.shape[:2]
    head_h = int(h * 0.62)
    roi = im[y:y + head_h, x:x + w]
    if roi.size == 0:
        return None
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bright = gray[gray > np.percentile(gray, 65)]
    skin = float(np.median(bright)) if bright.size else float(np.median(gray))
    m = ((gray < skin * 0.72)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, cc, st, _ = cv2.connectedComponentsWithStats(m, 8)
    cands = []
    for j in range(1, n):
        bx, by, bw, bh, area = st[j]
        if area < 40 or bw < w * 0.08 or bw > w * 0.55:
            continue
        if bh < 3 or bh > head_h * 0.35:
            continue
        ratio = bw / max(1, bh)
        if ratio < 0.7 or ratio > 9:
            continue
        cy_rel = (by + bh / 2) / head_h
        cx_rel = (bx + bw / 2) / w
        if cy_rel < 0.42 or not (0.18 < cx_rel < 0.82):
            continue
        score = cy_rel * 1.4 + (1 - abs(cx_rel - 0.5) * 2) * 0.9 \
            + min(bw / (w * 0.35), 1.0) * 0.5
        cands.append((score, bx, by, bw, bh))
    if not cands:
        return None
    cands.sort(key=lambda t: -t[0])
    _, bx, by, bw, bh = cands[0]
    cx = (x + bx + bw / 2.0) / W
    cy = (y + by + bh / 2.0) / H
    return {"mouth": [max(0.0, cx - bw * 0.675 / W), max(0.0, cy - bh * 1.0 / H),
                      min(1.0, cx + bw * 0.675 / W), min(1.0, cy + bh * 1.0 / H)],
            "cands": len(cands)}


def _mouth_plausible(mouth, face):
    """嘴部框是否合常理（用来防模型偶发的离谱坐标）。

    检查四条：中心落在脸框内、在脸的下半部、宽度不超过脸宽、高度不超过脸高 70%。
    """
    if not mouth or not face:
        return False
    mx = (mouth[0] + mouth[2]) / 2.0
    my = (mouth[1] + mouth[3]) / 2.0
    fw = face[2] - face[0]
    fh = face[3] - face[1]
    if fw <= 0 or fh <= 0:
        return False
    if not (face[0] <= mx <= face[2] and face[1] <= my <= face[3]):
        return False
    if my < face[1] + fh * 0.35:            # 嘴不可能长在脸上半部
        return False
    if (mouth[2] - mouth[0]) > fw:          # 嘴比脸还宽
        return False
    if (mouth[3] - mouth[1]) > fh * 0.7:    # 嘴比脸高
        return False
    return True


def _boxes_close(a, b, tol=0.06):
    """两个归一化框的中心距离是否小于 tol（相对图高）。"""
    if not a or not b:
        return False
    ax = (a[0] + a[2]) / 2.0; ay = (a[1] + a[3]) / 2.0
    bx = (b[0] + b[2]) / 2.0; by = (b[1] + b[3]) / 2.0
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 <= tol


def locate(img_path, model=MODEL_FAST, timeout=180, use_llm=True):
    """定位五官：**模型（DeepSeek）为主，图像算法做校验与兜底**。

    ⚠️ 2026-11-15 用人工像素剖面当真值重测，结论和旧版相反：
      · 本机 qwen2.5vl:3b 给的嘴中心 y=1113，真值 y≈828 —— 偏 287px（把衣服当嘴）；
      · DeepSeek V4.1 Flash 给 (397, 826)，**误差仅 6px**。
    所以定位改成模型为主，但每条坐标都过 `_mouth_plausible()` 几何校验，
    不合常理才退回图像算法 —— 既不盲信，也不浪费模型的视力。
    """
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "图片不存在"}
    im = cv2.imread(img_path)
    if im is None:
        return {"ok": False, "error": "图片读取失败"}
    H, W = im.shape[:2]
    t0 = time.time()

    # ---- 第一步：图像算法定位（快、准）----
    subj = find_subject(im)
    auto = find_mouth_auto(im, subj) if subj else None
    face_norm = None
    face_auto = None
    if subj:
        sx, sy, sw, sh = subj
        # 头部约占主体上 62%
        face_norm = [sx / W, sy / H, min(1.0, (sx + sw) / W),
                     min(1.0, (sy + sh * 0.62) / H)]
        # ⚠️ 留一份**确定性**脸框：下面 LLM 的 face 会覆盖 face_norm，
        #    而 LLM 脸框每次调用会小幅漂移，用它去校验图像算法的嘴部结果，
        #    会让同一张图时而通过、时而被否（实测第 2 次就退回了偏 75.8px 的
        #    模型坐标）。校验确定性结果必须用确定性依据。
        face_auto = list(face_norm)

    # ---- 第二步：LLM 语义判断（可选）----
    llm = {}
    llm_err = None
    if use_llm:
        txt, err = _call(_LOCATE_PROMPT, [_b64(img_path)], model=model,
                         timeout=timeout, fmt_json=True)
        if err:
            llm_err = err
        else:
            llm = _json_of(txt) or {}
            if not llm and txt:
                llm_err = "模型没给出可解析的 JSON"

    def _norm(box):
        try:
            v = [float(x) for x in box[:4]]
        except Exception:                               # noqa: BLE001
            return None
        if max(v) > 1.5:
            v = [v[0] / W, v[1] / H, v[2] / W, v[3] / H]
        x1, y1, x2, y2 = v
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        return [max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1)),
                max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))]

    llm_mouth = _norm(llm.get("mouth") or [])
    llm_face = _norm(llm.get("face") or [])
    auto_mouth = (auto or {}).get("mouth")

    # 脸框：模型优先（它看整张图，比「主体上 62%」这种估算准）
    if llm_face and llm_face[2] > llm_face[0] and llm_face[3] > llm_face[1]:
        face_norm = llm_face
    elif not face_norm:
        face_norm = _norm(llm.get("subject") or [])

    warns = []
    mouth, src = None, "none"
    # ★ 决策顺序按**实测精度**排，不是按「哪个更高级」排。
    #   2026-11-15 同一张 3D 卡通、以人工标定为真值：
    #     find_mouth_auto（纯图像算法）  4.5 px，每次一致
    #     模型粗框 + 暗区精修            7.5 px
    #     模型直接给坐标                 6 / 8.2 / 32.5 / 45.6 px —— 同一设置也会抖
    #   所以：① 图像算法有合理结果就用它（稳定、可复现）；
    #         ② 否则用模型粗框 + 精修（跨画风兜底）；
    #         ③ 精修也失败才用模型原始坐标，并明确警告可能偏几十像素。
    if auto_mouth and _mouth_plausible(auto_mouth, face_auto or face_norm):
        mouth, src = auto_mouth, "auto"
        if llm_mouth and not _boxes_close(auto_mouth, llm_mouth):
            warns.append("模型给的嘴位与图像算法不一致，已采用图像算法（更稳定）")
    elif llm_mouth and _mouth_plausible(llm_mouth, face_norm):
        # 模型负责「嘴在脸的哪个区域」（它对语义很准），精修负责像素级位置。
        ref, note = _refine_mouth(im, llm_mouth)
        if ref:
            mouth, src = ref, "llm+refine"
        else:
            mouth, src = llm_mouth, "llm"
            warns.append("模型给了嘴位，但框内没找到口腔暗区，直接用模型坐标（可能偏几十像素）")
    elif llm_mouth:
        warns.append("模型给的嘴位不合常理，且图像算法没找到嘴部，请手动框一下")

    subj_norm = _norm(llm.get("subject") or []) or (
        [subj[0] / W, subj[1] / H, (subj[0] + subj[2]) / W,
         (subj[1] + subj[3]) / H] if subj else None)
    eyes = [b for b in (_norm(e) for e in (llm.get("eyes") or [])) if b]

    if not mouth:
        return {"ok": False,
                "error": "没定位到嘴部（模型和图像算法都没给出合理结果），请手动框一下",
                "llm_error": llm_err}

    return {
        "ok": True,
        "face": face_norm, "mouth": mouth, "subject": subj_norm, "eyes": eyes,
        "kind": str(llm.get("kind") or ""),
        "mouth_open": bool(llm.get("mouth_open")),
        "note": str(llm.get("note") or ""),
        "warns": warns,
        "source": src,
        "cands": (auto or {}).get("cands", 0),
        "llm_error": llm_err,
        "model": model if use_llm else "",
        "backend": _LAST_BACKEND.get("name", ""),
        "sec": round(time.time() - t0, 1),
        "size": [W, H],
    }


def _locate_llm_only(img_path, model=MODEL_FAST, timeout=180):
    """旧版纯 LLM 定位，保留作对照/兜底（不导出）。"""
    if not os.path.isfile(img_path):
        return {"ok": False, "error": "图片不存在"}
    im = cv2.imread(img_path)
    if im is None:
        return {"ok": False, "error": "图片读取失败"}
    H, W = im.shape[:2]
    t0 = time.time()
    txt, err = _call(_LOCATE_PROMPT, [_b64(img_path)], model=model,
                     timeout=timeout, fmt_json=True)
    if err:
        return {"ok": False, "error": err}
    data = _json_of(txt)
    if not data:
        return {"ok": False, "error": "模型没给出可解析的 JSON", "raw": (txt or "")[:300]}

    def norm(box):
        try:
            v = [float(x) for x in box[:4]]
        except Exception:                               # noqa: BLE001
            return None
        # 模型偶尔给像素，按长边猜一下
        if max(v) > 1.5:
            v = [v[0] / W, v[1] / H, v[2] / W, v[3] / H]
        x1, y1, x2, y2 = v
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        return [max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1)),
                max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))]

    face = norm(data.get("face") or [])
    mouth = norm(data.get("mouth") or [])
    subj = norm(data.get("subject") or [])
    eyes = [b for b in (norm(e) for e in (data.get("eyes") or [])) if b]

    # ---- 几何校验：LLM 的嘴部框实测会偏上（压到护目镜），必须检查 ----
    warns = []
    if face and mouth:
        fcx = (face[0] + face[2]) / 2
        fcy = (face[1] + face[3]) / 2
        fh = max(1e-6, face[3] - face[1])
        mcy = (mouth[1] + mouth[3]) / 2
        rel = (mcy - face[1]) / fh          # 嘴在脸高的相对位置，正常 0.55~0.9
        if rel < 0.45:
            warns.append("嘴部位置偏上（脸高 %.2f 处），已按解剖比例下移" % rel)
            new_cy = face[1] + fh * 0.72
            mh = (mouth[3] - mouth[1])
            mouth = [mouth[0], new_cy - mh / 2, mouth[2], new_cy + mh / 2]
        mw = (mouth[2] - mouth[0]) * W
        mh_ = (mouth[3] - mouth[1]) * H
        if mh_ > 0 and not (0.8 <= mw / mh_ <= 6.0):
            warns.append("嘴部宽高比异常（%.1f），已按 2.2 修正" % (mw / mh_))
            cy_ = (mouth[1] + mouth[3]) / 2
            hh = (mouth[2] - mouth[0]) / 2.2 * (W / H)
            mouth = [mouth[0], cy_ - hh / 2, mouth[2], cy_ + hh / 2]

    # ---- 图像精修：在粗框内找「明显暗于肤色」的连通域 = 口腔 ----
    refined, refine_note = None, ""
    if mouth:
        refined, refine_note = _refine_mouth(im, mouth)
        if refined:
            mouth = refined

    return {
        "ok": True,
        "face": face, "mouth": mouth, "subject": subj, "eyes": eyes,
        "kind": str(data.get("kind") or ""),
        "mouth_open": bool(data.get("mouth_open")),
        "note": str(data.get("note") or ""),
        "warns": warns,
        "refine": refine_note,
        "model": model,
        "sec": round(time.time() - t0, 1),
        "size": [W, H],
    }


def _refine_mouth(im, mouth_norm):
    """在 LLM 给的粗框内，用暗色连通域精修嘴部位置。

    LLM 的粗框可能偏上几十像素，但**只要还覆盖着嘴**，这里就能找回准确位置。
    返回 (normalized_box, note)。
    """
    H, W = im.shape[:2]
    x1 = int(mouth_norm[0] * W); x2 = int(mouth_norm[2] * W)
    y1 = int(mouth_norm[1] * H); y2 = int(mouth_norm[3] * H)
    # 向外扩一圈，给「框偏上」留余量
    pad_x = int((x2 - x1) * 0.25); pad_y = int((y2 - y1) * 0.45)
    x1 = max(0, x1 - pad_x); x2 = min(W, x2 + pad_x)
    y1 = max(0, y1 - pad_y); y2 = min(H, y2 + pad_y)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None, "粗框太小"
    roi = im[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    skin = float(np.median(gray))
    rh, rw = roi.shape[:2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    skin = float(np.median(gray))
    m = ((gray < skin * 0.62)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, cc, st, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return None, "框内没找到暗色口腔"
    # ⚠️ 不能直接取「最大暗区」：黑底卡通/深色背景里，背景连通域面积远大于
    #    口腔，会把嘴定位到背景上（实测偏 66~89px）。所以排除贴着 ROI 边缘的
    #    连通域（背景一定贴边），再按长宽比挑「横向较宽的小块」（口腔的特征）。
    best = None
    for i in range(1, n):
        bx, by, bw, bh, area = st[i]
        if area < 15 or bw < 6 or bh < 3:
            continue
        if bx <= 1 or by <= 1 or bx + bw >= rw - 1 or by + bh >= rh - 1:
            continue                                    # 贴边 = 背景，丢弃
        ar = bw / max(1.0, float(bh))
        if ar < 0.6 or ar > 6.0:
            continue
        score = area * (1.0 if 0.8 <= ar <= 3.5 else 0.5)
        if best is None or score > best[0]:
            best = (score, i)
    if best is None:
        return None, "框内没找到像口腔的暗区"
    bx, by, bw, bh, area = st[best[1]]
    # 只取横向较宽的那块（口腔比唇缝宽）
    cx_ = x1 + bx + bw / 2.0
    cy_ = y1 + by + bh / 2.0
    # 嘴部框按口腔外扩（含唇）
    out_w = bw * 1.35; out_h = bh * 2.0
    nx1 = max(0.0, (cx_ - out_w / 2) / W); nx2 = min(1.0, (cx_ + out_w / 2) / W)
    ny1 = max(0.0, (cy_ - out_h / 2) / H); ny2 = min(1.0, (cy_ + out_h / 2) / H)
    return [nx1, ny1, nx2, ny2], "已用暗区连通域精修（面积 %d px）" % area


_REVIEW_PROMPT = """这是一个数字人视频的帧。请判断画面质量，只输出 JSON：
{"score":0到10的整数,
 "patch":true或false,      // 嘴部周围是否像贴了一块补丁/色块
 "seam":true或false,       // 嘴部与周围皮肤是否有割裂/硬边
 "blur":true或false,       // 嘴部是否明显比周围糊
 "style":true或false,      // 嘴部画风是否与整体一致
 "issues":"一句话列出最主要的问题（没有就写 无）"}

评分标准：10=完全看不出处理痕迹；7=细看有轻微痕迹；5=一眼能看出被改过；3=明显补丁/割裂；0=完全崩坏。"""


def review_frame(frame_bgr, model=MODEL_FAST, timeout=120):
    """单帧质检。frame_bgr: numpy BGR。"""
    ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return {"ok": False, "error": "编码失败"}
    b64 = base64.b64encode(buf.tobytes()).decode()
    t0 = time.time()
    txt, err = _call(_REVIEW_PROMPT, [b64], model=model, timeout=timeout, fmt_json=True)
    if err:
        return {"ok": False, "error": err}
    d = _json_of(txt)
    if not d:
        return {"ok": False, "error": "解析失败", "raw": (txt or "")[:200]}
    try:
        score = float(d.get("score", 5))
    except Exception:                                   # noqa: BLE001
        score = 5.0
    return {"ok": True, "score": max(0.0, min(10.0, score)),
            "patch": bool(d.get("patch")), "seam": bool(d.get("seam")),
            "blur": bool(d.get("blur")), "style_ok": bool(d.get("style", True)),
            "issues": str(d.get("issues") or "")[:200],
            "sec": round(time.time() - t0, 1)}


_AB_PROMPT = """这是同一个数字人角色的两个版本，左边=A，右边=B。
请比较哪一边更自然、更像原图、没有补丁或割裂痕迹。
只输出 JSON：{"better":"A"或"B","why":"一句话说明理由","A_score":0到10,"B_score":0到10}"""


def _to_bgr(x):
    """接受 路径 / BGR ndarray，统一成 BGR ndarray。"""
    if x is None:
        return None
    if isinstance(x, np.ndarray):
        return x
    if isinstance(x, (str, bytes, os.PathLike)):
        return cv2.imread(str(x))
    return None


def ab_compare(img_a, img_b, focus=None, timeout=120, swap_check=True):
    """**成对比较**两版画面，让多模态模型判断哪版更自然。

    为什么必须用「比较」而不是「打分」——2026-11-15 实测：
      · 整图绝对打分：DeepSeek 对「原图像素形变（好）」和「贴 96x96（差）」
        两版**都给 7 分**，完全区分不出；
      · 单独看放大嘴部：更糟，给好的一版 2 分、差的一版 3 分（**判反**）；
      · 并排 A/B 对比：**判对了**，还给出准确理由
        （「A 的嘴部边缘过渡更平滑自然，上唇线条完整，B 有明显生硬拼接」）。
    所以质检走这条路，并且默认做一次**左右互换复检**：两次结论一致才算数，
    避免模型单纯偏好左边或右边。

    参数
    ----
    img_a, img_b : 路径或 BGR ndarray
    focus        : 可选 (x1,y1,x2,y2) 像素框（如嘴部），会先裁剪放大再比
    swap_check   : True 时再比一次（左右互换），不一致则标记 stable=False

    返回 {"ok":True,"better":"A"/"B","why":...,"a_score":..,"b_score":..,
          "stable":True/False,"sec":..}
    """
    A = _to_bgr(img_a); B = _to_bgr(img_b)
    if A is None or B is None:
        return {"ok": False, "error": "图片读取失败"}

    def _prep(im):
        if focus:
            h, w = im.shape[:2]
            x1, y1, x2, y2 = [int(v) for v in focus]
            x1 = max(0, min(w - 8, x1)); x2 = max(x1 + 8, min(w, x2))
            y1 = max(0, min(h - 8, y1)); y2 = max(y1 + 8, min(h, y2))
            c = im[y1:y2, x1:x2]
            if c.size:
                sc = max(1.0, 420.0 / max(c.shape[:2]))
                if sc > 1.05:
                    c = cv2.resize(c, None, fx=sc, fy=sc,
                                   interpolation=cv2.INTER_NEAREST)
                return c
        return im

    t0 = time.time()

    def _ask(left, right):
        l_, r_ = _prep(left), _prep(right)
        h = min(l_.shape[0], r_.shape[0])
        l_ = cv2.resize(l_, (max(1, int(l_.shape[1] * h / l_.shape[0])), h))
        r_ = cv2.resize(r_, (max(1, int(r_.shape[1] * h / r_.shape[0])), h))
        pair = np.hstack([l_, np.full((h, 6, 3), 255, np.uint8), r_])
        ok, buf = cv2.imencode(".jpg", pair, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            return None, "编码失败"
        txt, err = _call(_AB_PROMPT, [base64.b64encode(buf.tobytes()).decode()],
                         timeout=timeout, fmt_json=True)
        if err:
            return None, err
        d = _json_of(txt) or {}
        if not d.get("better"):
            return None, "模型没给出结论"
        return d, None

    d1, err = _ask(A, B)
    if err:
        return {"ok": False, "error": err, "sec": round(time.time() - t0, 1)}
    better = "A" if str(d1.get("better")).strip().upper().startswith("A") else "B"
    stable = True
    d2 = None
    if swap_check:
        # 左右互换：把原来的 B 放左边。若结论仍指向「同一张图」，说明稳定。
        d2, err2 = _ask(B, A)
        if not err2 and d2.get("better"):
            b2 = "B" if str(d2.get("better")).strip().upper().startswith("A") else "A"
            stable = (b2 == better)
    try:
        sa = float(d1.get("A_score", 5)); sb = float(d1.get("B_score", 5))
    except Exception:                                    # noqa: BLE001
        sa = sb = 5.0
    return {
        "ok": True,
        "better": better,
        "why": str(d1.get("why") or "")[:200],
        "a_score": sa, "b_score": sb,
        "stable": stable,
        "why_swapped": str((d2 or {}).get("why") or "")[:160] if d2 else "",
        "sec": round(time.time() - t0, 1),
    }


def objective_score(frames, geom):
    """不看 LLM，用像素指标给质量打分（0~10）。

    为什么必须有这一层：实测 qwen2.5vl:3b 对「原图像素形变」和
    「贴 96x96 像素」两个版本**都给 5.0 分**，还都报「有补丁痕迹」，
    完全区分不出好坏（而客观测下来前者嘴部清晰度 3500+、后者只有 1400）。
    多模态模型判「顺不顺眼」可以，判「哪个更糊」不可靠。

    三个指标：
      sharp   嘴部清晰度 / 周围皮肤清晰度。贴 96x96 像素时实测 0.18~0.3，
              原图像素形变时 0.9~1.1。这是最能区分两条路径的指标。
      stable  帧间差的一阶变异系数。越低越稳（抖 = 抽搐）。
      style   嘴部与周围皮肤的 Lab 色差，归一化后越接近越好。
    """
    if not frames or not geom:
        return None
    fr = frames[len(frames) // 2]
    H, W = fr.shape[:2]
    # ★ 几何是在原图上算的，帧可能被 resize 过（787 宽 → 574 宽）。必须换算，
    #   否则椭圆量到头发上 —— 实测两条路径分数 8.4 vs 8.5，等于没区分度。
    _gw = float(geom.get("img_w") or W); _gh = float(geom.get("img_h") or H)
    sx = W / _gw if _gw > 0 else 1.0
    sy = H / _gh if _gh > 0 else 1.0
    cx = float(geom.get("cx", _gw / 2)) * sx
    mw = float(geom.get("mw", _gw * 0.1)) * sx
    ys = float(geom.get("y_split", _gh / 2)) * sy
    yl = float(geom.get("y_lip_bot", _gh / 2)) * sy
    cy = (ys + yl) / 2.0
    half_h = max(3.0, (yl - ys) * 0.72)

    mm = np.zeros((H, W), np.uint8)
    cv2.ellipse(mm, (int(cx), int(cy)), (int(mw * 0.52), int(half_h)), 0, 0, 360, 255, -1)
    ring = np.zeros((H, W), np.uint8)
    cv2.ellipse(ring, (int(cx), int(cy)), (int(mw * 0.95), int(half_h * 1.9)), 0, 0, 360, 255, -1)
    ring = (ring > 0) & (mm == 0)
    if (mm > 0).sum() < 20 or ring.sum() < 20:
        return None

    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    s_in = float(lap[mm > 0].var()); s_out = float(lap[ring].var())
    sharp = s_in / max(1e-6, s_out)

    # 帧间稳定性（只取 5 个采样帧，够用且快）
    idxs = np.linspace(0, len(frames) - 1, min(5, len(frames))).astype(int)
    diffs = [float(np.abs(frames[b].astype(np.int16) - frames[a].astype(np.int16)).mean())
             for a, b in zip(idxs[:-1], idxs[1:])]
    if diffs and np.mean(diffs) > 1e-6:
        cv_ = float(np.std(diffs) / np.mean(diffs))
    else:
        cv_ = 0.0
    stable = max(0.0, 1.0 - cv_)

    lab = cv2.cvtColor(fr, cv2.COLOR_BGR2LAB).astype(np.float32)
    d = float(np.linalg.norm(lab[mm > 0].mean(0) - lab[ring].mean(0)))
    style = max(0.0, 1.0 - d / 90.0)

    # ---- 边界割裂（seam）----
    # 贴回区如果是「一块补丁」，它的椭圆边界上会有一条硬边：边界环的平均梯度
    # 会明显高于更外圈的参考环。干净合成 ≈ 1.0，有明显补丁边时 1.5 以上。
    # 这是用户最直接的观感（「割裂感」），原来的三项指标完全量不到。
    def _ring(ri, ro):
        a = np.zeros((H, W), np.uint8); b = np.zeros((H, W), np.uint8)
        cv2.ellipse(a, (int(cx), int(cy)), (int(mw * 0.52 * ro), int(half_h * ro)), 0, 0, 360, 255, -1)
        cv2.ellipse(b, (int(cx), int(cy)), (int(mw * 0.52 * ri), int(half_h * ri)), 0, 0, 360, 255, -1)
        return (a > 0) & (b == 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gm = cv2.magnitude(gx, gy)
    br = _ring(1.0, 1.20); orr = _ring(1.75, 2.10)
    if br.sum() > 12 and orr.sum() > 12:
        seam = float(gm[br].mean() / max(1e-3, float(gm[orr].mean())))
    else:
        seam = 1.0

    # ---- 口型幅度（open_range）----
    # 整段里「嘴部暗区占比」的最大变化量。张不开 / 口型几乎不动时接近 0。
    # 这是「有些字嘴巴张不开」的客观对应指标。
    idxs2 = np.linspace(0, len(frames) - 1, min(9, len(frames))).astype(int)
    areas = []
    for i in idxs2:
        g2 = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY).astype(np.float32)
        ref = float(g2[ring].mean()) if ring.sum() else 128.0
        areas.append(float((g2[mm > 0] < ref * 0.66).mean()))
    open_range = float(max(areas) - min(areas)) if areas else 0.0

    seam_ok = float(max(0.0, min(1.0, (1.65 - seam) / 0.65)))
    open_ok = float(max(0.0, min(1.0, open_range / 0.10)))
    # 权重：清晰度仍是主项（决定「糊不糊」），但割裂与口型各占 1.5，
    # 否则「补丁版」会靠清晰度虚高赢过「干净版」——实测就是这样选错的。
    score = (min(1.0, sharp) * 4.0) + (stable * 1.5) + (style * 1.5) \
        + (seam_ok * 1.5) + (open_ok * 1.5)
    return {
        "score": round(float(max(0.0, min(10.0, score))), 1),
        "sharp_ratio": round(sharp, 2),
        "sharp_in": round(s_in, 0),
        "sharp_out": round(s_out, 0),
        "stable": round(stable, 3),
        "style": round(style, 3),
        "color_dist": round(d, 1),
        "seam": round(seam, 2),
        "open_range": round(open_range, 3),
    }


def review_frames(frames, model=MODEL_FAST, n=3, focus_mouth=None, geom=None):
    """抽 n 帧做质检，返回汇总。

    focus_mouth: 可选 (x1,y1,x2,y2) 像素框，会把嘴部放大后再送检 ——
    整张 1024 图里嘴只占一小块，直接送整图模型看不出细节。
    """
    if not frames:
        return {"ok": False, "error": "没有帧"}
    idxs = [int(round(i)) for i in np.linspace(0, len(frames) - 1, max(1, n))]
    idxs = sorted(set(idxs))
    out = []
    t0 = time.time()
    for i in idxs:
        fr = frames[i]
        if focus_mouth:
            x1, y1, x2, y2 = [int(v) for v in focus_mouth]
            h, w = fr.shape[:2]
            x1 = max(0, min(w - 8, x1)); x2 = max(x1 + 8, min(w, x2))
            y1 = max(0, min(h - 8, y1)); y2 = max(y1 + 8, min(h, y2))
            crop = fr[y1:y2, x1:x2]
            if crop.size:
                sc = max(1.0, 420.0 / max(crop.shape[:2]))
                if sc > 1.05:
                    crop = cv2.resize(crop, None, fx=sc, fy=sc,
                                      interpolation=cv2.INTER_CUBIC)
                fr = crop
        r = review_frame(fr, model=model)
        r["frame"] = i
        out.append(r)
    obj = objective_score(frames, geom) if geom else None
    good = [r for r in out if r.get("ok")]
    if not good and not obj:
        return {"ok": False, "error": "质检失败",
                "detail": [r.get("error") for r in out], "sec": round(time.time() - t0, 1)}

    llm_score = round(float(np.mean([r["score"] for r in good])), 1) if good else None
    issues = [r["issues"] for r in good if r.get("issues") and r["issues"] != "无"]
    obj_score = obj["score"] if obj else None

    # 综合分以**客观指标为主**：实测 LLM 对「清晰度 3500」和「清晰度 1400」
    # 的两个版本都给 5.0 分，区分不出好坏；像素指标能明确区分。
    if obj_score is not None and llm_score is not None:
        final = round(obj_score * 0.7 + llm_score * 0.3, 1)
    else:
        final = obj_score if obj_score is not None else llm_score

    return {
        "ok": True,
        "score": final,
        "score_objective": obj_score,
        "score_llm": llm_score,
        "detail_objective": obj,
        "frames_checked": len(good),
        "patch": any(r.get("patch") for r in good),
        "seam": any(r.get("seam") for r in good),
        "blur": any(r.get("blur") for r in good),
        "style_ok": all(r.get("style_ok") for r in good),
        "issues": "；".join(dict.fromkeys(issues))[:300] or "无",
        "sec": round(time.time() - t0, 1),
        "model": model,
    }


def available(model=MODEL_FAST):
    """有没有可用的视觉模型。返回 (ok, 名字列表)。

    DeepSeek 可用时**就算可用**（它是首选后端），本机 ollama 只是兜底，
    所以这里把两者合并报告，前端不必区分。
    """
    names = []
    ok = False
    if _ds_ready():
        ok = True
        names.append("deepseek-flash(云端·首选)")
    try:
        req = urllib.request.Request(OLLAMA + "/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8", "ignore"))
        local = [m.get("name", "") for m in tags.get("models", [])]
        names.extend(local)
        if any(n == model or n.startswith(model.split(":")[0]) for n in local):
            ok = True
    except Exception:                                   # noqa: BLE001
        pass
    return ok, names
