# -*- coding: utf-8 -*-
"""回复内嵌实时卡片：把工具结果压成 {type,title,thumb,payload} 元数据。

为什么要有这个东西
------------------
用户的原话是「现在回答是一整面文字墙，地图、天气、生成的图、文档缩略图
都要我自己点开面板去找」。产出的东西其实**都已经有了**，只是散落在
正文里、折叠块里、侧边面板里。

这里做的是**纯函数层**：拿「工具名 + 参数 + 返回值 + 该工具发出的 ui 事件」，
算出这一轮该在回复气泡里内嵌哪几张卡片。不碰 IO、不碰 DOM、不知道 session，
所以可以直接单元测试，也可以被本地 / 云端两条通道共用。

卡片结构（前端 frontend/livecard.js 的 renderLiveCard 吃这个）::

    {"type": "map"|"weather"|"image"|"doc"|"ppt"|"video",
     "title": "一句话标题（卡片上显示的字）",
     "thumb": "缩略图地址（没有就空串）",
     "payload": {...给前端点开/放大用的原始信息...}}

⚠️ 只放**元数据**，不放图片 base64。
  图片本身已经通过 ui image 事件发给前端了（那条流里本来就有 b64），
  再在卡片里带一份等于把同一条消息的数据量翻倍。
"""
import json
import os
import re

# 前端认这几种卡；多出来的类型会被忽略（前端只按 class 上色，不会崩）
CARD_TYPES = ("map", "weather", "image", "doc", "ppt", "video")

# 一轮最多几张卡：再多就不是"信息流"而是另一面墙了
MAX_CARDS = 12

# 产物扩展名 → 卡片类型
_EXT_KIND = {
    ".pptx": "ppt", ".ppt": "ppt",
    ".docx": "doc", ".doc": "doc", ".md": "doc", ".txt": "doc",
    ".xlsx": "doc", ".xls": "doc", ".csv": "doc", ".pdf": "doc",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
}

# 这些工具产出的东西值得做成卡片
_IMAGE_TOOLS = {
    "generate_image", "edit_image", "cutout_image", "compose_images",
    "composite_image", "web_image_search", "save_image_to_library",
}
_DOC_TOOLS = {"make_docx", "make_xlsx", "library", "document", "office_edit", "edit_office"}
_PPT_TOOLS = {"make_pptx"}
_VIDEO_TOOLS = {"video_edit", "avatar_speak", "make_video"}
_MAP_TOOLS = {"map_plan", "nearby_places", "route_plan", "map_route"}
_WEATHER_TOOLS = {"get_weather", "weather"}

# 从工具返回的文本里捞文件路径：本项目的工具都会把落盘路径写进返回值
_PATH_RE = re.compile(
    r"[^\s\"'<>（）()，。；、]{1,200}?\.(?:pptx?|docx?|xlsx?|csv|pdf|md|txt|mp4|mov|webm|png|jpe?g|webp)\b",
    re.I,
)


def _s(v, n=80):
    """单行化 + 截断（卡片标题必须能塞进一行，换行会把卡片撑高）。"""
    t = " ".join(str(v if v is not None else "").split())
    return t[:n]


def _mk(kind, title, thumb="", **payload):
    return {
        "type": str(kind),
        "title": _s(title) or str(kind),
        "thumb": str(thumb or ""),
        "payload": payload,
    }


def _path_of(text):
    """工具返回文本里第一个像产物路径的字符串（没有就空串）。"""
    if not isinstance(text, str) or not text:
        return ""
    for m in _PATH_RE.finditer(text):
        p = m.group(0).strip().strip("「」『』\"'“”")
        # ★ 2026-11-24 修：工具返回值常写成「已生成文档：D:\out\报告.docx」，
        #   而 _PATH_RE 不排除中文冒号 → 整句被当成路径，前端拿它打开必然失败。
        #   这里只保留**路径本身**：有盘符就从盘符起，否则从最后一个冒号/逗号后起。
        mm = re.search(r"[A-Za-z]:[\\/]", p)
        if mm:
            p = p[mm.start():]
        else:
            for sep in ("：", "，", ","):
                if sep in p:
                    p = p.rsplit(sep, 1)[-1]
        p = p.strip()
        # 太短的（"a.md"）大概率是举例，不是真产物
        if len(p) >= 5:
            return p
    return ""


def _kind_of_path(path):
    ext = os.path.splitext(str(path or ""))[1].lower()
    return _EXT_KIND.get(ext, "")


