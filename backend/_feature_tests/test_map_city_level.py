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
from backend import tools as T              # noqa: E402

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

# ★ 2026-11-22 修：这一段会**真打实时地图接口**（Overpass 单次 45 秒、重试 3 次），
#   实测把子进程回归拖到 200 秒以上还不结束 —— 而回归是「改完代码反复验证」的必经一步，
#   于是验证被它卡死。回归套件必须快且确定，所以实时网络改成**显式开关**：
#   平时跳过；想真验联网效果时用 MM_LIVE_NET=1 单独跑这个文件。
print("[3] 整城检索（联网 + 有 key + 显式开启 MM_LIVE_NET=1 才测）")
if not (net and has_key and os.environ.get("MM_LIVE_NET") == "1"):
    print("  skip  没网 / 没配高德 key / 没设 MM_LIVE_NET=1（实时接口太慢，不放进回归）")
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
if not (net and has_key and os.environ.get("MM_LIVE_NET") == "1"):
    print("  skip  没网 / 没配高德 key / 没设 MM_LIVE_NET=1（实时接口太慢，不放进回归）")
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

# ---------------------------------------------------------------- 5. 城市校验 + 相关性
# 用户原话：「我想要计划结果一堆不准确的内容」。实测两条根因：
#   ① 高德 place/text 在**目标城市查不到时不返回空，而是退化成全国搜索**
#      —— place_text("正佳广场", city="上海") 返回 20 条广州的。
#   ② 高德对长串查询会**模糊匹配** —— place_text("不存在的假地方xyz") 返回
#      北京 4 家「不存驿站」（只认了前两个字）。
# 另外 Photon 兜底对中国地名是纯模糊匹配（「上海正佳广场」→「上海电影广场」），
# 高德可用时不再走它。
print("[5] 城市校验 + 相关性（不给错的地方）")
ck("_city_ok：广州的结果不属于上海",
   not mt._city_ok({"city": "广州市", "district": "广东省广州市天河区"}, "上海"))
ck("_city_ok：广州的结果属于广州",
   mt._city_ok({"city": "广州市", "district": "广东省广州市天河区"}, "广州"))
ck("_city_ok：给了空城市就都算通过", mt._city_ok({"city": "广州市"}, ""))
ck("_name_relevant：假地名 vs 不存驿站 判不相关",
   not mt._name_relevant("不存在的假地方xyz", {"name": "不存驿站", "addr": "北京市朝阳区"}))
ck("_name_relevant：正佳广场 vs 正大广场 判不相关",
   not mt._name_relevant("正佳广场", {"name": "正大广场", "addr": "上海市浦东新区"}))
ck("_name_relevant：白云机场 vs 白云国际机场 判相关（别名不能误杀）",
   mt._name_relevant("白云机场", {"name": "广州白云国际机场", "addr": ""}))
ck("_name_relevant：含「路/号」的地址类查询不校验",
   mt._name_relevant("天河路208号", {"name": "随便什么", "addr": ""}))

if not (net and has_key and os.environ.get("MM_LIVE_NET") == "1"):
    print("  skip  没网 / 没配高德 key / 没设 MM_LIVE_NET=1（下面几项要联网）")
else:
    ck("假地名返回空（不给「不存驿站」）", not mt.search_place("不存在的假地方xyz", allow_net=True))
    ck("城市不匹配返回空（正佳广场不在上海）",
       not mt.search_place("正佳广场", allow_net=True, city="上海"))
    ck("城市不匹配返回空（上海正佳广场整串）",
       not mt.search_place("上海正佳广场", allow_net=True))
    r = mt.search_place("天河城", allow_net=True, city="天津") or []
    ck("说了天津就给天津的", bool(r) and all("天津" in str(x.get("city") or "") for x in r),
       str([x.get("city") for x in r[:3]]))
    ck("白云机场没被相关性校验误杀",
       bool(mt.search_place("广州白云机场", allow_net=True)))

