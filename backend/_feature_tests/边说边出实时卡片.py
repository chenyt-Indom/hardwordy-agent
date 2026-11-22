"""新功能验收断言：边说边出实时卡片（工具跑完立刻给一张可点的卡）

原来这份断言调 livecard_meta.build_card_meta —— 那个函数不存在（真实是 cards_for_tool），
而且按「卡片类型名」调用（真实是按「工具名」分派），所以永远失败、从没被执行过。
现按真实契约重写，并额外守住一条实测踩过的坑：
图片工具必须出 image 卡，不能被压成 doc（原实现 kind in ("", "image") -> "doc"）。
"""
import os as _os
import sys as _sys

_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))


def run():
    from backend import livecard_meta

    # 工具名 → 期望卡片类型（对应 livecard_meta 里 _WEATHER_TOOLS / _MAP_TOOLS / …）
    cases = [
        ("get_weather", {"city": "杭州"}, None, "weather"),
        ("map_plan", {"from": "杭州", "to": "上海"}, None, "map"),
        ("nearby_places", {"place": "杭州", "category": "餐厅"}, None, "map"),
        ("generate_image", {"prompt": "猫"}, {"path": "a.png"}, "image"),
        ("web_image_search", {"query": "猫"}, {"path": "a.jpg"}, "image"),
        ("make_docx", {"title": "报告"}, {"path": "a.docx"}, "doc"),
        ("make_pptx", {"title": "汇报"}, {"path": "a.pptx"}, "ppt"),
        ("video_edit", {}, {"path": "a.mp4"}, "video"),
    ]
    for tool, args, result, want in cases:
        cards = livecard_meta.cards_for_tool(tool, args, result)
        assert isinstance(cards, list) and cards, "%s 没产出卡片" % tool
        card = cards[0]
        for k in ("type", "title", "thumb", "payload"):
            assert k in card, "%s 卡片缺字段 %s: %r" % (tool, k, card)
        assert isinstance(card["payload"], dict), "%s payload 不是 dict" % tool
        assert card["type"] == want, "%s 卡片类型是 %s，应为 %s" % (tool, card["type"], want)

    # 回归守卫：图片工具不能被降级成 doc
    for tool in ("generate_image", "web_image_search", "cutout_image"):
        cards = livecard_meta.cards_for_tool(tool, {"prompt": "x"}, {"path": "x.png"})
        got = cards[0]["type"] if cards else None
        assert cards and got == "image", "%s 被降级成 %s" % (tool, got)

    print("livecard ok")
