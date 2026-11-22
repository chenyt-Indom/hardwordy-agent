# -*- coding: utf-8 -*-
"""地图能力：找地点（地理编码）、规划路线、周边场所、底图瓦片。

服务选型（都实测过）：
· **配了高德 key → 优先高德**（`backend/amap.py`）：有真实评分、实时路况、
  公交换乘、真实步行骑行路径，POI 覆盖也比 OSM 强一个量级。
· 没配 key / 高德失败时的免 key 兜底：
  · 地理编码 → **Photon**（`photon.komoot.io`）
    ⚠️ **千万别加 `&lang=zh`** —— 加上之后中文查询全部返回空（实测踩到，查了半天）。
  · 路线规划 → **OSRM**（`router.project-osrm.org`）
  · 周边 POI → **Overpass**（Photon 是"按名字搜"的，做不到按类别扫一片区域）
  · 瓦片 → OSM 系镜像（`tile.openstreetmap.org` 在国内**连不上**，换 `.fr` / `.de`）

坐标一律用 (lat, lon) 对外；高德那套要 GCJ-02，进出都在 amap.py 里换算。

------------------------------------------------------------
两种运行模式：跟着前端那个「联网」开关走
------------------------------------------------------------
· **联网模式**（web_enabled=True）：实时查，**查完不留任何东西**。
· **离线模式**（web_enabled=False）：**一个网络请求都不发**，只有
  内置常用地名表（backend/builtin_places.py：几百条常用城市/机场/车站/高校/景点）。

  所以离线时：内置表认得的地名搜得到；**路线只能给直线距离**
  （会明确标注"不是实际道路"，绝不假装是真路线）；**附近场所在离线时完全查不了**；
  **地图卡片不显示底图**。

⚠️ **地图不做任何本地持久化**（2026-09-18 起，用户明确要求去掉）。
   曾经有一套"联网查过的地名/路线/瓦片都存本地、断网重放"的缓存，问题是：
   缓存键要带数据源前缀，结果离线（tag 变成 off）就永远读不到自己写的东西，
   "断网重放"整个失效且很难发现；瓦片还要分两套坐标系目录 + TTL + 版本号，
   一处改错就表现为"地图整片空白"。而用户实际很少真的断网用地图。
   ⇒ 别再往这个模块里加缓存。要加的话先想清楚上面那两个坑。

⚠️ 别在离线分支里"顺手"发一个请求 —— 用户关掉开关就是不想联网。
   判断一律走 online()，不要在别处自己读 config，免得两处逻辑走岔。
"""
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import logging
logger = logging.getLogger(__name__)

_UA = "local-multimodal-assistant/1.0 (personal use)"
_TIMEOUT = 15

PHOTON = "https://photon.komoot.io/api/"
OSRM = "https://router.project-osrm.org/route/v1/%s/%s"
# 按顺序试，前面不通就用后面（实测 tile.openstreetmap.org 在这台机器上超时）
TILE_MIRRORS = [
    "https://a.tile.openstreetmap.fr/hot/%d/%d/%d.png",
    "https://tile.openstreetmap.de/%d/%d/%d.png",
    "https://a.tile.openstreetmap.org/%d/%d/%d.png",
]

# 高德底图（联网+配了 key 时的首选）。实测 0.1 秒一张，比 OSM 镜像快一个量级。
# style=7 是标准中文路网图（有中文路名、POI 标注）。
# ⚠️⚠️ 占位符顺序必须是 **z / x / y**（跟上面 TILE_MIRRORS 一致）——
#    取图那里是 `tpl % (z, x, y)` 统一喂进去的。写成 `&x=%d&y=%d&z=%d` 会静默错位：
#    实测表现是**地图整片空白**（高德对越界瓦片返回 179 字节的纯色图，不报错），
#    查了半天才发现是自己把参数喂反了。查询串的顺序本来无所谓，别手贱改。
# ⚠️⚠️ 高德瓦片是 **GCJ-02** 的，和 OSM 的 WGS-84 差 50~500 米 ——
#    所以两套底图的瓦片**绝不能共用缓存目录**，前端打点也必须跟着换坐标系
#    （见 tile_source / 前端 wgs2gcj）。混用会看到"点位整体偏出去一个街区"。
AMAP_TILE_MIRRORS = [
    "https://wprd01.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
    "https://wprd02.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
    "https://wprd03.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
]

MAX_ZOOM = 19


# ---------------------------------------------------------------- 运行模式
# 短缓存：离线时前端一次要拉几十张瓦片，每张都去读一遍配置文件太浪费。
# 2 秒的窗口对用户完全无感（拨开关最多 2 秒后就生效），却能省掉几十次文件读取。
_ONLINE_CACHE = {"t": -1e9, "v": False}
_ONLINE_TTL = 2.0


def online() -> bool:
    """当前是否允许联网 —— 跟随前端那个「联网」开关（web_enabled）。

    地图三件事（找地点 / 规划路线 / 取瓦片）全都看这个。
    """
    now = time.time()
    if now - _ONLINE_CACHE["t"] < _ONLINE_TTL:
        return _ONLINE_CACHE["v"]
    try:
        from . import config
        v = bool(config.load_config().get("web_enabled", False))
    except Exception:
        # 读不到配置时按"不联网"处理：宁可少做事，也不能偷偷联网。
        v = False
    _ONLINE_CACHE["t"] = now
    _ONLINE_CACHE["v"] = v
    return v


def invalidate_online() -> None:
    """配置里的「联网」开关刚被改过 → 立刻丢掉上面那个 2 秒小缓存。

    ⚠️ 少了这一步，用户点完开关后前端**马上**来问"现在联网吗"，
    仍可能拿到改之前的答案（最多差 2 秒）。表现就是：关了联网，
    地图那个按钮和底图要愣一下才跟着变 —— 看着像没生效。
    2 秒对人是无感的，但对"点完立刻要看到反馈"这条交互就是要命的。
    """
    _ONLINE_CACHE["t"] = -1e9


def mode_text() -> str:
    return "联网模式" if online() else "离线模式"


def tile_source(allow_net=None) -> str:
    """底图用哪一个 —— "amap"（高德，GCJ-02）还是 "osm"（WGS-84）。

    **联网 + 配了 key → 高德**（中文路网图，实测比 OSM 镜像快一个量级）；
    其余情况（离线、没配 key）→ OSM。

    离线为什么还用 OSM：本地离线缓存的那几百张瓦片就是 OSM 的，
    换成高德等于把用户攒了半天的离线地图全作废。所以"联网用高德、
    离线看已缓存的 OSM"，各用各的坐标系，互不干扰。
    """
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return "osm"
    try:
        from . import amap
        return "amap" if amap.has_key() else "osm"
    except Exception:
        return "osm"


def tile_source_text(src: str = "") -> str:
    return "高德地图" if (src or tile_source()) == "amap" else "OpenStreetMap"


def _ckey(s) -> str:
    """查表用的键归一化：去空格、转小写。"""
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


# ⚠️ 地图**不做任何本地持久化**（2026-09-18 起）。
#    这里曾经有一套"联网查过的地名/路线/瓦片都存本地，断网重放"的缓存，
#    后来发现它带来的问题远多于价值：
#      · 缓存键要带数据源前缀，结果离线（tag 变成 off）就永远读不到自己写的东西，
#        "断网重放"整个失效，还很难发现；
#      · 瓦片缓存要分两套坐标系目录，还有 60 天 TTL、浏览器 7 天缓存、版本号作废……
#        一处改错就表现为"地图整片空白"；
#      · 用户实际很少真的断网用地图，却要为这些复杂度买单。
#    所以现在：**联网时实时查，离线时只用内置地名表 + 直线距离**，
#    磁盘上不再留任何地图数据。离线时地图不显示底图 —— 这是明确的取舍，不是 bug。


# ---------------------------------------------------------------- 内置地名表
_BUILTIN = None


def _builtin():
    """(地名表, 别名表)；模块缺失时返回空表，不影响别的功能。"""
    global _BUILTIN
    if _BUILTIN is None:
        try:
            from .builtin_places import BUILTIN_PLACES, ALIASES
            _BUILTIN = (BUILTIN_PLACES or {}, ALIASES or {})
        except Exception:
            _BUILTIN = ({}, {})
    return _BUILTIN


def builtin_count() -> int:
    return len(_builtin()[0])


