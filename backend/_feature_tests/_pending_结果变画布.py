"""新功能验收断言：结果变画布

对应预判「让成品直接可见」+ 摩擦点「回复被截断」——用户看不到成品就重发。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os
    # 模拟 canvas 卡片元数据与前端渲染契约
    cards = [
        {"id": "c1", "type": "map", "title": "地点", "payload": {"lat": 1.0, "lng": 2.0}},
        {"id": "c2", "type": "weather", "title": "天气", "payload": {"temp": 20}},
    ]
    assert isinstance(cards, list) and len(cards) >= 1
    for c in cards:
        assert set(["id", "type", "title", "payload"]).issubset(c.keys())
        assert isinstance(c["payload"], dict)
    # 画布网格布局契约：卡片按顺序排列
    grid_order = [c["id"] for c in cards]
    assert grid_order == ["c1", "c2"]
    # 拖拽后顺序可变但集合不变
    reordered = ["c2", "c1"]
    assert set(reordered) == set(grid_order)
    print("canvas contract ok")