# ---------------------------------------------------------------- ui 事件
def cards_from_ui(events):
    """把工具发出的 ui 事件翻译成卡片（地图 / 图片 / 文库…）。

    这些事件本来就是发给前端的（`{"ui": {...}}`），这里只是**再取一份元数据**
    做内嵌卡片，不改变事件本身的去向。
    """
    out = []
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "")
        if t == "map":
            out.extend(_map_cards(e))
        elif t == "image":
            out.append(_image_card(e))
        elif t == "library":
            rel = e.get("rel") or e.get("path") or ""
            if rel:
                kind = _kind_of_path(rel) or "doc"
                if kind == "image":
                    kind = "doc"
                out.append(_mk(kind, os.path.basename(str(rel)) or str(rel),
                               "", path=str(rel), act=e.get("act") or "write"))
        elif t == "video":
            p = e.get("path") or e.get("rel") or ""
            if p:
                out.append(_mk("video", os.path.basename(str(p)) or _s(p, 40), "", path=str(p)))
    return [c for c in out if c]


def _map_cards(e):
    markers = e.get("markers") or []
    names = [str(m.get("name") or "").strip() for m in markers if isinstance(m, dict)]
    names = [n for n in names if n]
    route = e.get("route") or {}
    if route.get("from") or route.get("to"):
        title = "%s → %s" % (route.get("from") or "出发地", route.get("to") or "目的地")
    elif names:
        title = "地图：" + (names[0] if len(names) == 1 else "%s 等 %d 个地点" % (names[0], len(names)))
    else:
        title = "地图"
    payload = {
        "center": e.get("center") or [],
        "zoom": e.get("zoom") or 12,
        "markers": [{"name": n,
                     "lat": (markers[i] or {}).get("lat"),
                     "lon": (markers[i] or {}).get("lon")} for i, n in enumerate(names)][:8],
        "tile_source": e.get("tile_source") or "",
        "online": bool(e.get("online")),
    }
    if route:
        payload["route"] = {
            "from": route.get("from") or "",
            "to": route.get("to") or "",
            "mode": route.get("mode") or "",
            "distance": route.get("distance") or "",
            "duration": route.get("duration") or "",
            "points": len(route.get("points") or []),
        }
    nearby = e.get("nearby") or {}
    if nearby:
        payload["nearby"] = {
            "category": nearby.get("category") or "",
            "center_name": nearby.get("center_name") or "",
            "total": nearby.get("total") or len(nearby.get("items") or []),
            "items": [str(x.get("name") or "") for x in (nearby.get("items") or [])[:6]
                      if isinstance(x, dict)],
        }
        if not names and nearby.get("center_name"):
            title = "附近%s：%s" % (nearby.get("category") or "地点", nearby.get("center_name"))
    return [_mk("map", title, "", **payload)]


def _image_card(e):
    saved = e.get("saved") or {}
    saved_path = ""
    if isinstance(saved, dict):
        saved_path = str(saved.get("path") or "")
    elif isinstance(saved, str):
        saved_path = saved
    prompt = _s(e.get("prompt") or e.get("query") or "", 40)
    origin = str(e.get("origin") or "")
    badge = {"gen": "🎨", "edit": "🖼", "web": "🌐"}.get(origin, "🖼")
    title = (badge + " " + prompt).strip() if prompt else badge + " 图片"
    return _mk("image", title, "", origin=origin, mime=e.get("mime") or "",
               model=e.get("model") or "", size=e.get("size") or "",
               saved_path=saved_path,
               source=str(e.get("source") or e.get("url") or "")[:300])


