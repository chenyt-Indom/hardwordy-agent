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
import re

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

