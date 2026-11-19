# -*- coding: utf-8 -*-
"""语音画布：把「改成红色 / 删掉这张 / 合并前两张」这种**指着卡片说**的话，
解析成对画布卡片的具体操作（改色 / 删除 / 合并）。

为什么要单独一个模块（2026-11-20）
----------------------------------
摩擦点「同一需求被重复发送」—— 用户反复描述同一个修改意图，因为文字流里
**没法指认具体对象**，只能整段重述「我是说汕头那天那张图」。
前端已经有卡片画布（artifacts.js 的 renderCanvas，卡片带 data-card-id，
可拖、可点选），但点选之后仍然要把「怎么改」打成一段话发给模型，再等一大轮回复。
这里补上另一半：**指哪改哪** —— 一句话直接落到 card-id 上，当场改。

职责划分（刻意不混在一起）
--------------------------
  · 本模块：**只做纯逻辑** —— 口语 → {action, card_ids, color} → 卡片数组。
    不碰 DOM、不碰 PIL、不联网、不调模型，所以能秒级断言。
  · frontend/app.js 的 handleCanvasCommand：拿这里的返回值去改 DOM。
  · frontend/artifacts.js 的 renderCanvas：排版 / 拖动位置（已有，不重复造）。
  · backend/composite.py：像素级**图片**合成，跟卡片寻址无关，**故意不动它**。

第一版只做三类：改色（recolor）/ 删除（delete）/ 合并（merge）。
其他意图（「把这行字改短点」之类的语义改写）仍然走原来的对话链路 ——
那需要模型理解，不是这里能可靠做到的，硬猜不如不猜。

怎么指认卡片（这是这一版真正的难点）
------------------------------------
  ① 明说卡号：`cv0` / `cv3`（卡片角上就印着这个 id）
  ② 序号：`第 2 张` / `第三块`（人话最常用，1 起数）
  ③ 位置：`前两张` / `后一块` / `最后一张`
  ④ 全部：`全部` / `所有` / `都`
  ⑤ 引号里的原文：`把「鼓浪屿」那张删掉`（拿原片段去卡片正文里找）
  ⑥ 圈选兜底：前端把当前选中的 card-id 用 selected 传进来
指不到就返回错误（不猜），前端原样提示给用户。
"""
import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.parse
import zipfile

logger = logging.getLogger(__name__)

# 口语里的颜色 → 卡片强调色（取一眼能分清的色相，不是精确设计色）
COLORS = {
    "红": "#e5534b", "橙": "#e8883a", "黄": "#e0b93c", "绿": "#3fa66a",
    "青": "#35a8a0", "蓝": "#3b7dd8", "紫": "#8c5bd8", "粉": "#d86aa8",
    "灰": "#8a9099", "黑": "#2f3338", "白": "#eef1f5", "棕": "#8a6a4a",
    "褐": "#8a6a4a", "金": "#c9a227", "银": "#a9b1bb",
}

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 「第 2 张」「第三块」「第一个」—— 量词放宽一点，人说话不会那么规整
_RE_IDX = re.compile(r"第\s*([一二三四五六七八九十两\d]{1,3})\s*(?:张|块|个|片|条|页|幅|截)")
_RE_HEAD = re.compile(r"前\s*([一二三四五六七八九十两\d]{1,3})\s*(?:张|块|个)")
_RE_TAIL = re.compile(r"后\s*([一二三四五六七八九十两\d]{1,3})\s*(?:张|块|个)")
_RE_LAST = re.compile(r"(最后|末尾|最末|尾部)\s*(?:那)?\s*(?:一)?\s*(?:张|块|个)")
_RE_ALL = re.compile(r"(全部|所有|全都|统统|整体|一并|每一张)")
_RE_CARD_ID = re.compile(r"cv\s*(\d+)", re.I)
_RE_THIS = re.compile(r"(这两张|这两个|这俩|这几张|这几块)")
_RE_QUOTED = re.compile(r"[「『“\"']([^」』”\"']{1,40})[」』”\"']")