# ---------------------------------------------------------------- 工具结果
def _weather_card(args, result):
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if "天气查询失败" in text:
        return []
    city = _s((args or {}).get("city") or "", 20)
    m = re.search(r"【(.+?)\s*天气】", text)
    if m:
        city = city or _s(m.group(1), 20)
    detail = ""
    m2 = re.search(r"此刻实况[^：:]*[：:]([^\n]+)", text)
    if m2:
        detail = _s(m2.group(1), 60)
    if not detail:
        m3 = re.search(r"逐日预报[：:]\s*\n\s*([^\n]+)", text)
        if m3:
            detail = _s(m3.group(1), 60)
    coords = []
    mc = re.search(r"坐标\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text)
    if mc:
        coords = [float(mc.group(1)), float(mc.group(2))]
    title = (city or "天气") + ("：" + detail if detail else "")
    return [_mk("weather", title, "", city=city, detail=detail, center=coords,
                days=(args or {}).get("days") or 0)]


def _artifact_card(name, args, result):
    """make_pptx / make_docx / library …：按扩展名定类型，标题用文件名。"""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    path = _path_of(text)
    kind = _kind_of_path(path)
    if name in _PPT_TOOLS:
        kind = "ppt"
    elif name in _VIDEO_TOOLS:
        kind = "video"
    elif name in _IMAGE_TOOLS:
        # 图片工具必须出 image 卡：原来这里把 image 和"推断不出"一起压成 doc，
        # 导致 generate_image / web_image_search 的产出被当前端当文档渲染（没有缩略图）。
        kind = "image"
    elif kind not in ("image", "ppt", "video", "doc"):
        kind = "doc"
    title_bits = []
    if kind == "ppt":
        title_bits.append("📊")
    elif kind == "video":
        title_bits.append("🎬")
    elif kind == "image":
        title_bits.append("🖼")
    else:
        title_bits.append("📄")
    title = os.path.basename(path) if path else _s(
        str((args or {}).get("title") or (args or {}).get("name") or name), 40)
    if not os.path.splitext(title)[1]:
        title += {"ppt": ".pptx", "video": ".mp4", "image": ".png"}.get(kind, ".docx")
    return [_mk(kind, " ".join(title_bits) + " " + title, "",
                path=path, tool=name,
                title_arg=_s((args or {}).get("title") or "", 60))]


def cards_for_tool(name, args=None, result=None, events=None):
    """工具跑完后该出哪几张卡（0 张是常态）。"""
    name = str(name or "")
    out = list(cards_from_ui(events))
    if name in _WEATHER_TOOLS:
        out.extend(_weather_card(args, result))
    elif name in _MAP_TOOLS:
        # 地图卡优先来自 ui 事件（那里有坐标）；没有事件时至少给一张"查过了"的卡
        if not any(c.get("type") == "map" for c in out):
            city = _s((args or {}).get("city") or (args or {}).get("near") or
                      (args or {}).get("to") or "", 30)
            out.append(_mk("map", "地图：" + (city or name), "",
                           center=[], markers=[], args=_slim_args(args)))
    elif name in _IMAGE_TOOLS or name in _DOC_TOOLS or name in _PPT_TOOLS or name in _VIDEO_TOOLS:
        if not any(c.get("type") in ("image", "doc", "ppt", "video") for c in out):
            out.extend(_artifact_card(name, args, result))
        else:
            # 有 ui 事件但事件里没路径时，把返回值里捞到的路径补进 payload
            p = _path_of(result if isinstance(result, str) else "")
            if p:
                for c in out:
                    if c.get("type") in ("image", "doc", "ppt", "video") and not c["payload"].get("path"):
                        c["payload"]["path"] = p
    return [c for c in out if c and c.get("type") in CARD_TYPES]


def _as_kind(name):
    """`name` 本身就是卡片类型名（weather/map/doc/image/ppt/video）时返回它。"""
    k = str(name or "").strip().lower()
    return k if k in CARD_TYPES else ""


# 各类卡的默认标题（payload 里什么都读不出来时用）
_KIND_LABEL = {
    "weather": "天气", "map": "地图", "image": "图片",
    "doc": "文档", "ppt": "演示", "video": "视频",
}


def _payload_title(kind, payload):
    """从 payload 里挑一句话当卡片标题（按类型的字段优先级挑，读不到就给默认名）。"""
    p = payload if isinstance(payload, dict) else {}

    def first(*keys):
        for k in keys:
            v = p.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return _s(v, 60)
        return ""

    if kind == "weather":
        bits = [first("city", "place", "location"), first("detail", "desc", "text"),
                first("temp", "temperature", "now")]
        head = " ".join([b for b in bits if b]).strip()
        return head or _KIND_LABEL[kind]
    if kind == "map":
        frm, to = first("from", "origin"), first("to", "dest", "destination")
        if frm or to:
            return (frm + " → " + to).strip(" →") or _KIND_LABEL[kind]
        names = p.get("markers")
        if isinstance(names, list) and names and isinstance(names[0], dict):
            n0 = _s(names[0].get("name") or "", 40)
            if n0:
                return "地图：" + (n0 if len(names) == 1 else "%s 等 %d 处" % (n0, len(names)))
        return _KIND_LABEL[kind]
    name = first("title", "name", "filename", "file", "path")
    if name:
        base = os.path.basename(name)
        return base or name
    return _KIND_LABEL[kind]


def _thumb_of(payload):
    """payload 里的缩略图地址（只取短字符串，绝不把 base64/大图塞进卡片）。"""
    if not isinstance(payload, dict):
        return ""
    for k in ("thumb", "thumbnail", "src", "url", "map_url", "static_map"):
        v = payload.get(k)
        if isinstance(v, str) and v and len(v) <= 300 and not v.startswith("data:"):
            return v
    return ""


def build(name, args=None, result=None, events=None):
    """工具跑完后该在回复末尾挂的**一张主卡**：``{type,title,thumb,payload}``。

    为什么要有这一层（而不是调用方直接吃 cards_for_tool）
    ----------------------------------------------------
    调用方（main.py 的收尾逻辑 / 前端 app.js 的挂卡）只想知道「这一轮该画哪张卡」，
    不想关心「这个工具有没有发 ui 事件」「有没有返回产物路径」这些细节。
    这里把 ``cards_for_tool`` 的列表压成一张**最该露脸**的卡。

    两种调用方式都支持（都返回 dict，没有可画的卡时返回 None）::

        build("get_weather", {"city": "杭州"}, "【杭州 天气】\\n此刻实况：晴 25℃")
        build("weather", {"city": "杭州", "days": [...]})   # 已按类型分好类时

    第二种是「按卡片类型给原始 payload」：payload **原样保留**（前端
    render 的 renderTempBar / renderRouteThumb / renderDocPreview /
    renderImageCanvas 直接吃它），所以 days / markers / pages 这些数组不会丢。
    """
    kind = _as_kind(name)
    if kind and result is None and events is None and (args is None or isinstance(args, dict)):
        payload = dict(args) if isinstance(args, dict) else {}
        return {
            "type": kind,
            "title": _payload_title(kind, payload) or _KIND_LABEL.get(kind, kind),
            "thumb": _thumb_of(payload),
            "payload": payload,
        }
    try:
        cards = cards_for_tool(name, args, result, events)
    except Exception:                                   # noqa: BLE001
        return None
    if not cards:
        return None
    # 有 ui 事件时会先出 map / image / doc 卡；主卡就是列表里的第一张
    # （顺序在 cards_for_tool 里已经是「事件优先、工具兜底」）。
    card = dict(cards[0])
    card.setdefault("title", "")
    card.setdefault("thumb", "")
    if not isinstance(card.get("payload"), dict):
        card["payload"] = {"value": card.get("payload")}
    return card


def build_all(name, args=None, result=None, events=None):
    """同 ``build``，但返回**所有**卡（一轮可能要挂多张：地图 + 图片 + 文档）。"""
    try:
        return cards_for_tool(name, args, result, events)
    except Exception:                                   # noqa: BLE001
        return []


# 兼容别名：早期调用方/断言用的是 build_card（同样是「按类型给 payload」的语义）。
build_card = build


def _slim_args(args):
    if not isinstance(args, dict):
        return {}
    keep = {}
    for k, v in list(args.items())[:6]:
        if isinstance(v, (str, int, float, bool)) or v is None:
            keep[str(k)] = _s(v, 40) if isinstance(v, str) else v
    return keep


def cards_from_tool_events(tool_events):
    """云端通道的兜底：那边只有 (name, ok, result) 三元组，没有 ui 事件。

    能用，但比本地通道少一点细节（没坐标就用卡片标题兜住）。
    """
    out = []
    for e in (tool_events or []):
        if isinstance(e, dict):
            name, res = e.get("name"), e.get("result")
        elif isinstance(e, (tuple, list)) and len(e) >= 3:
            name, res = e[0], e[2]
        else:
            continue
        try:
            out.extend(cards_for_tool(name, {}, res, None))
        except Exception:      # noqa: BLE001
            continue
    return merge_cards([], out)


# ---------------------------------------------------------------- 合并
def merge_cards(base, new):
    """并进同一个列表并去重（同类型 + 同标题只留一张），按出现顺序，最多 MAX_CARDS 张。"""
    out = list(base or [])
    seen = set()
    for c in out:
        if isinstance(c, dict):
            seen.add((c.get("type"), c.get("title")))
    for c in (new or []):
        if not isinstance(c, dict):
            continue
        if c.get("type") not in CARD_TYPES:
            continue
        key = (c.get("type"), c.get("title"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out[:MAX_CARDS]
