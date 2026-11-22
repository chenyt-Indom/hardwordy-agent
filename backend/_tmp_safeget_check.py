# -*- coding: utf-8 -*-
"""jsonfix.safe_get 的**手动自检**脚本（不是自动化断言，不影响启动）。

用法（改完 jsonfix / iterpanel / main 的取字段逻辑后手跑一遍即可）::

    py -3 backend/_tmp_safeget_check.py

为什么留这么一个脚本（2026-11-22「safe_get 兜底取字段，避免 KeyError」）：
    这个改动最怕的不是报错，而是**静悄悄地降级** —— safe_get 一旦写错（比如把
    存在但值为 None 的键当成缺失、或者把 0/False/"" 当成缺失），面板会显示
    「暂无」而不是报错，看日志根本看不出来。所以把语义固化成可执行的自检：
    缺字段返回默认值 + 落 field_miss 事件；存在字段原样返回（含 0/False/空串）。
"""
from __future__ import annotations

import json


def _selftest() -> int:
    """跑一遍 safe_get 的语义自检，返回失败条数（0 = 全通过）。"""
    from backend import jsonfix, actlog

    fails = []

    def chk(name, got, want):
        if got != want:
            fails.append("%s：得到 %r，期望 %r" % (name, got, want))

    o = {"a": {"b": 1}}
    chk("缺字段返回 None", jsonfix.safe_get(o, "a.c"), None)
    chk("存在字段返回本体", jsonfix.safe_get(o, "a.b"), 1)
    chk("缺字段用默认值", jsonfix.safe_get(o, "a.c", 0), 0)
    chk("列表下标", jsonfix.safe_get({"a": [{"b": 2}]}, "a.0.b"), 2)
    chk("非容器兜底", jsonfix.safe_get(None, "x.y", "dflt"), "dflt")
    chk("存在但为 None 不算缺失",
        jsonfix.safe_get({"a": None}, "a", "dflt"), None)
    chk("0 不算缺失", jsonfix.safe_get({"n": 0}, "n", 9), 0)
    chk("空串不算缺失", jsonfix.safe_get({"s": ""}, "s", "x"), "")
    chk("键名大小写不敏感", jsonfix.safe_get({"Title": "t"}, "title"), "t")
    chk("safe_list 非 list 当缺失", jsonfix.safe_list({"items": {}}, "items"), [])
    chk("safe_dict 非 dict 当缺失", jsonfix.safe_dict({"d": []}, "d"), {})

    # field_miss 事件必须真的落到 actlog（带「实际可用 key 列表」）
    jsonfix.safe_get({"a": {"b": 1}}, "a.zzz_missing", where="selftest.safe_get")
    watch = [e for e in actlog.list_events()
             if isinstance(e.get("w"), dict) and e["w"].get("kind") == "field_miss"]
    if not watch:
        fails.append("actlog 里没有 field_miss 事件")
    else:
        note = str(watch[-1]["w"].get("note") or "")
        if "b" not in note:
            fails.append("field_miss 事件没带可用 key 列表：%r" % note)

    print("safe_get 自检：%s" % ("全部通过" if not fails else "失败 %d 条" % len(fails)))
    for f in fails:
        print("  ✗", f)
    print("最近一条 field_miss：",
          json.dumps(watch[-1], ensure_ascii=False) if watch else "（无）")
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
