# -*- coding: utf-8 -*-
"""临时自检（跑完即删）：backend/livecard_meta.build 的真实行为。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import livecard_meta as m          # noqa: E402

cases = [
    ("get_weather", {"city": "杭州"},
     "【杭州 天气】\n此刻实况：晴 25℃\n逐日预报：\n明天 多云 19~28℃\n坐标 30.25,120.16", "weather"),
    ("map_plan", {"from": "杭州", "to": "上海"}, None, "map"),
    ("nearby_places", {"place": "杭州", "category": "餐厅"}, None, "map"),
    ("make_docx", {"title": "报告"}, "已生成 out/a.docx", "doc"),
    ("make_pptx", {"title": "汇报"}, "已生成 out/a.pptx", "ppt"),
    ("generate_image", {"prompt": "猫"}, "saved out/a.png", "image"),
    ("video_edit", {"path": "a.mp4"}, "done a.mp4", "video"),
]
for tool, args, res, want in cases:
    card = m.build(tool, args, res)
    assert isinstance(card, dict), "%s build 没返回 dict：%r" % (tool, card)
    for k in ("type", "title", "thumb", "payload"):
        assert k in card, "%s 卡片缺字段 %s：%r" % (tool, k, card)
    assert isinstance(card["payload"], dict), "%s payload 不是 dict" % tool
    assert card["type"] == want, "%s 出的是 %s，应为 %s" % (tool, card["type"], want)
    assert card["title"], "%s 标题为空" % tool

# 按卡片类型直接造卡：payload 原样保留（前端 render 要吃 days / markers / pages）
days = [{"date": "d%d" % i, "high": 5 + i, "low": -3 + i} for i in range(5)]
c = m.build("weather", {"city": "北京", "days": days})
assert c["type"] == "weather" and len(c["payload"]["days"]) == 5, c
d = m.build("doc", {"pages": 12, "thumb": "x.png", "title": "t"})
assert d["type"] == "doc" and d["payload"]["pages"] == 12, d
assert d["thumb"] == "x.png", d
i2 = m.build("image", {"path": "a.png", "width": 1024})
assert i2["type"] == "image" and i2["payload"]["path"] == "a.png", i2
mp = m.build("map", {"markers": [{"name": "西湖", "lat": 30.2, "lon": 120.1}]})
assert mp["type"] == "map" and mp["payload"]["markers"], mp

# 没东西可画 → None（不是空壳卡）；异常/怪参数不能抛
assert m.build("no_such_tool", {}, "nothing") is None
assert m.build(None) is None
assert m.build("get_weather", None, None) is not None
assert isinstance(m.build_all("get_weather", {"city": "杭州"}, None), list)

# UI 事件通道不能被改坏
ev = [{"type": "map", "center": [30.2, 120.1], "markers": [{"name": "西湖", "lat": 30.2, "lon": 120.1}],
       "route": {"from": "西湖", "to": "灵隐寺", "distance": "6.2km", "points": [1, 2, 3]}}]
c2 = m.build("map_plan", {}, None, ev)
assert c2["type"] == "map" and "route" in c2["payload"], c2

print("livecard_meta.build ok")
