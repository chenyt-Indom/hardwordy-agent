# -*- coding: utf-8 -*-
"""地图检索回归测试（2026-11-18）。

钉住三件实测踩过的事（都是用户原话反馈出来的）：
  1. 「问广州有什么商场，连最大的天河城都没有」
     → 整座城市检索必须走 place/text（按相关度），不能走 place/around（按距离）。
  2. 「同一地方重复出现、根本不是景点」
     → 同一景区的子点位（「人民公园-宫灯帏」）只留父名；停车场/售票处/住宅小区要滤掉。
  3. 「地点跑错地方（广州的点标到外省）」
     → 模型没填 city 时，从「广州天河城」这种地名里自动认出广州。

联网相关的那几项：没网 / 没配高德 key 时自动跳过（不判失败）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import map_tools as mt          # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  ← " + detail) if detail and not cond else ""))


# ---------------------------------------------------------------- 1. 城市名识别
print("[1] city_of / 整城 vs 周边")
ck("city_of('广州市') == 广州", mt.city_of("广州市") == "广州", mt.city_of("广州市"))
ck("city_of('广州') == 广州", mt.city_of("广州") == "广州", mt.city_of("广州"))
ck("city_of('广州塔') 为空", mt.city_of("广州塔") == "", mt.city_of("广州塔"))
ck("city_of('汕头大学') 为空", mt.city_of("汕头大学") == "", mt.city_of("汕头大学"))

# ---------------------------------------------------------------- 2. 去噪去重
print("[2] _clean_items：去重 + 去噪")
items = [
    {"name": "人民公园", "lat": 23.13, "lon": 113.26, "kind": "公园"},
    {"name": "人民公园-宫灯帏", "lat": 23.1301, "lon": 113.2601, "kind": "公园"},
    {"name": "人民公园-新娘像", "lat": 23.1302, "lon": 113.2602, "kind": "公园"},
    {"name": "人民公园停车场", "lat": 23.1303, "lon": 113.2603, "kind": "停车场"},
    {"name": "某小区", "lat": 23.14, "lon": 113.27, "kind": "住宅小区"},
    {"name": "越秀公园", "lat": 23.14, "lon": 113.27, "kind": "公园"},
]
out = mt._clean_items(items, "景点")
names = [x["name"] for x in out]
ck("子点位被归并（宫灯帏/新娘像消失）",
   "人民公园-宫灯帏" not in names and "人民公园-新娘像" not in names, str(names))
ck("父名保留", "人民公园" in names, str(names))
ck("停车场被滤掉", "人民公园停车场" not in names, str(names))
ck("住宅小区被滤掉", "某小区" not in names, str(names))
ck("不同地点保留", "越秀公园" in names, str(names))

# ---------------------------------------------------------------- 3. 联网部分
net = mt.online()
has_key = False
try:
    from backend import amap
    has_key = bool(amap.has_key())
except Exception:
    has_key = False

print("[3] 整城检索（联网+有 key 才测）")
if not (net and has_key):
    print("  skip  没网或没配高德 key")
else:
    c = mt.geocode_one("广州市", allow_net=True)
    ck("广州市能定位", bool(c))
    if c:
        r = mt.nearby(c["lat"], c["lon"], "商场", radius=20000, limit=8,
                      allow_net=True, city="广州")
        names = [x["name"] for x in (r.get("items") or [])]
        ck("走的是整城检索", r.get("level") == "city", str(r.get("level")))
        ck("清单不超过 8 条", len(names) <= 8, str(len(names)))
        ck("含天河城", any("天河城" in n for n in names), str(names))
        ck("没有停车场/管理办公室这类噪声",
           not any(("停车场" in n or "管理办公室" in n or "售票处" in n) for n in names),
           str(names))

        # 局部周边不能被改成整城检索
        r2 = mt.nearby(23.35, 116.68, "餐厅", radius=1500, limit=5, allow_net=True)
        ck("局部周边仍是距离检索", r2.get("level") != "city", str(r2.get("level")))

# ---------------------------------------------------------------- 4. 裸地名
# 用户原话：「如果你只打「天河城」三个字、上下文里没提城市，仍可能解析到外省」。
# 根因：/place/text 不带 city 恒返回 0 条 → 落到 /geocode（按地址解析）→
#       命中"江西省南昌市进贤县天河城"（那边真有个同名村子）。
# 改走 /assistant/inputtips（高德全国相关度）：实测「天河城」第 3 条就是广州天河区那个。
print("[4] 裸地名（不给城市）不能跑外省")
if not (net and has_key):
    print("  skip  没网或没配高德 key")
else:
    g = mt.geocode_one("天河城", allow_net=True)
    ck("「天河城」能定位", bool(g))
    if g:
        ck("「天河城」落在广东（不是江西）",
           "广东" in str(g.get("district") or "") or "广州" in str(g.get("city") or ""),
           str(g.get("district")))
        ck("「天河城」经度接近 113.32",
           abs(float(g.get("lon") or 0) - 113.32) < 0.2, str(g.get("lon")))
    g2 = mt.geocode_one("正佳广场", allow_net=True)
    ck("「正佳广场」落在广州",
       bool(g2) and ("广州" in str(g2.get("district") or "") or "广州" in str(g2.get("city") or "")),
       str((g2 or {}).get("district")))
    # 带城市限定时必须尊重用户说的城市（惠州西湖 ≠ 杭州西湖）
    g3 = mt.geocode_one("西湖", city="惠州", allow_net=True)
    ck("给了城市就按城市查（惠州西湖）",
       bool(g3) and "惠州" in (str(g3.get("name") or "") + str(g3.get("district") or "")),
       str((g3 or {}).get("name")))
    # 地址类查询不能被打乱（仍应走地址解析）
    g4 = mt.geocode_one("天河路208号", allow_net=True)
    ck("地址类查询仍能解析",
       bool(g4) and "天河路" in str(g4.get("name") or ""), str((g4 or {}).get("name")))

print("\n%d 项，失败 %d" % (len(PASS) + len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