def _builtin_search(query: str, limit: int = 5) -> list:
    """在内置表里找。匹配分四档：完全相同 > 表中名以它开头 > 它出现在表中名里 > 表中名出现在它里。

    排序按 (档位, 名字长度) —— 同一档里短名字更通用，排前面。

    别名只管两件事：① 精确别名（"汕大"→"汕头大学"）；
    ② **常规匹配一个都没命中时**，再试"查询词里含某个别名"
       （"广州白云机场"里含"白云机场" → 广州白云国际机场）。
    ⚠️ ② 必须放在最后：否则「中山大学」会被别名「中山」抢到「中山市」去。
    """
    places, aliases = _builtin()
    if not places:
        return []
    key = _ckey(query)
    if not key:
        return []
    out, seen = [], set()

    def _hit(info, kind="builtin"):
        return {"name": info["name"], "lat": info["lat"], "lon": info["lon"],
                "addr": info.get("addr") or "", "kind": kind}

    # ① 精确别名
    al = aliases.get(str(query).strip()) or aliases.get(key)
    if al and al in places:
        out.append(_hit(places[al]))
        seen.add(al)

    cands = []
    for name, info in places.items():
        if name in seen:
            continue
        n = _ckey(name)
        if n == key:
            s = 0
        elif n.startswith(key):
            s = 1
        elif key in n:
            s = 2
        elif n in key:
            s = 3
        else:
            continue
        cands.append((s, len(name), name, info))
    cands.sort(key=lambda x: (x[0], x[1]))
    for _s, _l, name, info in cands:
        out.append(_hit(info))
        if len(out) >= limit:
            break

    # ② 一个都没命中，才试"查询词里含别名"。长的别名优先，
    #    免得短别名（如"中山"）把更具体的词带偏。
    #    ⚠️ 两个额外条件，缺一个都会给出误导性结果：
    #       a) 别名必须是查询词的**后缀**（多出来的部分通常是城市限定：
    #          「广州白云机场」以「白云机场」结尾 ✅）。
    #          反过来「广州大桥」以「大桥」结尾、不以「广州」结尾 → 不命中 ✅
    #          （不这么卡的话，会拿"广州市"去冒充"广州大桥"的答案）。
    #       b) 差额 ≤3 字。
    #    离线时这张表是**唯一**来源，张冠李戴特别容易骗过模型，必须卡住。
    if not out:
        q_s = str(query)
        for a in sorted(aliases.keys(), key=len, reverse=True):
            if len(a) < 2 or aliases[a] not in places:
                continue
            if not q_s.endswith(a) or len(q_s) - len(a) > 3:
                continue
            item = _hit(places[aliases[a]])
            item["loose"] = True      # 标成"猜的"，上层要提示用户核对
            out.append(item)
            break

    return out[:limit]


# ---------------------------------------------------------------- 天气（按坐标）
# 路线规划带上天气：知道出发地现在什么天气、到达那天什么天气，比只给一个公里数有用。
#
# 数据源：**只用中国气象局**（经高德地图）。⚠️ 高德**没有逐小时接口**，
# 所以"抵达时"只能退到**当天的逐日预报**（白天天气现象 + 温度区间），
# 不是"那个小时的确切天气" —— 卡片和给模型的文案里都要说清这一点。
#
# ⚠️ 2026-09-18 起 Open-Meteo 完全不用了（用户要求），所以这里**一个请求都不发给它**。


def weather_at(lat: float, lon: float) -> dict:
    """按坐标取「此刻实况 + 逐日预报」。失败返回 {}（不抛异常，路线照常给）。"""
    try:
        from . import amap as _am
        if not _am.key():
            return {}
        a = _am.weather_at(lat, lon)      # 实况
        fc = _am.forecast_of(lat, lon)    # 逐日（按坐标反查 adcode）
        out = {}
        if a.get("now"):
            out["now"] = a["now"]
        if fc:
            out["daily"] = fc
        if out:
            out["src"] = "中国气象局"
            out["city"] = a.get("city") or ""
        return out
    except Exception:
        return {}


def _rel_day(ds: str) -> str:
    """把日期写成「今天 / 明天 / 后天 / N天后」。

    ⚠️ 必须按**真实日期**算，不能按列表位置，也不能靠"等于今天就是今天、否则明天"猜。
       实测踩过：跨 2 天以上的长途会被错说成「明天」；而 `pick_day` 找不到日期时
       会退回第一条（今天），这时若还标「明天」，数据和标签就自相矛盾了。
    """
    try:
        d = time.strptime(str(ds or "")[:10], "%Y-%m-%d")
    except Exception:
        return ""
    t = time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")
    delta = int((time.mktime(d) - time.mktime(t)) / 86400.0 + 0.5)
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta) or ("%d天后" % delta if delta > 0 else "")


def pick_day(daily: list, when) -> dict:
    """从逐日预报里挑出 `when` 那一天的（按日期字符串匹配；找不到就用第一条）。"""
    rows = daily or []
    if not rows:
        return {}
    key = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when or "")
    for d in rows:
        if d.get("date") == key:
            return d
    return rows[0]


# ---------------------------------------------------------------- 直线距离（离线兜底）
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点直线距离（米）。离线时算不了路网，但至少能告诉用户"大概多远、什么方向"。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_DIRS = ["正北", "东北", "正东", "东南", "正南", "西南", "正西", "西北"]


