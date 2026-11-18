# -*- coding: utf-8 -*-
"""临时验证：高德地图改动端到端（跑完即删）"""
import os, sys, io, json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend import map_tools as M

def show(title, items, n=5):
    print("=" * 60)
    print(title, "| 共", len(items) if hasattr(items, "__len__") else "?", "条")
    for it in (items or [])[:n]:
        if isinstance(it, dict):
            name = it.get("name") or it.get("title")
            loc = it.get("location") or it.get("address") or ""
            city = it.get("city") or it.get("district") or ""
            rating = it.get("rating") or it.get("biz_ext") or ""
            print("   -", name, "|", city, "|", loc, "|", rating)
        else:
            print("   -", it)

# 1) 裸地名（不给城市）
for q in ["天河城", "正佳广场", "西湖"]:
    try:
        r = M.search_place(q, None)
        items = r.get("items", []) if isinstance(r, dict) else r
        show("裸地名: " + q, items, 3)
    except Exception as e:
        print("裸地名", q, "异常:", type(e).__name__, e)

# 2) 城市级「有什么」
for city, kw in [("广州", "商场"), ("广州", "景点")]:
    try:
        r = M.nearby(city, kw, radius=30000)
        items = r.get("items", []) if isinstance(r, dict) else r
        show("城市级: %s %s" % (city, kw), items, 8)
    except Exception as e:
        print("城市级", city, kw, "异常:", type(e).__name__, e)

# 3) 局部周边（应保持按距离）
try:
    r = M.nearby("汕头大学", "餐厅", radius=1500)
    items = r.get("items", []) if isinstance(r, dict) else r
    show("局部: 汕头大学 1500m 餐厅", items, 5)
except Exception as e:
    print("局部异常:", type(e).__name__, e)
