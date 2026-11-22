# -*- coding: utf-8 -*-
"""体检 / 面板三个接口的**手动冒烟脚本**（不是自动化断言，不影响启动）。

用法::

    py -3 backend/_tmp_api_check.py

为什么单独跑一遍（2026-11-22「safe_get 兜底取字段，避免 KeyError」）：
    这几个接口改成逐字段 safe_get 之后，最坏的情况是「值被降级成默认值但接口
    照样 200」—— 只跑单元级的 safe_get 自检看不出来，必须真调一次接口，
    确认 items / success_rate 这些关键字段还在、没有被误判成缺失。
"""
from __future__ import annotations

_ENDPOINTS = (
    ("/api/iter/health", "iter_health", {"days": 7}),
    ("/api/iter/panel", "iter_panel", {"days": 7}),
    ("/api/selfiter/health", "selfiter_health", {}),
)


def _smoke() -> int:
    """依次调三个接口，返回失败条数（0 = 全通过）。"""
    import json

    from backend import main as _M

    fails = []
    for label, fn_name, kw in _ENDPOINTS:
        fn = getattr(_M, fn_name, None)
        if fn is None:                                   # 接线路由被删了
            fails.append("%s：main.%s 不存在" % (label, fn_name))
            continue
        try:
            r = fn(**kw)
        except Exception as e:                           # noqa: BLE001
            fails.append("%s：抛异常 %s: %s" % (label, type(e).__name__, e))
            continue
        if not isinstance(r, dict):
            fails.append("%s：返回的不是 dict（%s）" % (label, type(r).__name__))
            continue
        if r.get("ok") is False:
            fails.append("%s：ok=False %r" % (label, r.get("error")))
            continue
        print("%-22s ok  keys=%s" % (label, ",".join(sorted(r.keys()))))
        print("   ", json.dumps(r, ensure_ascii=False)[:200])
    # 体检接口的关键字段不能因为兜底而整体消失
    try:
        h = _M.iter_health(days=7)
        if not isinstance(h.get("items"), list) or len(h["items"]) < 5:
            fails.append("iter_health 的 items 不足 5 项：%r"
                         % (h.get("items") is not None))
        if not h.get("verdict"):
            fails.append("iter_health 的 verdict 被降级成空串")
    except Exception as e:                               # noqa: BLE001
        fails.append("iter_health 复查失败：%s: %s" % (type(e).__name__, e))
    print("接口冒烟：%s" % ("全部通过" if not fails else "失败 %d 条" % len(fails)))
    for f in fails:
        print("  ✗", f)
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(1 if _smoke() else 0)
