# -*- coding: utf-8 -*-
"""回归：结果卡片（天气/地图/文档/图片）必须**真的发到前端**。

真实故障（2026-11-24 用户报「卡片功能重启后看不出和原来有什么区别」）：
  前端 livecard.js 的渲染器、app.js 的 mountResultCards 接线都写好了，
  但后端**从来没有人调用** livecard_meta.cards_for_tool ——
  对话流里一个 {"cards":[...]} 都没下发过，前端那段代码永远走不到。
  所以「重启了、代码是新的、界面毫无变化」。

本测试锁住三件事：
  ① 两条通道（本地 Ollama / 云端 DeepSeek）的流里都真的有 cards 下发；
  ② 合并去重口径（同 type+title 只留一张，上限 12）；
  ③ 返回值里的中文前缀不能被当成路径（否则文档卡点开是空的）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass


def run() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    import backend.main as M
    from backend import livecard_meta as LM

    # ① 两条通道的接线（结构检查：这个字段一被删，用户就又会"看不出变化"）
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    ck("本地通道下发 cards", '{"cards": _round_cards' in src)
    ck("云端通道下发 cards", '{"cards": _cloud_cards' in src)
    ck("两条通道都调了 cards_for_tool", src.count("cards_for_tool(") >= 2,
       "count=%d" % src.count("cards_for_tool("))

    # ② 合并去重
    a = [{"type": "weather", "title": "广州 25℃"}, {"type": "map", "title": "A→B"}]
    b = [{"type": "weather", "title": "广州 25℃"}, {"type": "doc", "title": "报告.docx"}]
    got = M._merge_live_cards(a, b)
    ck("同 type+title 去重", len(got) == 3, str([c["title"] for c in got]))
    ck("合并上限 12", len(M._merge_live_cards(
        [{"type": "doc", "title": "t%d" % i} for i in range(20)], [])) == 12)
    ck("空输入不炸", M._merge_live_cards(None, None) == [])

    # ③ 五类工具都要出卡（0 张 = 前端没东西可画）
    cases = [
        ("get_weather", {"city": "广州"}, "广州 25℃ 晴", "weather"),
        ("map_plan", {"from": "广州塔", "to": "白云机场"}, "全程 40 公里", "map"),
        ("make_docx", {}, "已生成文档：D:\\out\\项目周报.docx", "doc"),
        ("generate_image", {}, "图片已保存：D:\\pics\\cat.png", "image"),
        ("make_pptx", {}, "已生成 PPT：D:\\out\\slides.pptx", "ppt"),
    ]
    for name, args, res, kind in cases:
        cards = LM.cards_for_tool(name, args, res, None)
        ck("%s → %s 卡" % (name, kind),
           bool(cards) and cards[0].get("type") == kind,
           str(cards[0].get("type") if cards else "无"))

    # ④ 路径必须干净（中文前缀会被前端当文件名，打开必然失败）
    for res, want in [
        ("已生成文档：D:\\out\\项目周报.docx", "D:\\out\\项目周报.docx"),
        ("文档已保存到 C:/Users/a/报告.docx（共 3 页）", "C:/Users/a/报告.docx"),
    ]:
        c = LM.cards_for_tool("make_docx", {}, res, None)
        p = (c[0]["payload"].get("path") if c else "")
        ck("路径剥掉中文前缀", p == want, repr(p))

    # ⑤ 纯文字结果不该硬造卡
    ck("普通搜索不出卡",
       LM.cards_for_tool("web_search", {"query": "x"}, "一堆文字", None) == [])

    print("\n通过（失败 %d 项）" % len(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
