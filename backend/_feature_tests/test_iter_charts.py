# -*- coding: utf-8 -*-
"""回归：自迭代图表（2026-11-25 用户要求「自迭代加入图表」）。

用户原话：「自迭代是没有加入图表吗？」—— 查下来确实没有：后端提示词的
JSON schema 里没有图表字段，前端按字段手工拼卡片、也不走 markdown 渲染管线，
所以模型想画也没地方输出、面板收到 ```viz 也不会渲染。

本测试锁住四件事：
  ① _clean_charts 只放行合法结构（脏数据不许把前端 _vizHtml 搞崩）；
  ② 编造的数字必须标 unverified，材料里真实出现过的数字不许被误标；
  ③ 数量上限（最多 3 张、每张最多 8 点、少于 2 点丢弃）；
  ④ 接线：提示词要求 charts、plan 存 charts、前端有图表区且真的被 render 调用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    from backend import self_iterate as SI

    corpus = "真实运行指标：成功率 66%，共 42 条动作，失败 7 次。"

    # ① 合法图 + 真实数字
    good = [{"title": "动作", "type": "bar", "unit": "条",
             "data": [{"label": "提出", "value": 42},
                      {"label": "失败", "value": 7}]}]
    o1 = SI._clean_charts(good, corpus)
    ck("合法图被保留", len(o1) == 1, str(o1))
    ck("真实数字不误标未核实",
       bool(o1) and o1[0]["unverified"] is False,
       str(o1[0].get("miss") if o1 else ""))
    ck("字段原样保留", bool(o1) and o1[0]["title"] == "动作"
       and o1[0]["unit"] == "条" and o1[0]["type"] == "bar")

    # ② 编造的数字必须标出来
    fake = [{"title": "编的", "type": "bar",
             "data": [{"label": "A", "value": 999},
                      {"label": "B", "value": 888}]}]
    o2 = SI._clean_charts(fake, corpus)
    ck("编造数字被标未核实", bool(o2) and o2[0]["unverified"] is True, str(o2))
    ck("标出具体是哪几项",
       bool(o2) and set(o2[0]["miss"]) == {"A", "B"},
       str(o2[0].get("miss") if o2 else ""))
    # 材料为空时不做判断（不能冤枉模型）
    o3 = SI._clean_charts(fake, "")
    ck("材料为空 → 不冤枉模型",
       bool(o3) and o3[0]["unverified"] is False, str(o3))

    # ③ 脏结构全部丢弃
    bad = [
        None, "x", 3, {},
        {"data": [{"label": "A", "value": 1}]},                  # 只有 1 点
        {"data": [{"label": "A", "value": "abc"},
                  {"label": "B", "value": 2}]},                  # 非法值 → 剩 1 点
        {"data": [{"label": "A", "value": float("nan")},
                  {"label": "B", "value": 2}]},                  # NaN → 剩 1 点
        {"data": "not-a-list"},
        {"data": [{"label": "A", "value": float("inf")},
                  {"label": "B", "value": 2}]},                  # inf → 剩 1 点
    ]
    o4 = SI._clean_charts(bad, corpus)
    ck("脏结构全部丢弃", o4 == [], str(o4))
    ck("非 list → 空", SI._clean_charts("nope", corpus) == [])
    ck("None → 空", SI._clean_charts(None, corpus) == [])

    # 类型白名单
    o6 = SI._clean_charts([{"type": "weird",
                            "data": [{"label": "A", "value": 42},
                                     {"label": "B", "value": 7}]}], corpus)
    ck("未知类型降级成 bar", bool(o6) and o6[0]["type"] == "bar", str(o6))

    # 上限
    many = [{"data": [{"label": str(i), "value": i} for i in range(10)]}
            for _ in range(5)]
    o7 = SI._clean_charts(many, corpus)
    ck("最多 3 张图", len(o7) == 3, str(len(o7)))
    ck("每张最多 8 点", bool(o7) and len(o7[0]["data"]) == 8,
       str(len(o7[0]["data"]) if o7 else ""))

    # 字符串数字可转
    o8 = SI._clean_charts([{"data": [{"label": "A", "value": "42"},
                                     {"label": "B", "value": "7"}]}], corpus)
    ck("字符串数字能转成数值",
       bool(o8) and o8[0]["data"][0]["value"] == 42.0, str(o8))

    # ④ 接线检查（防止「改了函数但没人调用」这种假落地）
    src = open(os.path.join(ROOT, "backend", "self_iterate.py"),
               encoding="utf-8").read()
    ck("提示词里有 charts 字段说明",
       '"charts": [{"title":"图标题"' in src)
    ck("plan 里真的存了 charts",
       '"charts": _clean_charts(data.get("charts"), prompt),' in src)

    # ★ 本轮真实踩到的坑：一开始写的是 _clean_charts(..., _prompt)，而 _prompt
    #   是 _build_reflect_prompt 的局部变量 —— 在 _reflect_impl 里是未定义名字，
    #   真跑起来会 NameError 把整条反思链路打断。用 AST 把这类错钉死。
    import ast as _ast
    _tree = _ast.parse(src)
    _impl = None
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.FunctionDef) and _n.name == "_reflect_impl":
            _impl = _n
            break
    ck("找到 _reflect_impl", _impl is not None)
    if _impl is not None:
        _bad = [x.lineno for x in _ast.walk(_impl)
                if isinstance(x, _ast.Name) and x.id == "_prompt"]
        ck("_reflect_impl 里不许引用未定义的 _prompt", not _bad, str(_bad))
        _stores = {x.id for x in _ast.walk(_impl)
                   if isinstance(x, _ast.Name) and isinstance(x.ctx, _ast.Store)}
        ck("_clean_charts 的 corpus 用的是本函数已赋值的 prompt", "prompt" in _stores)

    app = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
    ck("前端有 chartBlockHtml", "function chartBlockHtml(d, p)" in app)
    ck("render 里真的调用了它", "h += chartBlockHtml(d, p);" in app)
    ck("模型图有未核实提示", "iter-chart-warn" in app)
    ck("图表走的是现有 _vizHtml 渲染器", "_vizHtml(JSON.stringify(spec))" in app)

    css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
    ck("样式里有 .iter-charts", ".iter-charts" in css)
    ck("样式里有 .iter-chart-warn", ".iter-chart-warn" in css)

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    if fails:
        print("失败项：" + "、".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