def bearing_text(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """B 在 A 的哪个方向。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return _DIRS[int((deg + 22.5) % 360 // 45)]


# ---------------------------------------------------------------- HTTP
def _get(url: str, timeout: int = _TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif r.headers.get("Content-Encoding") == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return raw


def _get_json(url: str, timeout: int = _TIMEOUT):
    return json.loads(_get(url, timeout).decode("utf-8", "replace"))


# ---------------------------------------------------------------- 找地点
def _addr(p: dict) -> str:
    parts = [p.get("street"), p.get("housenumber"), p.get("district"),
             p.get("city"), p.get("county"), p.get("state"), p.get("country")]
    return "".join(str(x) for x in parts if x)


def _builtin_exact(query: str):
    """内置表里**精确**命中（同名或别名）才返回，否则 None。

    联网时优先用它：这 231 条是人工核对过坐标的，比 Photon 稳 ——
    实测查「广州白云机场」，Photon 只给「广州白云机场综合保税区东区」这种
    保税区（坐标根本不在航站楼），而表里能直接命中机场。

    ⚠️ 别名匹配放在**精确匹配之后**，而且是"长的别名优先"：
    否则「中山大学」会被别名「中山」抢到「中山市」去。
    """
    places, aliases = _builtin()
    if not places:
        return None
    k = _ckey(query)

    def _pack(name):
        info = places[name]
        return {"name": info["name"], "lat": info["lat"], "lon": info["lon"],
                "addr": info.get("addr") or "", "kind": "builtin"}

    # ① 同名
    for name in places:
        if _ckey(name) == k:
            return _pack(name)
    # ② 正是别名
    al = aliases.get(str(query).strip()) or aliases.get(k)
    if al and al in places:
        return _pack(al)
    # ③ 查询词里**含**某个别名（"广州白云机场"含"白云机场"）
    for a in sorted(aliases.keys(), key=len, reverse=True):
        if len(a) >= 3 and a in str(query) and aliases[a] in places:
            return _pack(aliases[a])
    return None


def _norm_city(s: str) -> str:
    """城市名归一：去掉「省 / 市 / 自治区 / 特别行政区」后缀和空白。"""
    s = str(s or "").strip()
    for suf in ("特别行政区", "自治区", "省", "市"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def _city_ok(item: dict, city: str) -> bool:
    """这条结果是否真的落在 city 里。

    ⚠️ 这一步不能省（2026-11-18 实测）：高德 place/text 在**目标城市查不到时
    不返回空，而是退化成全国搜索** —— place_text("正佳广场", city="上海")
    返回 20 条广州的。不校验的话，用户问上海会拿到广州的结果，
    模型还会照着念，说得像真的一样。
    """
    c0 = _norm_city(city)
    if not c0:
        return True
    for k in ("city", "province", "district", "addr"):
        v = item.get(k)
        if isinstance(v, str) and v and c0 in _norm_city(v):
            return True
    return False


def _name_relevant(q: str, item: dict) -> bool:
    """兜底（地址解析）出来的结果，名字得跟查询词沾边。

    ⚠️ 2026-11-18 实测：geocode("正佳广场") 会把「正佳」当村镇名，返回
    "广东省江门市新会区正佳"、"江西省吉安市青原区正佳"。查询词不像地址
    （没有 路/街/号/市/区/县/镇/村）时，要求结果里含得住整个查询词，
    否则宁可返回空、让上层如实说"没查到"，也不能给个外地的同名村。
    """
    q = str(q or "").strip()
    if not q or re.search(r"[路街巷号市区县镇村]", q):
        return True
    # ⚠️ 只比 name，**不比 addr**：addr 里必然含城市名（"上海市…"），
    #    查询词里的城市部分就白送了覆盖率 —— 实测「上海正佳广场」对
    #    「正大广场」能算到 5/6=0.83 蒙混过关。
    blob = str(item.get("name") or "")
    if not blob:
        return False
    if q in blob:
        return True
    # 用**字符覆盖率**而不是连续子串：别名写法差很多（「白云机场」→「白云国际机场」、
    # 「中大」→「中山大学」），但字都在。实测阈值 0.8 能分开两类：
    #   白云机场 → 白云国际机场 4/4=1.0 留 ✅ ／ 正佳广场 → 江门新会区正佳 3/4=0.75 丢 ✅
    qs = set(q)
    hit = sum(1 for ch in qs if ch in blob)
    return (hit / max(1, len(qs))) >= 0.8


def search_place(query: str, limit: int = 5, allow_net=None, city: str = "") -> list:
    """按名字找地点，返回 [{name, lat, lon, addr, kind}]。

    顺序：① **内置表精确命中** → ② 联网查（高德优先，失败再 Photon）→ ③ 内置表模糊兜底。
    离线时（allow_net=False，或「联网」开关关着）② 跳过，只认内置表 —— 查不到就返回空，
    **不编坐标**。

    city：限定城市（用户说"广州市内""汕头有什么"时由调用方传进来）。

    ⚠️ 结果**不落盘**（2026-09-18 起地图不做本地持久化，见上面那段说明）。
    """
    q = str(query or "").strip()
    if not q:
        return []
    limit = max(1, min(10, int(limit or 5)))
    net = online() if allow_net is None else bool(allow_net)

    # ① 内置表精确命中：人工核过的名字与坐标，比联网查更稳，还省一次请求
    ex = _builtin_exact(q)
    if ex:
        return [ex]

    # ②b 联网且配了高德 key：**先关键词搜 POI，搜不到再当地址解析**
    #     ⚠️ 顺序反了会出大错（实测）：查「天河城」时 /geocode 把它当地址，
    #     命中"江西省南昌市进贤县天河城"—— 那边真有个叫天河城的村子。
    #     凡是想找"某个地方"而不是"某个门牌号"，都该先走 /place/text。
    amap_done = False
    if net:
        try:
            from . import amap
            if amap.has_key():
                amap_done = True
                hits = []
                c0 = str(city or "").strip()
                if c0:
                    # 调用方说了城市，但查询词里往往**还带着城市名**（"上海正佳广场"）。
                    # 整串丢给高德会被模糊匹配成别的地方 —— 实测返回"正大广场"。
                    # 能拆就拆掉前缀；拆不出（"广州塔"→"塔"太短）就整串用。
                    kw = _split_city(q)[1] or q
                else:
                    c0, kw = _split_city(q)   # 从"广州市天河城"里抠出 广州 + 天河城
                if not c0 and kw:
                    # 没有城市限定：/place/text 不带 city 一律 0 条，/geocode 又会
                    # 命中"江西省南昌市进贤县天河城"这种同名村镇。改走「输入提示」
                    # 按高德自己的全国相关度排 —— 实测「天河城」能给出广州天河区那个正主。
                    tips = amap.input_tips(kw)
                    hits = [dict(x, kind=x.get("kind") or "poi")
                            for x in (tips.get("items") or [])]
                    if hits:
                        # 名字完全一致的提前（高德顺序本身已按相关度，只加这一层加固）
                        hits.sort(key=lambda x: 0 if (x.get("name") or "").strip() == kw else 1)
                        return hits[:limit]
                if kw and (c0 or len(kw) >= 2):
                    r = amap.place_text(kw, city=c0)
                    hits = [dict(x, kind=x.get("kind") or "poi")
                            for x in (r.get("items") or [])]
                    if c0:
                        # 高德在目标城市查不到会退化成全国搜索，这里必须自己拦
                        hits = [h for h in hits if _city_ok(h, c0)]
                    # 高德对长串查询会**模糊匹配**：实测 place_text("不存在的假地方xyz")
                    # 返回北京 4 家「不存驿站」（只认了前两个字）。名字不沾边的丢掉。
                    hits = [h for h in hits if _name_relevant(kw, h)]
                if not hits:
                    # 兜底：当地址解析（把城市传下去，否则会解析到别的城市）
                    hits = amap.geocode(q, city=c0)
                    if c0:
                        hits = [h for h in hits if _city_ok(h, c0)]
                    if hits and not re.search(r"[路街巷号市区县镇村]", q):
                        hits = [h for h in hits if _name_relevant(q, h)]
                    # ⚠️ 这一步是"按地址解析"，对**地点名**很不可靠：实测查「天河城」
                    #    会命中"江西省南昌市进贤县天河城"（那边真有个同名村子）。
                    #    所以查询词不像地址（没有"路/街/号/市/区/县/镇/村"）时打个标记，
                    #    让上层把"可能是同名地点、建议补城市名"说出来 ——
                    #    宁可说"不确定"，也不能默默给个外省结果。
                    if hits and not re.search(r"[路街巷号市区县镇村]", q):
                        hits = [dict(h, loose=True) for h in hits]
                if hits:
                    return hits[:limit]
        except Exception:
            amap_done = False
            logger.debug("silent-except", exc_info=True)

    # ③ 联网查 Photon
    #    ⚠️ 只在高德**用不了**时才走（没配 key / 调用异常）。Photon 对中国地名
    #    是纯模糊匹配，实测：「不存在的假地方xyz」返回北京的"不存驿站"、
    #    「上海正佳广场」返回"上海电影广场"——这种噪声比"查不到"更糟。
    #    ⚠️ 不要带 &lang=zh —— 带了中文查询会全部返回空
    if net and not amap_done:
        url = "%s?q=%s&limit=%d" % (PHOTON, urllib.parse.quote(q), limit)
        try:
            d = _get_json(url)
            out = []
            for f in (d.get("features") or []):
                try:
                    lon, lat = f["geometry"]["coordinates"][:2]
                    p = f.get("properties") or {}
                    nm = p.get("name") or q
                    # Photon 有时给一个**比查询词更泛**的名字：查「汕头站」回「汕头」
                    # （坐标是对的，但名字对不上，用户会以为自己查错了）。
                    # 这种时候用用户自己的说法更准。
                    if nm and nm in q and len(nm) < len(q):
                        nm = q
                    out.append({"name": nm, "lat": float(lat),
                                "lon": float(lon), "addr": _addr(p),
                                "kind": p.get("osm_value") or p.get("osm_key") or ""})
                except Exception:
                    continue
            if out:
                return out[:limit]
        except Exception:
            logger.debug("silent-except", exc_info=True)      # 网断了 / Photon 挂了 → 继续往下走兜底

    # ④ 兜底：内置常用地名表（离线时主力）。查不到就返回空，不编坐标。
    return _builtin_search(q, limit)


def _parse_latlon(s: str):
    """支持直接给 "23.35,116.68" 这种坐标 —— 离线时没有地名表也能定位。"""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*$", str(s or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return {"name": "%.5f, %.5f" % (lat, lon), "lat": lat, "lon": lon,
                "addr": "", "kind": "coord"}
    return None


def geocode_one(query: str, allow_net=None, city: str = ""):
    """只要最匹配的一个，返回 dict 或 None。

    city 用来限定城市（用户说"广州市内有什么商场"时，把广州带下来，
    否则"天河城"会被解析成江西进贤县那个同名村子 —— 实测踩过）。
    """
    c = _parse_latlon(query)
    if c:
        return c
    r = search_place(query, limit=1, allow_net=allow_net, city=city)
    if not r:
        return None
    return r[0]


# ---------------------------------------------------------------- 路线
# 步行/骑行：OSRM 的免费服务**只加载了驾车路网**，请求 bike/foot 会静默回退到 car profile。
# 实测同一路线 driving/bike/foot 返回**完全一样**的 11.68km/12分钟 ——
# 12 分钟骑 11.7 公里等于 58km/h，显然给的就是驾车数据。
# 所以这里**一律按驾车取路**，步行/骑行只把**时间**按速度换算，
# 并明确标成估算（estimated=True），绝不假装那是真的步行路线。
_WALK_KMH = 4.8
_BIKE_KMH = 15.0


def plan_transit(origin: str, dest: str, allow_net=None) -> dict:
    """**公交换乘方案** —— 只有高德能给，OpenStreetMap 完全没有这类数据。

    高德的公交接口要求传起点城市（city 参数），所以先从地理编码结果里拿。
    """
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return {"ok": False, "offline": True,
                "error": "公交查询必须联网 —— 离线时本地没有公交数据。"}
    try:
        from . import amap
        if not amap.has_key():
            return {"ok": False, "error": "公交查询需要高德 key（在设置里填上即可）。"}
        a = geocode_one(origin, allow_net=net)
        b = geocode_one(dest, allow_net=net)
        if not a or not b:
            return {"ok": False,
                    "error": "找不到%s" % ("起点" if not a else "终点")}
        r = amap.transit(a["lat"], a["lon"], b["lat"], b["lon"],
                         city=a.get("city") or "")
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "没找到公交方案"}
        r["from"] = {"name": a["name"], "lat": a["lat"], "lon": a["lon"]}
        r["to"] = {"name": b["name"], "lat": b["lat"], "lon": b["lon"]}
        return r
    except Exception as e:
        return {"ok": False, "error": "公交查询失败：%s" % e}


def _route_amap(a: dict, b: dict, m: str):
    """走高德路径规划。没配 key / 失败 → 返回 None，调用方自动回退 OSRM。

    ⚠️ 关键差别：高德的步行/骑行是**真实路径**（会走人行道、小路），
       不像免费 OSRM 只能给驾车路网、再拿速度换算时间。
       所以走高德时**不能**再标 estimated=True。
    """
    try:
        from . import amap
        if not amap.has_key():
            return None
        r = amap.route(a["lat"], a["lon"], b["lat"], b["lon"], m,
                       alternatives=(m == "driving"))
        if not r.get("ok") or not r.get("routes"):
            return None
        return {"ok": True, "routes": r["routes"],
                "traffic_aware": bool(r.get("traffic_aware"))}
    except Exception:
        return None


def _best_and_reason(routes: list) -> tuple:
    """从多条候选里挑推荐的那条，并给出**基于真实数字**的理由。

    规则：时间优先；但若另一条只慢一点点（<8%）却明显更短（<95%），就选它 ——
    多绕十几公里省半分钟不划算。返回 (推荐下标, 理由文本)。
    """
    if not routes:
        return 0, ""
    if len(routes) == 1:
        return 0, "只有这一条可行路线"

    idx = min(range(len(routes)), key=lambda i: routes[i]["duration_s"])
    for i, r in enumerate(routes):
        if i == idx:
            continue
        if (r["duration_s"] <= routes[idx]["duration_s"] * 1.08
                and r["distance_m"] < routes[idx]["distance_m"] * 0.95):
            idx = i
            break

    b = routes[idx]
    other = min([r for i, r in enumerate(routes) if i != idx],
                key=lambda r: r["duration_s"])
    faster_min = (other["duration_s"] - b["duration_s"]) / 60.0    # >0 表示推荐的更快
    longer_km = (b["distance_m"] - other["distance_m"]) / 1000.0   # >0 表示推荐的更长

    bits = []
    if faster_min >= 0.5:
        bits.append("比另一条快约 %.0f 分钟" % faster_min)
    if longer_km <= -0.3:
        bits.append("距离还少约 %.1f 公里" % (-longer_km))
    elif longer_km >= 0.3:
        bits.append("代价是多走约 %.1f 公里" % longer_km)
    if not bits:
        bits.append("与另一条耗时接近，整体更顺")
    return idx, "；".join(bits)


def _mk_routes(raw_routes: list, mode: str, a: dict, b: dict) -> list:
    """把 OSRM 的多条候选整理成前端要的形状（统一 [lat,lon]、抽稀、算估算时间）。"""
    out = []
    for i, r in enumerate(raw_routes[:3]):        # 最多给 3 条，再多也没意义
        coords = ((r.get("geometry") or {}).get("coordinates") or [])
        pts = [[c[1], c[0]] for c in coords if len(c) >= 2]
        if len(pts) > 400:                        # 点太多前端画着卡，抽稀（首尾必留）
            step = len(pts) / 400.0
            keep = [pts[int(k * step)] for k in range(400)]
            if keep[-1] != pts[-1]:
                keep.append(pts[-1])
            pts = keep
        dist = float(r.get("distance") or 0)
        dur = float(r.get("duration") or 0)
        if mode == "foot":
            dur = dist / 1000.0 / _WALK_KMH * 3600.0
        elif mode == "bike":
            dur = dist / 1000.0 / _BIKE_KMH * 3600.0
        out.append({"idx": i, "distance_m": dist, "duration_s": dur, "points": pts})
    return out


def plan_route(origin: str, dest: str, mode: str = "driving", allow_net=None,
               want_weather: bool = False, city: str = ""):
    """规划两点之间的路线。origin/dest 传地名（会自动地理编码），也接受 "lat,lon"。

    返回里顶层保留 distance_m/duration_s/points（＝**推荐的那条**），
    另外加 routes:[…] 给出多条候选，每条带 recommended / reason。
    离线时：没有路网可用，返回 ok=False + approx（直线距离，仅供参考）。
    city：限定城市，透传给地理编码（避免同名地点被解析到外省）。

    ⚠️ 结果**不落盘**（地图不做本地持久化，见文件上方那段说明）——
       所以离线时不会"重放以前算过的路线"，只会给直线距离并明确说明。
    """
    m = {"驾车": "driving", "开车": "driving", "driving": "driving", "car": "driving",
         "步行": "foot", "走路": "foot", "walking": "foot", "foot": "foot",
         "骑行": "bike", "自行车": "bike", "cycling": "bike", "bike": "bike"}.get(
        str(mode or "").strip().lower(), "driving")
    net = online() if allow_net is None else bool(allow_net)

    a = geocode_one(origin, allow_net=net, city=city)
    b = geocode_one(dest, allow_net=net, city=city)
    if not a:
        return {"ok": False, "offline": not net, "error": "找不到起点「%s」" % origin}
    if not b:
        return {"ok": False, "offline": not net, "error": "找不到终点「%s」" % dest}

    # 联网算：**优先走高德**（真实步行/骑行路径 + 实时路况 + 备选方案），
    # 没配 key 或请求失败再回退 OSRM
    why = ""
    if net:
        _am = _route_amap(a, b, m)
        if _am:
            routes = _am["routes"]
            best, reason = _best_and_reason(routes)
            for rt in routes:
                rt.setdefault("tolls", 0.0)
                rt.setdefault("traffic_lights", 0)
                rt["recommended"] = (rt["idx"] == best)
                rt["reason"] = reason if rt["idx"] == best else ""
            res = _shape(routes, best, m, a, b, net, want_weather, amap_ok=True)
            res["traffic_aware"] = _am.get("traffic_aware")
            res["source"] = "amap"
            return res

        url = OSRM % ("driving", "%.6f,%.6f;%.6f,%.6f"
                      % (a["lon"], a["lat"], b["lon"], b["lat"]))
        # alternatives=true：中长途能给 2~3 条备选（实测广州塔→白云机场 2 条）；
        # 短途本来就只有一条路，返回 1 条也正常。
        url += "?overview=full&geometries=geojson&alternatives=true"
        try:
            d = _get_json(url, timeout=25)
            if d.get("code") == "Ok" and d.get("routes"):
                routes = _mk_routes(d["routes"], m, a, b)
                best, reason = _best_and_reason(routes)
                for r in routes:
                    r["recommended"] = (r["idx"] == best)
                    r["reason"] = reason if r["idx"] == best else ""
                return _shape(routes, best, m, a, b, net, want_weather)
            why = str(d.get("code") or "无结果")
        except Exception as e:
            why = str(e)

    # ②b 联网没成功（或本来就离线）→ 直线距离兜底（明确标注不是实际道路）
    return _approx_route(a, b, m, net, why)


def _shape(routes: list, best: int, m: str, a: dict, b: dict,
           net: bool, want_weather: bool, amap_ok: bool = False) -> dict:
    """统一拼装返回结构。

    amap_ok=True 表示数据来自高德 —— 它的步行/骑行是**真实路径**，
    所以不再标 estimated，也不显示"时间系估算"那句说明。
    """
    b_rt = routes[best] if routes else {"distance_m": 0, "duration_s": 0, "points": []}
    est = (m in ("foot", "bike")) and not amap_ok
    res = {
        "ok": True, "mode": m, "routes": routes, "best": best,
        "estimated": est,
        "from": {"name": a.get("name"), "lat": a.get("lat"), "lon": a.get("lon")},
        "to": {"name": b.get("name"), "lat": b.get("lat"), "lon": b.get("lon")},
        # 顶层保留这三个，兼容老调用方
        "distance_m": b_rt["distance_m"], "duration_s": b_rt["duration_s"],
        "points": b_rt["points"],
    }
    if est:
        # ⚠️ 这段会直接显示在卡片上（前端只做转义、不渲染 markdown），所以别写星号
        res["note"] = ("免费路网服务只提供驾车路径，这里是驾车路线；"
                       "%s时间按 %.1f km/h 估算，仅供参考，不是真实步行/骑行路径。"
                       % ("步行" if m == "foot" else "骑行",
                          _WALK_KMH if m == "foot" else _BIKE_KMH))
    if want_weather and net:
        wf, wt = weather_at(a["lat"], a["lon"]), weather_at(b["lat"], b["lon"])
        if wf or wt:
            # 抵达"那一天"（不是"那一小时"）—— 高德没有逐小时接口，
            # 所以这里拿当天/次日的**逐日预报**，文案里也必须这么讲。
            now_ts = time.time()
            arrive_ts = now_ts + float(b_rt.get("duration_s") or 0)
            arrive_date = time.strftime("%Y-%m-%d", time.localtime(arrive_ts))
            # ⚠️ 标签按**选中的那条数据自己的日期**算（见 _rel_day 的注释）：
            #    pick_day 找不到时会退回今天那条，此时若还写"明天"就自相矛盾。
            _picked = pick_day(wt.get("daily"), arrive_date)
            res["weather"] = {
                "from": {"name": a.get("name"), "now": wf.get("now") or {},
                         "src": wf.get("src") or ""},
                "to": {"name": b.get("name"),
                       "day": _picked,
                       "day_label": _rel_day((_picked or {}).get("date")) or "当天",
                       "src": wt.get("src") or ""},
            }
    return res


def _approx_route(a: dict, b: dict, m: str, net: bool, why: str = "") -> dict:
    """离线兜底：没有路网数据就算不出真路线，但直线距离和方位是**本地能算的**。

    ⚠️ 必须让上层知道这不是真路线（ok=False + approx + 说明文字），
       否则用户会以为"直线 6 公里"就是开车距离。
    """
    d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if not net:
        err = "离线模式下没有「%s → %s」的路线缓存" % (a["name"], b["name"])
    else:
        err = "路线服务暂时不可用%s" % ("（%s）" % why if why else "")
    return {
        "ok": False, "offline": not net, "mode": m, "error": err,
        "from": {"name": a["name"], "lat": a["lat"], "lon": a["lon"]},
        "to": {"name": b["name"], "lat": b["lat"], "lon": b["lon"]},
        "approx": {"distance_m": d,
                   "bearing": bearing_text(a["lat"], a["lon"], b["lat"], b["lon"])},
        "hint": "打开「联网」开关就能查到真实道路距离；离线时地图不做缓存，只能给直线参考。",
    }


def fmt_distance(m: float) -> str:
    return ("%.0f 米" % m) if m < 1000 else ("%.1f 公里" % (m / 1000.0))


def fmt_duration(s: float) -> str:
    s = int(s or 0)
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分钟" % round(s / 60.0)
    return "%d 小时 %d 分钟" % (s // 3600, round((s % 3600) / 60.0))


# ---------------------------------------------------------------- 瓦片（实时代理，不落盘）
def get_tile(z: int, x: int, y: int, allow_net=None, src: str = ""):
    """取一张瓦片（**纯实时转发，不在本地存任何东西**）。返回 (bytes, from_cache)。

    ⚠️ 返回值的第二个元素永远是 False —— 地图不做本地缓存了，这里保留这个
       二元组只是为了兼容调用方的老签名。

    离线时**绝不联网**，直接返回 (None, False)，前端据此提示"离线不显示底图"。
    """
    z, x, y = int(z), int(x), int(y)
    if not (0 <= z <= MAX_ZOOM):
        return None, False
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        return None, False
    src = src or tile_source(allow_net)

    if not (online() if allow_net is None else bool(allow_net)):
        return None, False

    for tpl in (AMAP_TILE_MIRRORS if src == "amap" else TILE_MIRRORS):
        try:
            data = _get(tpl % (z, x, y), timeout=12)
        except Exception:
            continue
        # ⚠️ 高德对越界/没有数据的瓦片会回一张 ~179 字节的**纯色 PNG**，HTTP 200、
        #    看着像成功。这种一律当失败（否则地图会静默地整片空白）。
        if data and data[:4] == b"\x89PNG" and len(data) >= 300:
            return data, False

    # ⚠️ 高德**只管中国大陆**：境外瓦片是空白的。落在境外就拿**同号**的 OSM 瓦片顶上 ——
    #    境外 GCJ-02 与 WGS-84 重合（偏移算法只在国境内生效），同号即同一块地，位置不会错。
    #    境内**不出这招**：真出问题（参数写错、服务挂了）必须让它 404 看得见。
    if src == "amap":
        la, lo = _tile_center(z, x, y)
        if not _in_china(la, lo):
            return get_tile(z, x, y, allow_net=allow_net, src="osm")
    return None, False


def _tile_center(z: int, x: int, y: int):
    """瓦片中心点的经纬度（用来判断这块瓦片在不在国内）。"""
    n = 1 << z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2.0 * (y + 0.5) / n))))
    return lat, lon


def _in_china(lat: float, lon: float) -> bool:
    """是否落在国内（范围和 GCJ-02 偏移判定用的那套一致）。

    ⚠️ 这是个**矩形近似**，不是国境线。蒙古、中南半岛北部（河内/曼谷一线以北）、
    中亚部分落在矩形内会被判成"境内"。后果：那些地方高德同样没数据，
    但我们不会回退 OSM，于是那块地会空白（不是错位，只是没有底图）。
    港澳台高德有数据，不受影响。要更准得上国境线多边形，目前不值得。
    """
    return 73.66 < lon < 135.05 and 3.86 < lat < 53.55


def _lonlat_to_tile(lat: float, lon: float, z: int):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _tile_latlon(lat: float, lon: float, src: str):
    """把 WGS-84 的坐标换成"瓦片所在坐标系"的坐标。

    高德瓦片是 GCJ-02 的：拿 WGS-84 去算瓦片号，会正好错开半条街，
    瓦片号算错的话，图和点位就会对不上。这一步不能省。
    """
    if src != "amap":
        return lat, lon
    try:
        from . import amap
        lng, la = amap.wgs84_to_gcj02(float(lon), float(lat))
        return la, lng
    except Exception:
        return lat, lon


def tile_url(z: int, x: int, y: int) -> str:
    return "/api/map/tile/%d/%d/%d.png" % (z, x, y)


def static_map_hint(lat: float, lon: float, zoom: int = 13) -> str:
    """给一个不用 JS 也能看的入口（OSM 网页版）。"""
    return ("https://www.openstreetmap.org/?mlat=%.5f&mlon=%.5f#map=%d/%.5f/%.5f"
            % (lat, lon, zoom, lat, lon))


# ---------------------------------------------------------------- 附近场所（Overpass）
# ⚠️ 为什么不能用 Photon 做"附近"：Photon 是**按名字搜**的 ——
#    搜「餐厅」它只会找**名字里带"餐厅"两个字**的店（实测返回的是几十上百公里外的
#    "餐厅""XX餐厅"），根本给不出"这一带所有餐厅"。按类别+半径查 POI 得用 Overpass。
# ⚠️ Overpass 会限流（实测连续几个请求就 HTTP 429）。现在不做本地缓存了，
#    所以被限流时如实告诉用户稍后再试，不要拿旧数据糊弄。
OVERPASS = "https://overpass-api.de/api/interpreter"

# 类别关键词 → Overpass 标签。用户说什么词都能对上（中英文都收）
_CAT_TAGS = [
    (("餐厅", "吃饭", "吃的", "吃点", "下馆子", "饭店", "餐馆", "美食", "快餐",
      "restaurant", "food"),
     '["amenity"~"restaurant|fast_food|food_court"]'),
    (("咖啡", "咖啡馆", "cafe", "coffee"), '["amenity"="cafe"]'),
    (("便利", "小卖", "杂货", "便利店", "convenience"),
     '["shop"~"convenience|grocery|general"]'),
    (("购物中心", "商场", "商城", "百货", "综合体", "mall", "shopping"),
     '["shop"~"mall|department_store"]'),
    (("超市", "supermarket"), '["shop"~"supermarket|department_store"]'),
    (("药店", "药房", "pharmacy"), '["amenity"="pharmacy"]'),
    (("医院", "诊所", "卫生院", "hospital", "clinic"), '["amenity"~"hospital|clinic|doctors"]'),
    (("银行", "bank"), '["amenity"="bank"]'),
    (("取款", "atm", "取钱"), '["amenity"~"atm|bank"]'),
    (("加油", "加油站", "fuel"), '["amenity"="fuel"]'),
    (("充电", "充电桩", "charging"), '["amenity"="charging_station"]'),
    (("停车", "停车场", "parking"), '["amenity"="parking"]'),
    (("酒店", "宾馆", "旅馆", "住宿", "hotel"), '["tourism"~"hotel|hostel|guest_house|motel"]'),
    (("学校", "大学", "学院", "school", "university"), '["amenity"~"school|university|college"]'),
    (("公交", "巴士", "bus"), '["highway"="bus_stop"]'),
    (("地铁", "subway", "metro"), '["railway"="station"]["station"~"subway"]'),
    (("火车", "高铁", "车站", "railway"), '["railway"~"station|halt"]'),
    (("厕所", "洗手间", "卫生间", "toilet"), '["amenity"="toilets"]'),
    (("公园", "绿地", "park"), '["leisure"~"park|garden"]'),
    (("菜市场", "市场", "market"), '["amenity"="marketplace"]'),
    (("快递", "驿站", "邮局", "post"), '["amenity"~"post_office|post_depot"]'),
    (("理发", "美发", "hair"), '["shop"~"hairdresser|beauty"]'),
    (("五金", "建材", "hardware"), '["shop"~"hardware|doityourself"]'),
    (("服装", "衣", "clothes"), '["shop"~"clothes|shoes"]'),
    (("景点", "旅游", "attraction", "sight"), '["tourism"~"attraction|viewpoint|museum"]'),
    (("医院", "clinic"), '["amenity"~"hospital|clinic"]'),
]


def _cat_tag(category: str) -> tuple:
    """把用户说的类别翻译成 Overpass 标签。返回 (标签, 标准类别名) 或 (None, "")。

    标准类别名还会被当成**关键词发给高德**（高德的类型码太杂，用中文词更稳），
    所以每组第一个词要选"最适合当搜索词"的那个。
    """
    c = str(category or "").strip().lower()
    if not c:
        return None, ""
    for keys, tag in _CAT_TAGS:
        for k in keys:
            if k in c:
                return tag, keys[0]
    return None, ""


# 常见城市名（不带"市"字），用来从查询词里抠出"要在哪个城市找"。
# ⚠️ 为什么非要有这个：高德的 /place/text **必须带地区限定**才出结果 ——
#    实测纯关键词（哪怕加 citylimit=false）一律返回 0 条；
#    而它的 /geocode 是**按地址解析**的：查「天河城」会命中
#    "江西省南昌市进贤县天河城"（那边真有个叫天河城的村子），
#    广州那个正主反而出不来。所以"搜 POI"和"解析地址"是两条路，不能混。
# 全国地级行政区（2026-11-18 从 81 个补全到 305 个）。
#
# ⚠️ 为什么必须补全（用户原话：「我问广州有什么景点，结果一堆不准确的内容」）：
#    原来只硬编码了 81 个城市，**承德、保定、临沂、漳州、襄阳这些全部不在表里** →
#    city_of() 返回空 → 退化成「以 geocode 结果为中心的 1.5 公里周边搜索」。
#    实测 place="承德" 被定位成「承德站」，只返回火车站附近 4 个景点；
#    补全后走全城检索，返回避暑山庄 / 普宁寺 / 双塔山森林公园 ✅
_CITIES = [
    # 直辖市
    "北京", "上海", "天津", "重庆",
    # 河北
    "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德",
    "沧州", "廊坊", "衡水",
    # 山西
    "太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州",
    "临汾", "吕梁",
    # 内蒙古
    "呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔",
    "巴彦淖尔", "乌兰察布", "锡林郭勒", "阿拉善",
    # 辽宁
    "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新",
    "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛",
    # 吉林
    "长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城", "延边",
    # 黑龙江
    "哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯",
    "七台河", "牡丹江", "黑河", "绥化", "大兴安岭",
    # 江苏
    "南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城",
    "扬州", "镇江", "泰州", "宿迁",
    # 浙江
    "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山",
    "台州", "丽水",
    # 安徽
    "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山",
    "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城",
    # 福建
    "福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德",
    # 江西
    "南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春",
    "抚州", "上饶",
    # 山东
    "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安",
    "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽",
    # 河南
    "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳",
    "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店",
    # 湖北
    "武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州",
    "黄冈", "咸宁", "随州", "恩施",
    # 湖南
    "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳",
    "郴州", "永州", "怀化", "娄底", "湘西",
    # 广东
    "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门",
    "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山",
    "潮州", "揭阳", "云浮",
    # 广西
    "南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林",
    "百色", "贺州", "河池", "来宾", "崇左",
    # 海南
    "海口", "三亚", "三沙", "儋州",
    # 四川
    "成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江",
    "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳",
    # 贵州
    "贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔东南", "黔南", "黔西南",
    # 云南
    "昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "大理",
    "红河", "文山", "西双版纳", "楚雄", "德宏", "迪庆",
    # 西藏
    "拉萨", "日喀则", "昌都", "林芝", "山南", "那曲",
    # 陕西
    "西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛",
    # 甘肃
    "兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉",
    "庆阳", "定西", "陇南", "临夏", "甘南",
    # 青海
    "西宁", "海东",
    # 宁夏
    "银川", "石嘴山", "吴忠", "固原", "中卫",
    # 新疆
    "乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "昌吉", "阿克苏", "喀什", "和田",
    "伊犁", "塔城", "阿勒泰",
]


def _split_city(q: str) -> tuple:
    """从查询词里抠出 (城市名, 剩余关键词)。

    ⚠️ 两个不能省的保护：
      ① 只在**开头**匹配，而且剩余部分得有 ≥2 个字 ——
         否则「广州塔」会被拆成 city=广州 + kw=塔，搜出来一堆别的塔；
      ② 剩下的部分不能是个**裸的通用词** —— 否则「汕头大学」会被拆成
         city=汕头 + kw=大学，搜出"汕头广播电视大学"这种。
         这种情况宁可整串当地址解析（高德对"汕头大学"是能解析的）。
    """
    s = str(q or "").strip()
    best = ""
    for c in _CITIES:
        for form in (c + "市", c):
            if s.startswith(form) and len(s) - len(form) >= 2:
                if len(form) > len(best):
                    best = form
                break
    if not best:
        return "", s
    kw = s[len(best):].strip()
    if kw in _GENERIC_SUFFIX:
        return "", s
    return best[:-1] if best.endswith("市") else best, kw


# 「城市名 + 这些词」不该被拆开当关键词搜（会搜出一堆别的同名机构）
_GENERIC_SUFFIX = {
    "大学", "中学", "小学", "学院", "学校", "医院", "公园", "车站", "机场",
    "广场", "大厦", "酒店", "银行", "市场", "政府", "火车站", "高铁站",
}


def _poi_info(t: dict) -> dict:
    """看这条记录有多少可用信息，回一句**人话说明**。

    ⚠️ 刻意**不输出分数、不输出星级** —— OSM 根本没有评分数据，
       给个数字出来一定会被当成口碑分（用户明确要求把评分完全去掉）。
       只回答"这条记录靠不靠谱、要不要先确认"。
    """
    n = 0
    if t.get("name"):
        n += 1
    if any(t.get(k) for k in ("addr:street", "addr:full", "addr:housenumber",
                              "addr:city", "addr:district", "addr:province")):
        n += 1
    if t.get("phone") or t.get("contact:phone"):
        n += 1
    if t.get("website") or t.get("contact:website"):
        n += 1
    if t.get("opening_hours"):
        n += 1
    if n >= 3:
        return {"info_note": "记录里地址、联系方式这类信息比较全，看着是正常营业的场所"}
    if n >= 2:
        return {"info_note": "记录信息不太全，出发前建议先确认一下"}
    return {"info_note": "记录里基本只有名字和坐标，可能是小摊小店、也可能已经关了，去之前最好先确认"}


def _overpass(q: str):
    """问 Overpass。

    ⚠️ 两个必踩的坑：
    1. **必须重试**。官方实例 `overpass-api.de` 负载很高，实测 504 Gateway Timeout
       和 429 Too Many Requests 都很常见 —— 一次失败不代表"没有结果"。
    2. headers 的值**必须是字符串**。上次传了个 dict，urllib 在 putheader 里抛
       `TypeError: expected string or bytes-like object, got 'dict'`，
       看着像网络故障，其实是自己写错，白白浪费一轮排查。

    备选实例实测情况（别再试一遍了）：
      · `overpass-api.de`            —— 唯一能出中国数据的，但经常 504/429
      · `overpass.osm.ch`            —— 通，但是**瑞士区域实例**，只有瑞士数据
      · `kumi.systems` / `mail.ru` / `private.coffee` —— 一律超时
    所以这里只打官方实例，靠重试 + 缓存扛住。
    """
    body = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(OVERPASS, data=body, headers={
                "User-Agent": _UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))       # 504/429 大多歇一下就好
    raise last


def _nearby_amap(lat, lon, cat, radius, limit):
    """走高德周边搜索。没配 key / 失败 → 返回 None，调用方自动回退 Overpass。"""
    try:
        from . import amap
        if not amap.has_key():
            return None
        r = amap.place_around(lat, lon, cat, radius=radius, offset=min(int(limit), 25))
        if not r.get("ok") or not r.get("items"):
            return None
        items = []
        for it in r["items"][:limit]:
            row = {
                "name": it.get("name") or "",
                "lat": it["lat"], "lon": it["lon"],
                "dist_m": int(it.get("dist_m") or 0),
                "kind": it.get("kind") or cat,
                "addr": it.get("addr") or "",
                "phone": it.get("phone") or "",
                "website": "",
                "hours": "",
                "info_note": "",
            }
            # 高德有**真实评分**（餐饮/酒店等类目）和人均消费 —— OSM 完全没有
            if it.get("rating"):
                row["rating"] = str(it["rating"])
            if it.get("cost"):
                row["cost"] = str(it["cost"])
            items.append(row)
        return {"ok": True, "category": cat, "radius": radius, "center": [lat, lon],
                "total": int(r.get("total") or len(items)), "items": items,
                "source": "amap",
                "note": "数据来自高德地图。"}
    except Exception:
        return None


# 「河北省承德市」「上海市浦东新区」这种**纯行政区划**名的识别用
_PROV_PREFIX_RE = re.compile(
    r"^(?:北京市|天津市|上海市|重庆市|"
    r"[\u4e00-\u9fa5]{2,7}?省|[\u4e00-\u9fa5]{2,8}?自治区|"
    r"香港特别行政区|澳门特别行政区)")
_CITY_TAIL_RE = re.compile(r"^[\u4e00-\u9fa5]{2,7}市$")
_CITY_TAIL2_RE = re.compile(r"^([\u4e00-\u9fa5]{2,7}市)([\u4e00-\u9fa5]{2,7}市)$")
_ZHOU_TAIL_RE = re.compile(r"^[\u4e00-\u9fa5]{2,10}?自治州([\u4e00-\u9fa5]{2,7}市)$")
_PREF_TAIL_RE = re.compile(r"^[\u4e00-\u9fa5]{2,8}?(地区|盟)$")
# 「广州市天河区」「上海市浦东新区」→ 广州 / 上海
_CITY_SUB_RE = re.compile(
    r"^([\u4e00-\u9fa5]{2,7})市(?:[\u4e00-\u9fa5]{2,6}(?:区|县|市))?$")


def _city_from_admin_name(name: str) -> str:
    """从「河北省承德市」这种纯行政区划名里取出城市名；不是纯行政区划就返回 ""。

    ⚠️ 判据是**整个名字就是「省+市」结构**，不是「以地名结尾」——
       实测 geocode("天河城") 返回 "江西省南昌市进贤县天河城"，
       它确实以地名结尾，但中间多了县一级，必须判成「不是城市」。
    """
    s = _PROV_PREFIX_RE.sub("", str(name or "").strip())
    m = _CITY_TAIL2_RE.match(s)          # 甘肃省酒泉市敦煌市（县级市）→ 敦煌
    if m:
        return m.group(2)[:-1]
    if _CITY_TAIL_RE.match(s):           # 河北省承德市 → 承德
        return s[:-1]
    m = _ZHOU_TAIL_RE.match(s)           # 迪庆藏族自治州香格里拉市 → 香格里拉
    if m:
        return m.group(1)[:-1]
    m = _PREF_TAIL_RE.match(s)           # 喀什地区 / 锡林郭勒盟 → 喀什 / 锡林郭勒
    if m:
        return s[:m.start(1)]
    return ""


def city_of(place: str, allow_net=None) -> str:
    """「广州市」「广州」「广州市天河区」→「广州」；不是城市名就返回 ""。

    用途：判断这次查询是「整座城市里有什么」还是「某个地点周围有什么」——
    两者的检索方式完全不同（见 nearby 里的说明）。
    返回空 = 「这不是城市名」，调用方会按「地点周边」处理。
    """
    s = str(place or "").strip()
    if not s:
        return ""
    for c in _CITIES:
        if s in (c, c + "市"):
            return c
    m = _CITY_SUB_RE.match(s)            # 广州市天河区 → 广州
    if m:
        return m.group(1)
    # ---- 表外城市（县级市、自治州首府、更冷门的地级市）----
    # 不靠再硬编码一份列表：问一次高德地理编码，看返回的是不是「省+市」纯行政区划。
    # 实测召回 23/28、对地点名 0 误判（天河城/汕头大学/广州塔/正佳广场全判对）。
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return ""
    try:
        from . import amap
        if not amap.has_key():
            return ""
        r = amap.geocode(s)
        if r:
            return _city_from_admin_name(r[0].get("name"))
    except Exception:                                       # noqa: BLE001
        logger.debug("反查城市失败（按表外处理）", exc_info=True)
    return ""


# 这些不是「场所」，是场所的附属设施 —— 出现在清单里只会挤掉真正要找的地方。
_POI_NOISE = ("管理办公室", "管理委员会", "客服中心", "服务中心", "售票处", "停车场",
              "停车库", "入口", "出口", "卫生间", "公共厕所", "派出所", "居委会",
              "物业管理", "游客中心", "门卫", "值班室")
# 类型明显不对的（高德按关键词搜会捎带出来）
_KIND_NOISE = ("住宅小区", "公司企业", "政府机构", "商务写字楼", "产业园区", "地名地址")


def _clean_items(items: list, cat: str) -> list:
    """去噪 + 去重。

    ⚠️ 两个实测出来的毛病（用户原话：「同一地方重复出现、根本不是景点」）：
      ① 高德会把同一景区的子点位全列出来 ——「人民公园-宫灯帏」「人民公园-新娘像」…
         600 条里前 20 条全是这种，真正想看的公园反而被挤到后面。
         → 取「-」前的父名去重，一个景区只留一条。
      ② 关键词搜会捎带「XX停车场」「XX售票处」「XX管理办公室」和「住宅小区」。
         → 按词表过滤。
    """
    out, seen, pts = [], set(), []
    for it in items:
        nm = str(it.get("name") or "").strip()
        if not nm:
            continue
        if any(b in nm for b in _POI_NOISE):
            continue
        kind = str(it.get("kind") or "")
        if kind and any(b in kind for b in _KIND_NOISE):
            continue
        # 「人民公园-宫灯帏」→ 父名「人民公园」；已经收过就不再收
        base = nm.split("-")[0].strip() if "-" in nm else nm
        key = base.lower()
        if key in seen:
            continue
        dup = False
        for (la, lo, kn) in pts:
            if kn == key and haversine(it["lat"], it["lon"], la, lo) < 200:
                dup = True
                break
        if dup:
            continue
        seen.add(key)
        pts.append((it["lat"], it["lon"], key))
        out.append(it)
    return out


def _nearby_amap_text(city: str, cat: str, limit: int,
                      clat: float, clon: float):
    """**整座城市**里找某类场所：走高德关键词搜索（按相关度），不按距离。

    ⚠️ 为什么不能用周边搜索（place/around）：
       实测「广州有什么商场」→ around 返回 82 条，但按**距离**排，
       前 20 全是市政府旁边 140~484 米的小店（Amall商场、广德商场…），
       **6 公里外的天河城根本进不来**（用户原话：「连最大的天河城都没有」）。
       而 place/text 带 city 搜「购物中心」：广州塔广场 / 正佳广场 / 天河城 /
       太古汇 / 万菱汇 全在前 10 —— 这才是用户想要的「广州有什么商场」。
    """
    try:
        from . import amap
        if not amap.has_key():
            return None
        r = amap.place_text(cat, city=city, offset=25)
        if not r.get("ok") or not r.get("items"):
            return None
        # ⚠️ 必须校验城市（2026-11-18 实测）：城市名不存在或打错字时，
        #    高德**不返回空，而是退化成默认城市** —— place_text(city="广洲")
        #    和 place_text(city="不存在市xyz") 都返回北京故宫/景山/北海公园。
        #    不拦的话用户问「承德有什么景点」，拿到的是一堆北京的。
        raw = [it for it in r["items"] if _city_ok(it, city)]
        if not raw:
            return None
        items = []
        for it in raw:
            row = {
                "name": it.get("name") or "",
                "lat": it["lat"], "lon": it["lon"],
                "dist_m": int(haversine(clat, clon, it["lat"], it["lon"])),
                "kind": it.get("kind") or cat,
                "addr": it.get("addr") or "",
                "phone": it.get("phone") or "",
                "website": "",
                "hours": "",
                "info_note": "",
            }
            if it.get("rating"):
                row["rating"] = str(it["rating"])
            if it.get("cost"):
                row["cost"] = str(it["cost"])
            items.append(row)
        items = _clean_items(items, cat)
        if not items:
            return None
        return {"ok": True, "category": cat, "radius": 0, "center": [clat, clon],
                "total": len(items), "items": items[:limit], "source": "amap",
                "city": city, "level": "city",
                "note": "数据来自高德地图（**全城**检索，按相关度排序；距离是到城市中心的直线距离）。"}
    except Exception:
        return None


def nearby(lat: float, lon: float, category: str, radius: int = 1500,
           limit: int = 20, allow_net=None, city: str = "") -> dict:
    """查某个坐标周围指定类别的场所（按距离排序）。

    返回 {"ok","items":[{name,lat,lon,dist_m,kind,addr,phone,website,hours,
                        info_note}],"category","radius","note"}
    ⚠️ **没有 score 字段** —— OSM 不提供评分，也不该拿别的数字冒充评分。
    """
    tag, cat = _cat_tag(category)
    if not tag:
        return {"ok": False, "error": "不认识这个类别：「%s」。可以试：餐厅 / 便利店 / "
                                     "超市 / 药店 / 医院 / 银行 / 加油站 / 停车场 / "
                                     "酒店 / 学校 / 公交站 / 公园 / 厕所…" % category}
    net = online() if allow_net is None else bool(allow_net)
    # 高德最大支持 50000 米；「整个城市里有什么」这种问法需要大半径
    radius = max(100, min(50000, int(radius or 1500)))
    limit = max(1, min(60, int(limit or 20)))
    # ⚠️ 不做缓存（结果不落盘），离线时这类查询**没有兜底数据** —— 直接如实说查不了。
    if not net:
        return {"ok": False, "offline": True,
                "error": "离线模式下查不了附近场所（这一带的数据不在本地）。"
                         "把「联网」开关打开就能查。"}

    # ---- 联网：**优先走高德**（配了 key 的话）----
    # 高德的 POI 库比 OSM 强太多：实测汕头大学 1.5km 内，
    # OSM 只录到 3 家餐厅、便利店 **0 家**；高德分别有 7 家和 11 家，
    # 而且带**真实评分**和人均消费 —— 这正是用户要的东西。
    #
    # ⚠️ 两条路要分开走，这是实测出来的（别再合并回去）：
    #    · **整座城市**里有什么 → place/text（按相关度）。否则天河城这种
    #      离市中心 6 公里的正主永远排不进「最近的 N 个」。
    #    · **某个地点周围**有什么 → place/around（按距离），这才是「附近」。
    city = str(city or "").strip()
    if city and radius >= 8000:
        _amt = _nearby_amap_text(city, cat, limit, lat, lon)
        if _amt:
            return _amt
    _am = _nearby_amap(lat, lon, cat, radius, limit)
    if _am:
        return _am

    # ---- 回退 Overpass（没配 key / 高德失败）----
    q = ('[out:json][timeout:30];('
         'node(around:%d,%.6f,%.6f)%s;'
         'way(around:%d,%.6f,%.6f)%s;'
         'relation(around:%d,%.6f,%.6f)%s;'
         ');out center %d;'
         % (radius, lat, lon, tag, radius, lat, lon, tag,
            radius, lat, lon, tag, limit * 2))
    try:
        d = _overpass(q)
    except Exception as e:
        # 429 很常见，把话说清楚，别让用户以为是"没有结果"
        msg = str(e)
        if "429" in msg:
            msg = "查询太频繁被限流了，歇十几秒再试"
        return {"ok": False, "error": "附近查询失败：%s" % msg}

    items = []
    for el in (d.get("elements") or []):
        t = el.get("tags") or {}
        la = el.get("lat") or (el.get("center") or {}).get("lat")
        lo = el.get("lon") or (el.get("center") or {}).get("lon")
        if la is None or lo is None:
            continue
        dd = haversine(lat, lon, float(la), float(lo))
        if dd > radius:
            continue
        info = _poi_info(t)
        addr = "".join(str(t.get(k) or "") for k in
                       ("addr:province", "addr:city", "addr:district",
                        "addr:street", "addr:housenumber"))
        items.append({
            "name": t.get("name") or "",
            "lat": round(float(la), 6), "lon": round(float(lo), 6),
            "dist_m": int(dd), "kind": cat,
            "addr": addr, "phone": t.get("phone") or t.get("contact:phone") or "",
            "website": t.get("website") or t.get("contact:website") or "",
            "hours": t.get("opening_hours") or "",
            "brand": t.get("brand") or "",
            "info_note": info["info_note"],
        })
    # 只按距离排（没有评分可用，也不该拿记录完整度冒充评分去排序）
    items.sort(key=lambda x: x["dist_m"])
    items = _clean_items(items, cat)
    res = {"ok": True, "category": cat, "radius": radius, "center": [lat, lon],
           "total": len(items), "items": items[:limit],
           "note": "数据来自 OpenStreetMap（志愿者测绘）。中国的小微店铺覆盖很稀疏，"
                   "「查不到」不等于「没有」。这里**没有评分数据**（OSM 不提供评分），"
                   "只给距离和「这条记录全不全」。"}
    return res


# ---------------------------------------------------------------- 交通方式对比与建议
_MODE_CN = {"driving": "驾车", "foot": "步行", "bike": "骑行"}


def suggest_mode(distance_m: float) -> tuple:
    """按距离给出更合适的出行方式 + 原因。返回 (mode, 理由)。"""
    d = float(distance_m or 0) / 1000.0
    if d <= 1.2:
        return "foot", ("才 %.1f 公里，走路最快，还不用找车位" % d) if d > 0.05 else "距离很近，走过去就行"
    if d <= 4.5:
        return "bike", ("%.1f 公里这个距离骑车最划算：比走路快得多，又比开车灵活"
                        "（市区找车位、堵车都省了）" % d)
    if d <= 30:
        return "driving", "%.1f 公里，开车明显更省时间，骑车要花好几倍功夫" % d
    return "driving", "%.1f 公里属于长途，只能开车或坐公共交通" % d


def compare_modes(origin: str, dest: str, allow_net=None) -> dict:
    """把三种出行方式都算一遍（用同一套路网数据），供"该选哪种"的对比。

    ⚠️ 三种方式的**路径都来自驾车路网**（免费 OSRM 只跑 car profile），
       步行/骑行的时间是按速度换算的估算值 —— 结果里带 estimated 标记，
       提交给用户时必须说明，不能让他以为那是真实步行路径。
    """
    out, dist = {}, 0.0
    for m in ("driving", "foot", "bike"):
        r = plan_route(origin, dest, m, allow_net=allow_net)
        if r.get("ok"):
            out[m] = {"distance_m": r["distance_m"], "duration_s": r["duration_s"],
                      "estimated": bool(r.get("estimated"))}
            dist = dist or r["distance_m"]
    if not out:
        return {"ok": False, "error": "三种方式都没算出来（可能是地点没找到或网不通）"}
    best, why = suggest_mode(dist)
    return {"ok": True, "modes": out, "distance_m": dist,
            "suggest": best, "suggest_cn": _MODE_CN.get(best, best),
            "suggest_reason": why,
            "note": "步行/骑行的路径也取自驾车路网，时间按速度估算，仅供参考。"}
