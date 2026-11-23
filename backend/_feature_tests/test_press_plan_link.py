# -*- coding: utf-8 -*-
"""新功能验收断言：压分点 → 改进计划 一键联动（2026-11-22 激活）

原草稿 backend/_feature_tests/_pending_压分点→改进计划一键联动.py 断言
scorepress.link_plan() 存在，但该函数一直没实现；而草稿以 "_" 开头会被
回归跳过 —— 等于这个「功能」长期是空壳。现在实现并转正为正式用例。

约定：不联网、不调 LLM、不落盘真实计划（一律 create=False）。
"""
import os
import sys

_B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _B)
sys.path.insert(0, os.path.dirname(_B))


def run():
    from backend import scorepress as SP

    # ① 结构：三个键都在
    r = SP.link_plan({"file": "backend/handoff.py", "line": 42,
                      "kind": "missing_error_handling"})
    assert isinstance(r, dict), "link_plan 必须返回 dict"
    for k in ("press", "plan", "evidence"):
        assert k in r, "缺键 %s：%r" % (k, r)

    # ② 文件存在 → 必须给出可执行动作
    assert r["plan"] is not None, "文件存在却没给出动作：%r" % (r["evidence"],)
    assert "backend/handoff.py" in (r["plan"].get("files") or []), r["plan"]
    assert r["plan"].get("status") == "pending", "动作必须是待执行状态"

    # ③ 文件不存在 → 明确不联动（不能造出改不动的任务）
    r2 = SP.link_plan({"file": "backend/__nope__.py", "line": 9, "kind": "x"})
    assert r2.get("plan") is None, "文件不存在却给了动作：%r" % (r2,)
    assert r2["evidence"].get("why"), "不联动必须说明原因"

    # ④ 空路径 → 不联动
    r3 = SP.link_plan({})
    assert r3.get("plan") is None, r3

    # ⑤ 批量联动预览不该建计划
    r4 = SP.link_top(3, create=False)
    assert isinstance(r4, dict) and r4.get("ok"), r4
    assert r4.get("created", 0) == 0, "create=False 不应该建计划"

    # ⑥ 接线：必须真被调用方用起来（只 import 不算接线）
    be = _B
    hit = False
    for f in ("main.py", "scoregate.py", "self_iterate.py"):
        p = os.path.join(be, f)
        if not os.path.isfile(p):
            continue
        s = open(p, encoding="utf-8", errors="ignore").read()
        if "link_top(" in s or "link_plan(" in s:
            hit = True
            break
    assert hit, "link_plan / link_top 没有任何调用方（功能等于没接上）"

    print("press_plan_link OK")


if __name__ == "__main__":
    # ⚠️ 2026-11-23 补：原来只有 def run()，没有调用入口 ——
    #   直接跑这个文件 rc=0 但一条断言都没执行（静默通过）。补上入口。
    run()