# ================================================================
# 6. 表外城市（2026-11-18 第二轮）
#    用户原话：「我问广州有什么景点，结果一堆不准确的内容」。
#    查出来城市表只硬编码了 81 个 —— 承德、保定、临沂、漳州全不在里面，
#    city_of() 返回空 → 退化成「以 geocode 结果为中心的 1.5 公里周边」。
#    实测 place="承德" 被定位成「承德站」，只返回火车站附近 4 个景点。
# ================================================================
print("[6] 表外城市 / 城市名校验")
# ① 城市表补全后，纯本地（不联网）就得认出来
for _c in ("承德", "保定", "临沂", "漳州", "襄阳", "张家界"):
    ck("city_of 本地认得出 %s" % _c, mt.city_of(_c, allow_net=False) == _c)
ck("city_of：承德市 → 承德（去掉「市」）", mt.city_of("承德市", allow_net=False) == "承德")
ck("city_of：广州市天河区 → 广州", mt.city_of("广州市天河区", allow_net=False) == "广州")
ck("city_of：上海市浦东新区 → 上海", mt.city_of("上海市浦东新区", allow_net=False) == "上海")
# ② 地点名绝不能当城市（判错了就会把「天河城附近餐厅」变成「天河城全城餐厅」）
for _q in ("天河城", "汕头大学", "广州塔", "正佳广场", "承德避暑山庄", "北京朝阳"):
    ck("city_of 不把地点名当城市：%s" % _q, mt.city_of(_q, allow_net=False) == "")
ck("_city_from_admin_name：河北省承德市 → 承德",
   mt._city_from_admin_name("河北省承德市") == "承德")
ck("_city_from_admin_name：甘肃省酒泉市敦煌市 → 敦煌（县级市取最后一个）",
   mt._city_from_admin_name("甘肃省酒泉市敦煌市") == "敦煌")
ck("_city_from_admin_name：带县的地点名不算城市",
   mt._city_from_admin_name("江西省南昌市进贤县天河城") == "")

if not (net and has_key and os.environ.get("MM_LIVE_NET") == "1"):
    print("  skip  没网 / 没配高德 key / 没设 MM_LIVE_NET=1（下面几项要联网）")
else:
    # ③ 县级市 / 自治州首府：靠 geocode 的「省+市」结构动态判定
    ck("city_of 联网认出县级市：敦煌", mt.city_of("敦煌", allow_net=True) == "敦煌")
    ck("city_of 联网认出州府：香格里拉", mt.city_of("香格里拉", allow_net=True) == "香格里拉")
    # ④ 城市名打错时，高德不返回空而是退回默认城市（实测 → 北京故宫）
    ck("城市名不存在时不返回外地结果",
       not mt._nearby_amap_text("不存在市xyz", "景点", 8, 31.23, 121.47))
    ck("城市名打错时不返回外地结果",
       not mt._nearby_amap_text("广洲", "景点", 8, 31.23, 121.47))
    # ⑤ 端到端：表外城市走**全城**检索，不是 1.5km 周边
    _out = T._do_nearby_places({"place": "承德", "category": "景点"})
    ck("承德走全城检索（不是火车站周边）", "全城搜索" in _out.split("\n")[0],
       _out.split("\n")[0][:60])
    _names = [ln.split("**")[1] for ln in _out.split("\n") if ln.startswith("· **")]
    ck("承德返回的景点 ≥6 条", len(_names) >= 6, str(len(_names)))
    # ⑥ 回归：城市问法走全城、地点问法走周边，不能互相串
    ck("广州仍走全城检索", "全城搜索" in T._do_nearby_places(
        {"place": "广州", "category": "景点"}).split("\n")[0])
    ck("天河城附近仍是周边检索", "周边搜索" in T._do_nearby_places(
        {"place": "天河城", "category": "餐厅"}).split("\n")[0])

print("\n%d 项，失败 %d" % (len(PASS) + len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
