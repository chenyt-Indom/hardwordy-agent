# -*- coding: utf-8 -*-
"""语音画布「指哪改哪」回归测试。

背景（2026-11-18 实测踩到两个误判）：
  ① 单字「标」把「标题再短点」判成改色指令 —— 用户想语义改写，却被当成改色
     拦下，连对话链路都走不了；
  ② 「去掉背景」被判成删卡片 —— 一句话就把卡片删了，用户得重新生成才有。
两处都改成「认不准就不认」，交回对话链路。

另外钉住三条前端依赖的契约：
  · ok=True  → 前端就地重画（改色 / 删除 / 合并）；
  · ok=False 且 action 非空 → 前端弹提示、**不**再丢给模型（是画布指令但指不到）；
  · action 为空 → 前端照旧走对话（不是画布指令）。

跑法：python backend/_feature_tests/test_canvas_command.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import canvas

_ok = 0
_fail = 0


def check(name, cond, extra=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   " + name)
    else:
        _fail += 1
        print("  FAIL " + name + ("  " + str(extra) if extra else ""))


def _cards():
    return [
        {"id": "cv0", "type": "image", "content": "![鼓浪屿](x.png)"},
        {"id": "cv1", "type": "text", "content": "第二天：南澳岛"},
        {"id": "cv2", "type": "text", "content": "预算合计 4786 元"},
    ]


def main():
    print("=" * 60)
    print("语音画布 canvas：detect_action / resolve_targets / handle")
    print("=" * 60)

    # ---------- 1) 误判防护：不是画布指令的必须认不出 ----------
    print("\n[1] 不该被当成画布指令的句子（认不出就交回对话）")
    for t in ["标题再短点", "调整一下顺序", "换成第二天的说法", "把这段改得口语一点",
              "把这张图的背景去掉", "删掉水印", "标注一下重点"]:
        check("detect_action(%r) == ''" % t, canvas.detect_action(t) == "",
              canvas.detect_action(t))

    # ---------- 2) 正例：三类动作都要认对 ----------
    print("\n[2] 该认出来的三类动作")
    check("改色-明说颜色", canvas.detect_action("把第一张改成红色") == "recolor")
    check("改色-带色字", canvas.detect_action("改个颜色") == "recolor")
    check("改色-换个色", canvas.detect_action("换个色") == "recolor")
    check("删除", canvas.detect_action("删掉最后一张") == "delete")
    check("合并", canvas.detect_action("合并前两张") == "merge")
    check("合并优先于改色", canvas.detect_action("把两张并成一张红色的") == "merge")

    # ---------- 3) 指认方式 ----------
    print("\n[3] 六种指认卡片的方式")
    cs = _cards()
    check("序号「第一张」", canvas.resolve_targets("把第一张改成红色", cs)[0] == ["cv0"])
    check("卡号 cv2", canvas.resolve_targets("把 cv2 删掉", cs)[0] == ["cv2"])
    check("位置「前两张」", canvas.resolve_targets("合并前两张", cs)[0] == ["cv0", "cv1"])
    check("位置「最后一张」", canvas.resolve_targets("删掉最后一张", cs)[0] == ["cv2"])
    check("全部", sorted(canvas.resolve_targets("全部删掉", cs)[0]) == ["cv0", "cv1", "cv2"])
    check("引号原文", canvas.resolve_targets("把「南澳岛」那张删掉", cs)[0] == ["cv1"])
    check("圈选兜底", canvas.resolve_targets("把它改成红色", cs, selected="cv1")[0] == ["cv1"])

    # ---------- 4) 指不到就不猜（这是唯一不能容忍的失败方式）----------
    print("\n[4] 指不到卡片时：报错、不默认全部")
    ids, err = canvas.resolve_targets("改成红色", cs, selected="")
    check("ids 为空", ids == [], ids)
    check("err 是人话", bool(err) and "哪张" in err, err)
    d = canvas.handle("改成红色", cs, selected="")
    check("handle ok=False", d.get("ok") is False)
    check("handle action 非空（前端据此弹提示、不丢给模型）", d.get("action") == "recolor", d.get("action"))
    check("卡片一张没动", d.get("cards") == cs)

    # ---------- 5) 执行结果 ----------
    print("\n[5] 改色 / 删除 / 合并的实际结果")
    d = canvas.handle("把第一张改成红色", cs)
    check("改色 ok", d.get("ok") is True)
    check("改色只动 cv0", d.get("card_ids") == ["cv0"], d.get("card_ids"))
    check("改色写进卡片", d["cards"][0].get("color") == "#e5534b", d["cards"][0].get("color"))
    check("其余卡片颜色未变", "color" not in d["cards"][1] or not d["cards"][1].get("color"))

    d = canvas.handle("删掉最后一张", cs)
    check("删除后只剩两张", len(d["cards"]) == 2, len(d["cards"]))
    check("删掉的是 cv2", [c["id"] for c in d["cards"]] == ["cv0", "cv1"])

    d = canvas.handle("合并前两张", cs)
    check("合并后只剩两张", len(d["cards"]) == 2, len(d["cards"]))
    check("合并结果占用原位置（id 不变，前端 data-card-id 还对得上）",
          d["cards"][0]["id"] == "cv0", d["cards"][0]["id"])
    check("合并正文含两块内容",
          "鼓浪屿" in d["cards"][0]["content"] and "南澳岛" in d["cards"][0]["content"])

    # ---------- 6) 不就地改入参（前端要能撤销 / 重放）----------
    print("\n[6] 入参不被就地修改")
    cs = _cards()
    snapshot = [dict(c) for c in cs]
    canvas.handle("把第一张改成红色", cs)
    canvas.handle("删掉最后一张", cs)
    canvas.handle("合并前两张", cs)
    check("调用三次后入参完全没变", cs == snapshot, cs)

    # ---------- 7) 边界 ----------
    print("\n[7] 边界")
    d = canvas.handle("", cs)
    check("空话不崩、ok=False", d.get("ok") is False and d.get("action") == "")
    d = canvas.handle("合并前两张", [{"id": "cv0", "type": "text", "content": "只有一张"}])
    check("合并单张 → 报错而不是硬合", d.get("ok") is False and "两张" in (d.get("error") or ""))
    d = canvas.handle("把 cv9 删掉", cs)
    check("指到不存在的卡 → 报错", d.get("ok") is False and "cv9" in (d.get("error") or ""))
    d = canvas.handle("改个颜色", cs, selected="cv1")
    check("说了改色但没说颜色 → 报错不瞎猜", d.get("ok") is False and "颜色" in (d.get("error") or ""))

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (_ok, _fail))
    print("=" * 60)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
