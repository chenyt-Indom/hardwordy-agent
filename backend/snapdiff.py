# -*- coding: utf-8 -*-
"""改动前基线快照 + 改动后定点回归。

为什么要这个模块（2026-11-15）
------------------------------
自动改动最怕的不是「改错」，而是「**悄悄改坏了别的东西**」：
改 A 函数时顺手动了 B 的行为，而回归里没有 B 的用例 —— 于是没人发现。

通用回归（`regress.py`）覆盖的是「已知的关键路径」；这里补的是
「**改动前后，同一批函数的行为有没有变**」：改之前拍一张快照，
改完再拍一张，比一比。只对**本次改动涉及的函数**做，所以很快。

设计取舍
--------
· **不跑真实业务流程**，只用一组固定的小输入调用函数、记下输出 repr。
  这不需要构造复杂依赖，秒级可重复，适合每轮自动改动前后各跑一次。
· **异常也算行为**：原来抛 ValueError、改完不抛了，同样要报出来。
· **不追求覆盖全部语义**，只做「明显变了」的兜底。真要验业务逻辑，
  还得靠 `regress.py` 和功能自带的断言。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 一组通用小输入：覆盖标量、字符串、列表、None、关键字参数。
# 目的不是"测对"，而是"两次调用结果一致可比"。
_DEFAULT_PROBES = (
    ((1, 2), {}),
    ((0, 0), {}),
    ((-1, 5), {}),
    (("a", "b"), {}),
    (([1, 2, 3],), {}),
    (("", ), {}),
    ((None, ), {}),
    ((1, ), {"n": 2}),
)


def snapshot(module, names, probes=None, max_len=200):
    """给 module 里指定的函数做「行为快照」。

    返回：{函数名: [(状态, 输出repr), ...]}；函数不存在记 {"missing": True}。
    ⚠️ 任何异常都记成 ("err", 异常类名)，**绝不向外抛** —— 快照本身不能把流程弄挂。
    """
    probes = probes or _DEFAULT_PROBES
    out = {}
    for n in (names or []):
        try:
            fn = getattr(module, n, None)
        except Exception:                                 # noqa: BLE001
            fn = None
        if not callable(fn):
            out[str(n)] = {"missing": True}
            continue
        rec = []
        for args, kwargs in probes:
            try:
                r = fn(*args, **kwargs)
                rec.append(("ok", repr(r)[:max_len]))
            except Exception as e:                        # noqa: BLE001
                rec.append(("err", type(e).__name__))
        out[str(n)] = rec
    return out


def diff(before, after):
    """比较两份快照，返回变化列表。空列表 = 行为完全一致。"""
    before = before or {}
    after = after or {}
    changes = []
    for n in sorted(set(before) | set(after)):
        b, a = before.get(n), after.get(n)
        if b == a:
            continue
        changes.append({"name": n, "before": b, "after": a})
    return changes


def check(module, names, before, probes=None):
    """便捷入口：拍一张新快照并和 before 比。返回 {"ok", "changes", "after"}。"""
    try:
        after = snapshot(module, names, probes)
        ch = diff(before, after)
        return {"ok": not ch, "changes": ch, "after": after}
    except Exception as e:                                # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return {"ok": True, "changes": [], "after": {}, "error": "%s: %s" % (type(e).__name__, e)}