# 「合成」也算合并（用户常说「把第 1 张和第 3 张合成一张」）
_RE_MERGE = re.compile(r"(合并|并成|并到|合到|合起来|合一起|合成|拼成|拼一|拼合|并一张)")
_RE_DELETE = re.compile(r"(删掉|删了|删去|删除|去掉|拿掉|移除|不要这|干掉|清掉|剔除)")
# 「改/换/变/涂/标/调/染/刷」都算上色意图，但**必须带上「色」**。
# 只写单字会误命中：「标」→「标题」「标注」、「调」→「调整」、「变」→「变成」、
# 「换」→「换行」。误判代价很实：用户想说「标题再短点」，却被当成改色指令
# 拦下来、连对话链路都走不了（2026-11-18 实测踩到）。
_RE_RECOLOR = re.compile(
    r"((改|换|变|涂|标|调|染|刷)(成|为)?\s*(个|一下|别的|其他)?\s*(颜色|色)|上色|配色)")
# 「去掉背景 / 删掉水印」是**图片处理**，不是「删掉这张卡」——
# 一句话就把卡片删了，用户得重新生成才有，这个代价不能赌
_RE_IMG_EDIT = re.compile(r"(背景|水印|边框|阴影|留白|空白|logo|LOGO|Logo)")


def _num(s):
    """中文数字 / 阿拉伯数字 → int（认 '十'、'十二'、'二十'、'两'）。"""
    s = str(s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_NUM.get(s[1:2], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return _CN_NUM.get(a, 0) * 10 + (_CN_NUM.get(b[:1], 0) if b else 0)
    return _CN_NUM.get(s, 0)


def _ids(cards):
    return [str((c or {}).get("id") or "") for c in (cards or [])]


def find_color(text):
    """从一句话里认出颜色，返回 #RRGGBB；认不出返回空串。"""
    t = str(text or "")
    m = re.search(r"#([0-9a-fA-F]{6})", t)
    if m:
        return "#" + m.group(1).lower()
    # 长词优先，免得「红」抢在「粉红」前面
    for word in sorted(COLORS, key=len, reverse=True):
        if word in t:
            return COLORS[word]
    return ""


def detect_action(text):
    """认出这句话想干什么：merge / delete / recolor / ''（认不出）。"""
    t = str(text or "")
    # 顺序有讲究：合并排第一 —— 「把两张并成一张红色的」该是合并，不是改色
    if _RE_MERGE.search(t):
        return "merge"
    if _RE_DELETE.search(t):
        # 「把这图的背景去掉」是图片处理，不是删卡片 —— 交回对话链路（那边有抠图工具）
        if _RE_IMG_EDIT.search(t):
            return ""
        return "delete"
    if find_color(t):
        return "recolor"
    if _RE_RECOLOR.search(t):
        # 说了「改颜色」却没说成什么色 —— 交给上层报错（别瞎猜成红色）
        return "recolor"
    return ""
def resolve_targets(text, cards, selected=""):
    """这句话指的是哪几张卡片。返回 (ids, error)。

    ⚠️ 指不到就返回 error，**绝不默认「全部」** —— 猜错会把用户的画布改坏，
    比什么都不做更糟（这是本功能唯一不能容忍的失败方式）。
    """
    t = str(text or "")
    all_ids = [i for i in _ids(cards) if i]
    if not all_ids:
        return [], "画布上还没有卡片"

    picked = []

    def _add(cid):
        if cid and cid not in picked:
            picked.append(cid)

    # ① 明说卡号：cv0 / cv3。说了不存在的号 → 直接报错（不静默跳过）
    for m in _RE_CARD_ID.finditer(t):
        cid = "cv%d" % int(m.group(1))
        if cid not in all_ids:
            return [], "画布上没有这张卡片：%s（现有 %s）" % (
                cid, "、".join(all_ids[:8]))
        _add(cid)

    # ② 序号：「第 2 张」「第三块」，1 起数
    for m in _RE_IDX.finditer(t):
        n = _num(m.group(1))
        if 1 <= n <= len(all_ids):
            _add(all_ids[n - 1])
        else:
            return [], "画布上只有 %d 张卡片，没有第 %d 张" % (len(all_ids), n)

    # ③ 「前两张」/「后一块」
    m = _RE_HEAD.search(t)
    if m:
        n = _num(m.group(1))
        if n >= 1:
            for cid in all_ids[:n]:
                _add(cid)
    m = _RE_TAIL.search(t)
    if m:
        n = _num(m.group(1))
        if n >= 1:
            for cid in all_ids[-n:]:
                _add(cid)

    # ④ 「最后一张」
    if _RE_LAST.search(t):
        _add(all_ids[-1])

    # ⑤ 「全部」—— 只在用户真的这么说时才全选
    if _RE_ALL.search(t):
        for cid in all_ids:
            _add(cid)

    # ⑥ 「这两张」（画布上正好两张时才认，多了就请用户说清）
    if _RE_THIS.search(t) and len(all_ids) == 2:
        for cid in all_ids:
            _add(cid)

    # ⑦ 引号里的原文：「把『鼓浪屿』那张删掉」
    if not picked:
        for q in _RE_QUOTED.findall(t):
            q = str(q or "").strip()
            if not q:
                continue
            for c in (cards or []):
                body = str((c or {}).get("content") or "")
                if q in body:
                    _add(str((c or {}).get("id") or ""))

    # ⑧ 圈选兜底：前端把当前选中的那张 card-id 传进来
    if not picked and selected and str(selected) in all_ids:
        _add(str(selected))

    if not picked:
        return [], ("没听出是哪张卡片 —— 可以说「第 2 张」「cv1」「最后一张」，"
                    "或先点一下那张卡再点 🎤")
    return picked, ""


def parse_command(text, cards, selected=""):
    """一句话 → 一条指令。**永不抛异常**，认不出就 ok=False + 带 error。"""
    try:
        cards = list(cards or [])
        t = str(text or "").strip()
        if not t:
            return {"ok": False, "action": "", "card_ids": [], "color": "", "error":
                    "没说改什么 —— 试着说「第 2 张改成红色」「删掉最后一张」「合并前两张」"}
        action = detect_action(t)
        if not action:
            return {"ok": False, "action": "", "card_ids": [], "color": "",
                    "error": "这句没认出是改色 / 删除 / 合并 —— 换个说法再说一次"}
        ids, err = resolve_targets(t, cards, selected=selected)
        if err:
            return {"ok": False, "action": action, "card_ids": [], "color": "",
                    "error": err}
        color = ""
        if action == "recolor":
            color = find_color(t)
            if not color:
                return {"ok": False, "action": action, "card_ids": ids, "color": "",
                        "error": "没说改成什么颜色（红/橙/黄/绿/青/蓝/紫/粉/灰/黑/白都认）"}
        if action == "merge" and len(ids) < 2:
            return {"ok": False, "action": action, "card_ids": ids, "color": "",
                    "error": "合并至少要两张 —— 说「合并前两张」或「合并第 1 张和第 2 张」"}
        return {"ok": True, "action": action, "card_ids": ids, "color": color,
                "error": "", "text": t}
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "action": "", "card_ids": [], "color": "",
                "error": "解析失败：%s: %s" % (type(exc).__name__, exc)}


def _all_table(cards):
    return bool(cards) and all(str((c or {}).get("type") or "") == "table" for c in cards)


def apply_card_op(card, op):
    """对**一张**卡片做一次操作，返回新卡片（不改入参）。

    op: {"action": "set_color", "color": "#RRGGBB"} / {"action": "delete"}（→ None）
    单独抽出来是因为「改色」在批量路径里也要用到同一种口径。
    """
    src = dict(card or {})
    act = str((op or {}).get("action") or "")
    if act == "delete":
        return None
    if act == "set_color":
        src["color"] = str((op or {}).get("color") or "")
        return src
    raise ValueError("不认识的操作：%s" % act)


def apply_command(cards, cmd):
    """把 parse_command 的结果落到卡片数组上。返回 {ok, cards, action, card_ids, ...}。

    · 入参**不被就地修改**（前端拿返回值整体重画，改入参会让撤销/重放变难）；
    · 卡片 id 操作后保持不变（改色/合并都保留原 id），这样前端 DOM 上的
      data-card-id 一直对得上，下一句话还能继续指着同一张卡改。
    """
    cards = [dict(c or {}) for c in (cards or [])]
    cmd = cmd or {}
    if not cmd.get("ok"):
        return {"ok": False, "action": str(cmd.get("action") or ""), "card_ids": [],
                "cards": cards, "error": str(cmd.get("error") or "指令无效")}
    action = str(cmd.get("action") or "")
    ids = [str(x) for x in (cmd.get("card_ids") or [])]
    exist = [str(c.get("id") or "") for c in cards]
    missing = [i for i in ids if i not in exist]
    if missing:
        return {"ok": False, "action": action, "card_ids": ids, "cards": cards,
                "error": "画布上没有这张卡片：%s" % "、".join(missing)}
    if not ids:
        return {"ok": False, "action": action, "card_ids": [], "cards": cards,
                "error": "没指定是哪张卡片"}

    if action == "recolor":
        color = str(cmd.get("color") or "")
        for i, c in enumerate(cards):
            if str(c.get("id") or "") in ids:
                cards[i] = apply_card_op(c, {"action": "set_color", "color": color})
        return {"ok": True, "action": action, "card_ids": ids, "cards": cards,
                "color": color, "error": "",
                "note": "已把 %d 张卡片改成 %s" % (len(ids), color)}

    if action == "delete":
        keep = [c for c in cards if str(c.get("id") or "") not in ids]
        dropped = [str(c.get("id") or "") for c in cards
                   if str(c.get("id") or "") in ids]
        return {"ok": True, "action": action, "card_ids": dropped, "cards": keep,
                "error": "", "note": "已删掉 %d 张：%s" % (
                    len(dropped), "、".join(dropped))}

    if action == "merge":
        if len(ids) < 2:
            return {"ok": False, "action": action, "card_ids": ids, "cards": cards,
                    "error": "合并至少要两张"}
        # 按画布原顺序合并（用户看到的顺序，不是他念的顺序）
        chosen = [c for c in cards if str(c.get("id") or "") in ids]
        chosen_ids = [str(c.get("id") or "") for c in chosen]
        body = "\n".join(str(c.get("content") or "") for c in chosen
                         if str(c.get("content") or "").strip())
        merged = {
            "id": chosen_ids[0],
            "type": "table" if _all_table(chosen) else "text",
            "content": body,
            "color": str(chosen[0].get("color") or ""),
            "merged_from": chosen_ids,
        }
        out, placed = [], False
        for c in cards:
            cid = str(c.get("id") or "")
            if cid not in chosen_ids:
                out.append(c)
            elif not placed:
                out.append(merged)          # 合并结果占最前面那张的位置
                placed = True
        return {"ok": True, "action": action, "card_ids": chosen_ids, "cards": out,
                "error": "", "note": "已把 %d 张合并成一张（%s）" % (
                    len(chosen_ids), "、".join(chosen_ids))}

    return {"ok": False, "action": action, "card_ids": ids, "cards": cards,
            "error": "不认识的操作：%s（第一版只支持 改色 / 删除 / 合并）" % action}


def handle(text, cards, selected=""):
    """解析 + 执行，一步到位（给 /api/canvas/command 用）。永不抛异常。"""
    cards = [dict(c or {}) for c in (cards or [])]
    cmd = parse_command(text, cards, selected=selected)
    out = apply_command(cards, cmd)
    out["cmd"] = {k: cmd.get(k) for k in ("action", "card_ids", "color", "ok")}
    out["text"] = str(text or "")
    return out


# ============================================================================
# 常驻工作台：产出物堆在桌面上，回来接着干（2026-11-21）
# ----------------------------------------------------------------------------
# 摩擦点：图片 / PPT / Excel / 视频 / 数字人视频做出来以后**只活在对话流里**，
# 关掉页面就再也找不回来 —— 用户只能重新让模型做一遍（最贵的一种浪费）。
#
# 分工（和上面的语音画布一样，刻意分层）
#   · 本模块：**纯逻辑 + 一份 JSON 落盘**。条目口径、去重、排序、打包成 zip
#     全在这里；不碰 DOM、不联网、不调模型 —— 所以能秒级断言。
#   · frontend/artifacts.js：localStorage 那份镜像 + 面板渲染 + 拖拽排序。
#     两边**双写**：后端没起来时前端照样能用，起来以后再对齐（见 sync）。
#   · frontend/app.js：工具产出（图 / PPT / 文档 / 视频）到手时调 add。
#
# 条目口径（前后端共用，字段少而稳）
#   {id, kind, title, thumb, path, ts, status, tool, note}
#   id   —— 由 path+title 推出的稳定短哈希：同一个文件重复添加不会堆成两行
#   kind —— image / ppt / doc / sheet / video / audio / web / other
#   path —— 源文件路径（相对项目根，或站内下载口 /api/doclib/download?rel=）
#   ts   —— 第一次被添加的时间（重复添加不刷新：用户要看的是"什么时候做出来的"）
#   status —— done（默认）；别的值表示"还没做完"，供"接着上次干"用
# ============================================================================
WORKBENCH_KINDS = ("image", "ppt", "doc", "sheet", "video", "audio", "web", "other")

# 一次最多留多少条：再多就不是"桌面"而是一面墙（前端同口径，见 artifacts.js）
MAX_ARTIFACTS = 240

STORE_NAME = "canvas_artifacts.json"

# 产物扩展名 → 种类（前端 artifacts.js 的 WB_EXT_KIND 与此保持一致）
_ART_EXT_KIND = {
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image",
    "gif": "image", "bmp": "image", "svg": "image",
    "pptx": "ppt", "ppt": "ppt", "key": "ppt",
    "docx": "doc", "doc": "doc", "pdf": "doc", "md": "doc", "txt": "doc",
    "xlsx": "sheet", "xls": "sheet", "csv": "sheet",
    "mp4": "video", "mov": "video", "webm": "video", "mkv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio",
    "html": "web", "htm": "web", "url": "web",
}


def store_path():
    """工作台条目的落盘位置（data/canvas_artifacts.json）。

    ⚠️ **必须延迟解析**：config.data() 会顺手建目录，模块导入时不该有副作用
      （自检会导入全部模块，见 regress 的 imports 用例）。
    """
    from . import config
    return config.data(STORE_NAME)


def guess_kind(path="", hint=""):
    """按显式类型 / 扩展名判断种类。认不出返回 other（不瞎猜成图片）。"""
    k = str(hint or "").strip().lower()
    if k in WORKBENCH_KINDS:
        return k
    p = str(path or "").strip().split("?")[0]
    ext = os.path.splitext(p)[1].lower().lstrip(".")
    return _ART_EXT_KIND.get(ext, "other")


def guess_thumb(path="", kind="", thumb=""):
    """缩略图地址。

    ⚠️ 两条必须守住：
      ① `data:image/png;base64,…` 动辄几百 KB，**绝不进 localStorage**
         （5MB 配额，十几张就写满，之后整个工作台都存不进去）；
      ② 只给图片生成站内下载口，其它种类让前端用图标。
    """
    t = str(thumb or "").strip()
    if t.lower().startswith("data:"):
        t = ""
    if t:
        return t
    p = str(path or "").strip()
    if not p or guess_kind(p, kind) != "image":
        return ""
    if p.startswith(("http://", "https://", "/api/")):
        return p
    return "/api/doclib/download?rel=" + urllib.parse.quote(p, safe="/")


def _art_id(path="", title=""):
    """稳定短 id：同一个文件被重新生成 / 历史重放，永远落在同一条上。"""
    raw = (str(path or "") + "\x01" + str(title or "")).encode("utf-8", "replace")
    return "a" + hashlib.md5(raw).hexdigest()[:10]


def normalize_artifact(raw):
    """一条产出物 → 统一口径。**永不抛异常**（前端传什么都不能把工作台搞崩）。"""
    src = dict(raw) if isinstance(raw, dict) else {}
    path = str(src.get("path") or src.get("rel") or src.get("file") or "").strip()
    title = " ".join(str(src.get("title") or src.get("name") or "").split())[:120]
    kind = guess_kind(path, src.get("kind") or src.get("type"))
    if not title:
        title = os.path.basename(path.split("?")[0]) or "产出物"
    item_id = str(src.get("id") or "").strip() or _art_id(path, title)
    try:
        ts = float(src.get("ts") or src.get("time") or 0) or time.time()
    except (TypeError, ValueError):
        ts = time.time()
    status = str(src.get("status") or "done").strip() or "done"
    return {
        "id": item_id,
        "kind": kind,
        "title": title,
        "thumb": guess_thumb(path, kind, src.get("thumb")),
        "path": path[:400],
        "ts": round(ts, 3),
        "status": status[:20],
        "tool": str(src.get("tool") or "")[:40],
        "note": " ".join(str(src.get("note") or "").split())[:200],
    }


def merge_artifact(items, raw):
    """把一条产出物并进列表：**已在列表里就搬到最前面、保留原时间**。

    返回 (新列表, 归一化后的条目)。入参不被就地修改。
    为什么重复添加要保留原 ts：用户看到的时间该是"这东西什么时候做出来的"，
    而不是"我最后一次刷新页面"。
    """
    one = normalize_artifact(raw)
    out = [dict(x or {}) for x in (items or [])]
    keep_ts = 0.0
    for old in out:
        if str(old.get("id") or "") == one["id"] and old.get("ts"):
            try:
                keep_ts = float(old["ts"])
            except (TypeError, ValueError):
                keep_ts = 0.0
            break
    if keep_ts:
        one["ts"] = round(keep_ts, 3)
    out = [x for x in out if str(x.get("id") or "") != one["id"]]
    out.insert(0, one)
    return out[:MAX_ARTIFACTS], one


def _load_store():
    """读盘。文件坏了 / 没权限 → 当空桌面（**不许**把整个工作台搞挂）。"""
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("silent-except", exc_info=True)
        return []
    raw = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [normalize_artifact(x) for x in raw if isinstance(x, dict)][:MAX_ARTIFACTS]


def _save_store(items):
    """写盘（原子替换：先写 .tmp 再 os.replace，免得写一半留下坏 JSON）。"""
    path = store_path()
    payload = {"version": 1, "saved_at": time.time(),
               "items": [dict(x or {}) for x in (items or [])][:MAX_ARTIFACTS]}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.debug("silent-except", exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            logger.debug("silent-except", exc_info=True)
        return False


def add_artifact(raw, items=None):
    """加一条产出物。items 传进来就**只算不存**（纯逻辑，方便断言）。

    返回 {ok, item, items, count, persisted, error}。
    既没路径也没标题 → ok=False（不往桌面上放一个点不开的空壳）。
    """
    src = dict(raw) if isinstance(raw, dict) else {}
    has_path = bool(str(src.get("path") or src.get("rel") or src.get("file") or "").strip())
    has_title = bool(str(src.get("title") or src.get("name") or "").strip())
    pure = items is not None
    base = [dict(x or {}) for x in (items or [])] if pure else _load_store()
    if not (has_path or has_title):
        return {"ok": False, "item": None, "items": base, "count": len(base),
                "persisted": False,
                "error": "这条产出物既没有文件路径也没有标题，加不进工作台"}
    merged, one = merge_artifact(base, src)
    persisted = False if pure else _save_store(merged)
    return {"ok": True, "item": one, "items": merged, "count": len(merged),
            "persisted": persisted, "error": ""}


def list_artifacts(items=None):
    """列出桌面（新的在前）。附带「没做完的」一栏，给"接着上次干"用。"""
    pure = items is not None
    out = [normalize_artifact(x) for x in (items or [])] if pure else _load_store()
    out = out[:MAX_ARTIFACTS]
    pending = [x for x in out if str(x.get("status") or "done") != "done"]
    return {"ok": True, "items": out, "count": len(out),
            "unfinished": pending[:8], "error": ""}


def remove_artifact(ids, items=None):
    """删掉若干条。ids 可以是单个 id 或数组。返回 {ok, removed, items, count}。"""
    if isinstance(ids, str):
        want = [ids]
    elif isinstance(ids, (list, tuple)):
        want = [str(x) for x in ids]
    else:
        want = []
    want = [w.strip() for w in want if w.strip()]
    pure = items is not None
    base = [normalize_artifact(x) for x in (items or [])] if pure else _load_store()
    if not want:
        return {"ok": False, "removed": [], "items": base, "count": len(base),
                "error": "没指定要删哪一条"}
    dropped = [x for x in base if str(x.get("id") or "") in want]
    keep = [x for x in base if str(x.get("id") or "") not in want]
    if not dropped:
        return {"ok": False, "removed": [], "items": keep, "count": len(keep),
                "error": "工作台上没有 %s 这一条" % "、".join(want[:4])}
    if not pure:
        _save_store(keep)
    return {"ok": True, "removed": [str(x.get("id") or "") for x in dropped],
            "items": keep, "count": len(keep), "error": ""}


def reorder_artifacts(order, items=None):
    """按前端拖拽后的 id 顺序重排。

    ⚠️ 只重排、**不增删**：order 里没提到的条目按原相对顺序接在后面
      （前端只发它看得到的那几条，别因为漏发就把用户的产出物弄丢）。
    """
    if isinstance(order, str):
        order = [order]
    ids = [str(x) for x in (order or []) if str(x).strip()]
    pure = items is not None
    base = [normalize_artifact(x) for x in (items or [])] if pure else _load_store()
    by_id = {}
    for it in base:
        by_id.setdefault(str(it.get("id") or ""), it)
    out = []
    for i in ids:
        if i in by_id:
            out.append(by_id.pop(i))
    for it in base:                      # 没被点名的保持原顺序，接在后面
        i = str(it.get("id") or "")
        if i in by_id:
            out.append(by_id.pop(i))
    if not pure:
        _save_store(out)
    return {"ok": True, "items": out, "count": len(out), "error": ""}


def safe_abs(path=""):
    """条目里的路径 → 本机真实文件（找不到 / 越界一律返回空串）。

    白名单口径与 selfdev 一致：只认项目根和数据根（打包时 data_root 可能是
    别的目录），并且必须落在其内部 —— 导出打包绝不能把 ../../ 之类的东西打进去。
    """
    from . import config
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith("/api/") and "?" in p:
        q = urllib.parse.urlparse(p).query
        p = (urllib.parse.parse_qs(q).get("rel") or [""])[0]
    if not p:
        return ""
    roots = []
    for r in (config.PROJECT_ROOT, config.data_root()):
        try:
            roots.append(os.path.abspath(r))
        except OSError:
            logger.debug("silent-except", exc_info=True)
    cands = [p] if os.path.isabs(p) else [os.path.join(r, p) for r in roots]
    for c in cands:
        try:
            ap = os.path.abspath(c)
        except OSError:
            continue
        inside = any(ap == r or ap.startswith(r + os.sep) for r in roots)
        if not inside:
            continue
        if os.path.isfile(ap):
            return ap
    return ""


def _manifest_md(items, packed, missing):
    """包里那份「清单.md」：用户打开 zip 第一眼就知道这些都是什么。"""
    lines = ["# 工作台产出物", "",
             "- 导出时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "- 条目：%d（打进包 %d 个文件）" % (len(items), len(packed)), ""]
    for it in items:
        name = packed.get(str(it.get("id") or ""), "")
        stamp = ""
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(it.get("ts") or 0)))
        except (TypeError, ValueError, OSError):
            stamp = ""
        lines.append("## %s" % (it.get("title") or it.get("id")))
        lines.append("- 种类：%s%s" % (it.get("kind") or "other",
                                       ("（%s）" % it.get("tool")) if it.get("tool") else ""))
        if stamp:
            lines.append("- 时间：%s" % stamp)
        if it.get("path"):
            lines.append("- 源文件：%s" % it.get("path"))
        lines.append("- 包内文件：%s" % (name or "（源文件不在本机了，只留记录）"))
        if it.get("note"):
            lines.append("- 备注：%s" % it.get("note"))
        lines.append("")
    if missing:
        lines.append("## 找不到源文件的条目")
        for m in missing:
            lines.append("- %s" % m)
        lines.append("")
    return "\n".join(lines)


def export_artifacts(items=None):
    """把桌面上的产出物打成一个 zip（在内存里打，不写临时文件）。

    返回 {ok, name, blob, count, missing, error}；条目一个文件都找不到时
    仍然给一个只含「清单.md」的包（ok=True + missing 列表），别让用户点了没反应。
    """
    pure = items is not None
    base = [normalize_artifact(x) for x in (items or [])] if pure else _load_store()
    buf = io.BytesIO()
    packed, missing, used = {}, [], set()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for it in base:
                ap = safe_abs(it.get("path"))
                if not ap:
                    missing.append(it.get("title") or it.get("id"))
                    continue
                name = os.path.basename(ap) or ("file_%d" % len(packed))
                base_name, ext = os.path.splitext(name)
                k = 2
                while name in used:
                    name = "%s_%d%s" % (base_name, k, ext)
                    k += 1
                try:
                    z.write(ap, name)
                except OSError:
                    logger.debug("silent-except", exc_info=True)
                    missing.append(it.get("title") or it.get("id"))
                    continue
                used.add(name)
                packed[str(it.get("id") or "")] = name
            z.writestr("清单.md", _manifest_md(base, packed, missing))
            z.writestr("manifest.json", json.dumps(
                {"exported_at": time.time(), "count": len(packed),
                 "items": base, "packed": packed, "missing": missing},
                ensure_ascii=False, indent=1))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:      # noqa: BLE001
        return {"ok": False, "name": "", "blob": b"", "count": 0, "missing": [],
                "error": "打包失败：%s: %s" % (type(exc).__name__, exc)}
    blob = buf.getvalue()
    return {"ok": True, "name": "工作台产出物_%s.zip" % time.strftime("%Y%m%d_%H%M"),
            "blob": blob, "count": len(packed), "missing": missing,
            "error": "" if packed or not base else "工作台上还没有产出物"}

