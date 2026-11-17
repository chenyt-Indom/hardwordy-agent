# -*- coding: utf-8 -*-
"""交接卡（backend/handoff.py）的**内容级**回归测试。

为什么要有这个文件（2026-11-18）
--------------------------------
`self_audit` 只做静态检查：语法错误、跨模块调用不存在的函数、前端调
不存在的路由、裸 except、同名函数重复定义 —— 全是「代码能不能跑」。
而交接卡出过的问题是**输出质量**：
  · 截断不加省略号，读者以为那就是全文；
  · 截断断在反引号中间，未闭合的 ` 把后文 markdown 全吞掉；
  · 换行被压成空格，「结论：通过」+「通过 14 / 失败 0」粘成「通过 通过」；
  · 中断后重试的重复消息原样列两遍；
  · 轮数按 len//2 算，与实际用户消息数不符。
这些代码全都能正常跑、语法全对，静态检查原理上发现不了，只能对**输出**
做断言。所以单独留一份跑得快的内容测试。

用法：python backend/_feature_tests/test_handoff_card.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import handoff  # noqa: E402
from backend import sessions as real_ss  # noqa: E402

FAILS = []

# Windows 控制台默认 GBK，打不出「…」这类字符会直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


CHECKS = [0]


def check(name, ok, extra=""):
    CHECKS[0] += 1
    print(("  OK   " if ok else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


LONG = ("结论：通过\n通过 14 / 失败 0\n\n问题值得认真解决：\n"
        "- `rescue.py` 是 92,009 B\n- `backend/main.py` 正常\n" + "填充内容" * 120)


def render_with(dialog, session="t", **extra):
    handoff.DIR = tempfile.mkdtemp(prefix="hoftest_")
    handoff.LAST = os.path.join(handoff.DIR, "last.json")
    # render() 读的是 "text"（真实链路由 _dialog_digest 产出这个字段），
    # 测试里为了直观用 "content" 写，这里归一一下
    dialog = [{"role": m.get("role"), "text": m.get("text", m.get("content", ""))}
              for m in dialog]
    d = {"dialog": dialog, "changes": extra.get("changes") or [],
         "ts": time.time(), "restarted_at": "2026-11-18 00:14:17",
         "dialog_session": session, "reason": "测试"}
    d.update({k: v for k, v in extra.items() if k != "changes"})
    with open(handoff.LAST, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return handoff.render(session)


def main():
    # ---- 1. 换行必须保留（不能被压平导致两行粘成一个词）----
    txt = render_with([{"role": "user", "content": "问"},
                       {"role": "assistant", "content": LONG}])
    check("换行保留，不粘成「通过 通过」",
          "结论：通过\n" in txt and "通过 通过" not in txt)

    # ---- 2. 超长截断必须有省略号 ----
    check("超长内容截断带「…」", "…" in txt)

    # ---- 3. 每条对话条目里反引号必须成对（否则吞掉后文）----
    seg = [l for l in txt.splitlines()
           if l.strip().startswith(("你：", "用户："))]
    check("条目内反引号全部成对", all(l.count("`") % 2 == 0 for l in seg), len(seg))

    # ---- 4. 多行内容要缩进对齐，别顶到行首 ----
    check("多行内容缩进对齐", "  - `rescue.py`" in txt)

    # ---- 5. 中断重试的重复消息要合并 ----
    fake = [{"role": "user", "content": "问"},
            {"role": "assistant", "content": LONG},
            {"role": "assistant", "content": LONG},
            {"role": "user", "content": "再问"}]
    real_ss.get_messages = lambda s: fake
    dlg = handoff._dialog_digest("t")
    check("重复条目被合并（4→3）", len(dlg) == 3, [m["role"] for m in dlg])

    # ---- 6. 轮数按真实用户消息数标注 ----
    txt2 = render_with(dlg)
    users = sum(1 for m in dlg if m["role"] == "user")
    check("轮数标注 == 用户消息数", ("最近 %d 轮" % users) in txt2,
          "期望 %d" % users)

    # ---- 7. 没有内容时返回空串（不注入噪音）----
    handoff.DIR = tempfile.mkdtemp(prefix="hoftest_")
    handoff.LAST = os.path.join(handoff.DIR, "last.json")
    check("无内容返回空串", handoff.render("t") == "")

    # ---- 8. 超过 KEEP_DAYS 不再注入 ----
    with open(handoff.LAST, "w", encoding="utf-8") as f:
        json.dump({"dialog": [{"role": "user", "text": "旧"}], "changes": [],
                   "ts": time.time() - (handoff.KEEP_DAYS + 1) * 86400},
                  f, ensure_ascii=False)
    check("超过 %d 天不再注入" % handoff.KEEP_DAYS, handoff.render("t") == "")

    # ---- 9. 改动清单渲染不受影响 ----
    txt3 = render_with([{"role": "user", "content": "问"}],
                       changes=[{"rel": "rescue.py", "note": "修 x",
                                 "added": 10, "removed": 2, "time": "2026-11-18 00:08:11"}])
    check("改动清单正常渲染",
          "- `rescue.py`（+10 -2 行）：修 x" in txt3, txt3[-160:])

    # ---- 9b. 改动清单必须按重启时间分组（实测真实卡片把 02:36~02:41 的改动
    #          标成了「那次重启前」，读卡的人会以为这些都发生在重启前）----
    txt3b = render_with([{"role": "user", "content": "问"}],
                        changes=[{"rel": "a.py", "note": "重启前改的", "added": 1,
                                  "removed": 0, "time": "2026-11-18 00:10:00"},
                                 {"rel": "b.py", "note": "重启后改的", "added": 2,
                                  "removed": 0, "time": "2026-11-18 00:20:00"}])
    _pre = txt3b.split("重启之后又改过的文件")[0]
    check("重启后的改动不混进「重启前」组",
          "a.py" in _pre and "b.py" not in _pre, txt3b[-280:])
    check("重启后的改动单独成组",
          "· 重启之后又改过的文件（最近 1 个）" in txt3b, txt3b[-200:])

    # ---- 9c. 没有重启记录时，标题不能谎称「那次重启前」----
    txt3c = render_with([{"role": "user", "content": "问"}], restarted_at="",
                        changes=[{"rel": "c.py", "note": "x", "added": 1,
                                  "removed": 0, "time": "2026-11-18 00:10:00"}])
    check("无重启记录时标题为「最近改过的文件」",
          "· 最近改过的文件（最近 1 个）" in txt3c and "那次重启前" not in txt3c,
          txt3c[-200:])

    # ---- 9d. 重启后改动很多时，「重启前」那组不能被整个挤掉 ----
    # （2026-11-18 上一版拆分引入的回归：先取最近 12 条再分组，重启后改了 12 条
    #   以上，重启前那组就消失了，交接卡最该交代的信息反而没了）
    _many_chg = ([{"rel": "old%d.py" % i, "note": "重启前", "added": 1, "removed": 0,
                   "time": "2026-11-18 00:%02d:00" % i} for i in range(5)] +
                 [{"rel": "new%d.py" % i, "note": "重启后", "added": 1, "removed": 0,
                   "time": "2026-11-18 00:%02d:00" % (20 + i)} for i in range(12)])
    txt3d = render_with([{"role": "user", "content": "问"}], changes=_many_chg)
    _sec = txt3d.split("重启之后又改过的文件")[0]
    check("重启前那组没被挤掉", "那次重启前改过的文件" in txt3d, txt3d[-260:])
    check("重启前保留 4 个", _sec.count("old") == 4, str(_sec.count("old")))
    check("两组加起来不超 SHOW_CHANGES",
          (txt3d.count("- `") ) <= handoff.SHOW_CHANGES, str(txt3d.count("- `")))

    # ---- 10. 缩进必须保留（嵌套列表被压平就丢了层级）----
    txt4 = render_with([{"role": "user", "content": "问"},
                        {"role": "assistant", "content": "- 一层\n  - 二层\n    - 三层"}])
    check("嵌套缩进保留", "  - 二层" in txt4 and "    - 三层" in txt4, repr(txt4[-90:]))

    # ---- 11. 段落之间的空行要保留（否则 markdown 里粘成一段）----
    txt5 = render_with([{"role": "user", "content": "问"},
                        {"role": "assistant", "content": "第一段\n\n第二段"}])
    check("段落空行保留", "\n\n" in txt5)

    # ---- 12. 截断代码块必须补闭合围栏，且不能留下非法的双反引号 ----
    fence_src = "说明：\n```python\nprint(1)\nprint(2)\nprint(3)\n```\n结束"
    for cap in (20, 30, 40):
        one = handoff._clip(fence_src, cap)
        check("cap=%d 代码块围栏闭合" % cap,
              one.count("```") % 2 == 0 and "``" not in one.replace("```", ""),
              repr(one))

    # ---- 13. cap 极小也不能超出上限 ----
    bad_cap = [(c, handoff._clip("abcdefghij" * 20, c)) for c in (1, 2, 3, 5)]
    check("cap 极小不超限", all(len(o) <= c for c, o in bad_cap),
          [(c, len(o)) for c, o in bad_cap])

    # ---- 14. 连发两次「继续」是两轮，不能被合并掉 ----
    check("短消息重复不合并（轮数不丢）",
          handoff._similar("继续", "继续") is False
          and handoff._similar("好", "好") is False)
    txt6 = render_with([{"role": "user", "content": "继续"},
                        {"role": "user", "content": "继续"}])
    check("两次「继续」算 2 轮", "最近 2 轮" in txt6, txt6[:120])

    # ---- 15. 中断重试：一条是另一条的开头 → 必须合并 ----
    a1 = "结论：通过\n\n通过 14 / 失败 0\n\n- 项1\n  - 子项"
    check("前缀关系的重试消息被合并", handoff._similar(a1, a1 + "（重试版）") is True)
    check("短消息仅开头像不算重复",
          handoff._similar("好的，我来看看这个文件", "好的，我来看看那个目录") is False)

    # ---- 16. 超长代码块要折叠，且折叠提示不能落在围栏里面 ----
    txt7 = render_with([{"role": "user", "content": "看"},
                        {"role": "assistant", "content": "```python\n" + "x = 1\n" * 200 + "```"}])
    check("长代码块被折叠", "已折叠" in txt7)
    check("折叠后围栏成对", txt7.count("```") % 2 == 0, txt7.count("```"))
    _i = txt7.find("已折叠")
    check("折叠提示在围栏外", _i > 0 and txt7[:_i].count("```") % 2 == 0)

    # ---- 17. 渲染结果里不能有「只有空格」的行（markdown 里是硬换行）----
    check("无纯空格行",
          not any(l and not l.strip() for l in txt7.split("\n")))

    # ---- 18. 不截断时也必须配平（短消息里落单的 ` 会吞掉后面整张卡）----
    for _src in ("```python\ncode here", "code here\n```", "用 `rescue.py 处理"):
        _one = handoff._clip(_src, 500)
        check("不截断也配平围栏 %r" % _src[:14],
              _one.count("```") % 2 == 0
              and _one.replace("```", "").count("`") % 2 == 0, repr(_one))
        _card = render_with([{"role": "user", "content": "q"},
                             {"role": "assistant", "content": _src}])
        check("渲染后整卡围栏成对 %r" % _src[:14],
              _card.count("```") % 2 == 0, _card.count("```"))

    # ---- 19. 各种 cap 下都不超限且围栏成对 ----
    _bad = []
    for _cap in (1, 2, 3, 5, 10, 20, 30, 40, 60, 200):
        for _src in ("```python\nprint(1)\nprint(2)\n```", "用 `x 处理一下", "abcdefghij" * 50):
            _o = handoff._clip(_src, _cap)
            if len(_o) > _cap or _o.count("```") % 2:
                _bad.append((_cap, len(_o), _o.count("```")))
    check("各种 cap 不超限且围栏成对", not _bad, _bad[:5])

    # ---- 20. 改动清单的 note 含落单反引号 → 不能吞掉后面的清单和提示 ----
    # 真实事故：note「实测短消息里落单的 ` 会吞掉整张卡」让那一行反引号变 3 个，
    # 卡片里剩下的清单行 + ⚠️ 提示全被当成行内代码。
    _c = [{"rel": "backend/x.py", "note": "落单的 ` 会吞卡",
           "added": 1, "removed": 0, "time": "2026-11-18 02:37"},
          {"rel": "backend/y.py", "note": "第二条", "added": 1, "removed": 0,
           "time": "2026-11-18 02:38"}]
    _card = render_with([{"role": "user", "content": "q"}], changes=_c)
    check("清单 note 落单反引号被配平",
          not [l for l in _card.split("\n") if l.count("`") % 2 == 1],
          [l for l in _card.split("\n") if l.count("`") % 2 == 1][:2])
    check("清单后续条目没被吞", "`backend/y.py`" in _card)

    # ---- 21. 清单 note / rel 超长要带省略号（原来硬截断，读的人以为全文）----
    _long_note = handoff._fmt_change({"rel": "a.py", "note": "啊" * 300,
                                      "added": 0, "removed": 0, "time": ""})
    check("清单 note 超长带省略号", "…" in _long_note and len(_long_note) < 220,
          len(_long_note))
    _long_rel = handoff._fmt_change({"rel": "d/" * 100 + "x.py", "note": "n",
                                     "added": 0, "removed": 0, "time": ""})
    check("清单 rel 超长带省略号且配平",
          "…" in _long_rel and _long_rel.count("`") % 2 == 0, len(_long_rel))

    # ---- 22. 原因行含落单反引号也不能吞卡 ----
    _card = render_with([{"role": "user", "content": "q"}],
                        reason="用户点了重启 ` 然后关窗")
    check("原因行落单反引号被配平",
          not [l for l in _card.split("\n") if l.count("`") % 2 == 1])

    # ---- 23. _tail 必须保留换行（原来压成一行，列表/分段全丢）----
    _t = handoff._tail("第一行\n第二行")
    check("_tail 保留换行", "\n" in _t and "第一行" in _t and "第二行" in _t, repr(_t))
    _t2 = handoff._tail("x" * 400 + "\n第二行\n第三行", 240)
    check("_tail 截尾不切断半行",
          "\n" in _t2 and "…" in _t2 and not _t2.lstrip().startswith("xxx"),
          repr(_t2[:40]))

    # ---- 24. 折叠提示里的「原文共 N 行」必须是真实行数 ----
    # 原来用的是 _clip 之后的行数：原文 60 行、clip 到 200 字后剩 30 行 → 谎报 30
    _many = "\n".join("第%d行内容" % i for i in range(60))
    _card = render_with([{"role": "user", "content": "q"},
                         {"role": "assistant", "content": _many}], )
    _m = [l for l in _card.split("\n") if "原文共" in l]
    check("折叠提示行数用原文行数", bool(_m) and "共 60 行" in _m[0], _m[:1])

    # ---- 25. 配平删掉落单反引号后不能留下两个空格 ----
    _n = handoff._fmt_change({"rel": "a.py", "note": "落单的 ` 会吞卡",
                              "added": 0, "removed": 0, "time": ""})
    check("配平后不留双空格", "  " not in _n.split("：", 1)[-1].split("　")[0], repr(_n))

    # ================= 2026-11-18 模糊测试（随机内容 × 多种 cap）挖出来的 =============
    # ---- 26. 配平不能吃掉刚补上的闭合围栏（最小复现：输入 ``` → 输出 "```\n`"）----
    _b = handoff._balance_fence("```")
    check("配平不吃掉闭合围栏", _b.count("```") % 2 == 0, repr(_b))
    _b2 = handoff._balance_fence("```\n用 `x")
    check("围栏+落单行内反引号同时存在也成对",
          _b2.count("```") % 2 == 0 and
          all(p.count("`") % 2 == 0 for p in _b2.split("```")[::2]), repr(_b2))

    # ---- 27. cap 极小时的兜底截断不能切出半截围栏 ----
    for cap in (1, 2, 3, 5, 7, 9):
        for src in ("```", "```\ncode", "````", "a\n```\nb"):
            o = handoff._clip(src, cap)
            check("cap=%d 截断后围栏成对（%r）" % (cap, src[:6]),
                  len(o) <= cap and o.count("```") % 2 == 0, repr(o))

    # ---- 28. 不能输出一行纯空格 ----
    for src in (" `", "`` ", "``` `"):
        o = handoff._clip(src, 5)
        check("无纯空格行（%r）" % src, not any(l and not l.strip()
                                                for l in o.split("\n")), repr(o))

    # ---- 29. 单行场景（原因/rel/note）里不能出现换行围栏 ----
    _i = handoff._inline("```python\ncode\n```\n落单 `", 120)
    check("_inline 不产生换行", "\n" not in _i, repr(_i))
    check("_inline 围栏成对", _i.count("```") % 2 == 0, repr(_i))
    _i2 = handoff._inline("a ``` b ``` c ```", 120)      # 3 个围栏（奇数）
    check("_inline 修掉奇数围栏", _i2.count("```") % 2 == 0, repr(_i2))

    # ---- 30. 改动清单：rel 自己带反引号时，外层包裹不能破 ----
    _r = handoff._fmt_change({"rel": "````", "note": "x", "added": 1,
                              "removed": 0, "time": ""})
    check("清单 rel 不带反引号", _r.count("`") % 2 == 0, repr(_r))

    # ---- 31. 整卡随机模糊（200 组）：围栏/行内反引号/纯空格行/轮数 ----
    import random as _random
    _alpha = ["`", "``", "```", "```python\n", "a", "中", "\n", "\n\n", " ", "\t",
              "…", "**", "|", "- ", "#", "\\", "\r\n", "~~~", "🤖"]
    _random.seed(20261118)
    _bad = []
    for _ in range(200):
        _dlg = [{"role": _random.choice(["user", "assistant"]),
                 "text": "".join(_random.choice(_alpha)
                                 for _ in range(_random.randint(0, 25)))}
                for _ in range(_random.randint(0, 6))]
        _chg = [{"rel": "".join(_random.choice(_alpha)
                                for _ in range(_random.randint(0, 8))),
                 "note": "".join(_random.choice(_alpha)
                                 for _ in range(_random.randint(0, 25))),
                 "added": 1, "removed": 0, "time": "2026-11-18 00:10:00"}
                for _ in range(_random.randint(0, 10))]
        _c = render_with(_dlg, changes=_chg,
                         reason="".join(_random.choice(_alpha)
                                        for _ in range(15)))
        _ps = _c.split("```")
        if _c.count("```") % 2 == 1:
            _bad.append(("围栏奇数", _c[:120]))
        elif any(_ps[i].count("`") % 2 == 1 for i in range(0, len(_ps), 2)):
            _bad.append(("围栏外反引号奇数", _c[:120]))
        elif any(l and not l.strip() for l in _c.split("\n")):
            _bad.append(("纯空格行", _c[:120]))
        _u = sum(1 for m in _dlg if m["role"] == "user")
        if _u and ("最近 %d 轮" % _u) not in _c:
            _bad.append(("轮数不符", _c[:120]))
    check("整卡随机模糊 200 组无不变量违例", not _bad, str(_bad[:2]))

    # ---- 32. 落盘数据本身要干净：rel/note 不能带换行，反引号必须成对 ----
    handoff.DIR = tempfile.mkdtemp(prefix="hoftest_rec_")
    handoff.LAST = os.path.join(handoff.DIR, "last.json")
    handoff.record_change("a.py", note="落单 ` 会吞卡", added=3, removed=1)
    handoff.record_change("````", note="```python\nx", added=1, removed=0)
    handoff.record_change("a.py", note="第二次改", added=2, removed=0)
    _rec = json.load(open(handoff.LAST, encoding="utf-8"))["changes"]
    check("落盘 rel/note 无换行", all("\n" not in c["rel"] and "\n" not in c["note"]
                                    for c in _rec), str(_rec)[:160])
    check("落盘 note 反引号成对", all(c["note"].count("`") % 2 == 0 for c in _rec),
          str(_rec)[:160])
    check("同一文件被合并", len(_rec) == 1 and _rec[0]["added"] == 5, str(_rec)[:160])

    # ---- 33. 保存 reason 时超长要留省略号（原来 _clean 硬截断，落盘后看不出被截过）----
    handoff.touch("s", "x" * 200)
    _r = json.load(open(handoff.LAST, encoding="utf-8"))["reason"]
    check("落盘 reason 超长带省略号", "…" in _r and len(_r) <= 120, repr(_r[:40]))

    # ===== 2026-11-18 用户贴的真实卡片 + 复现挖出来的（配平/截断） =====
    # ---- 34. 配平不能靠「删内容」：note 里的 ``` 必须还看得见 ----
    _n = handoff._inline("实测 reason 里 3 个 ``` 会漏", 200)
    check("_inline 配平不丢内容", ("｀｀｀" in _n) or ("```" in _n), repr(_n))
    check("_inline 配平不留双空格", "  " not in _n, repr(_n))
    _n2 = handoff._inline("落单 ` 反引号", 200)
    check("_inline 落单反引号保留可辨识标记", ("｀" in _n2) or ("`" in _n2), repr(_n2))

    # ---- 35. ** 必须在配平反引号之前处理（删 ** 会让两段反引号连成奇数围栏）----
    for _src in ("``**`````python", "**``````**", "``**`**``"):
        _o = handoff._balance_fence(_src)
        check("先处理 ** 再配平围栏（%r）" % _src[:10],
              _o.count("```") % 2 == 0 and
              all(p.count("`") % 2 == 0 for p in _o.split("```")[::2]), repr(_o))

    # ---- 36. _finalize 末尾的兜底截断不能破坏刚配好的围栏 ----
    for _src, _cap in (('|```\r\n\n``**`````python\n', 200),
                       ('`````**``\\a\\中\n\n', 200),
                       ('```\n' + 'x' * 50, 12)):
        _o = handoff._clip(_src, _cap)
        check("_finalize 硬切后仍配平（cap=%d）" % _cap,
              len(_o) <= _cap and _o.count("```") % 2 == 0, repr(_o))

    # ---- 37. 截断不能只留结尾的 **（整串 ** 为偶数时也会发生）----
    _src = "开头很长" * 30 + "**唯一确凿的新 bug 就是「重启前」这个标题在说谎**。现在修它。"
    _t = handoff._tail(_src, 60)
    check("_tail 截断后 ** 成对", _t.count("**") % 2 == 0, repr(_t))
    _c = handoff._clip(_src, 60)
    check("_clip 截断后 ** 成对", _c.count("**") % 2 == 0, repr(_c))

    # ---- 38. rel 是文件路径：全角 ｀ 也必须剔除（否则 "｀｀｀｀" 会被当成文件名）----
    handoff.DIR = tempfile.mkdtemp(prefix="hoftest_rel_")
    handoff.LAST = os.path.join(handoff.DIR, "last.json")
    handoff.record_change("a.py", note="x", added=1, removed=0)
    handoff.record_change("````", note="x", added=1, removed=0)
    _rel = json.load(open(handoff.LAST, encoding="utf-8"))["changes"]
    check("rel 里的反引号（含全角）一律剔除",
          len(_rel) == 1 and all("`" not in c["rel"] and "｀" not in c["rel"]
                                 for c in _rel), str(_rel)[:120])

    # ---- 39. 清单整行拼好后 ** 还要成对（rel 里带 *** 时整行会变奇数）----
    for _rel in ("``#*****\n…\n***``````***", "\\#** - ***````***", "a.py"):
        _l = handoff._fmt_change({"rel": _rel, "note": "**重点** 说明", "added": 1,
                                  "removed": 0, "time": "2026-11-18 00:10:00"})
        _p = _l.split("```")
        check("清单整行 ** 成对（rel=%r）" % _rel[:8],
              _l.count("**") % 2 == 0, repr(_l))
        check("清单整行反引号成对（rel=%r）" % _rel[:8],
              _l.count("```") % 2 == 0 and
              all(x.count("`") % 2 == 0 for x in _p[::2]), repr(_l))

    # ---- 40. 落单反引号有**多处**时要一次修完（原来写死循环 4 次，修不完）----
    _multi = "```````python\n```python\n- \n- ##````\n```\\\n````\n```python\n…```````python"
    _o = handoff._inline(_multi, 400)
    check("多处落单反引号全部修完",
          _o.count("```") % 2 == 0 and
          all(x.count("`") % 2 == 0 for x in _o.split("```")[::2]), repr(_o))

    # ---- 41. 行内代码里的 `**` 不该把**正常粗体**拆成 ＊＊（用户贴的真实卡片抓到）----
    _s = ('根因：**4 处"`**`"和"`**`"**，统一改成**全角 `＊＊`**。现在**必须重启**——否则')
    _o = handoff._clip(_s, 2000)
    check("行内代码里的 ** 不拆正常粗体（_clip）", "重启＊＊" not in _o, repr(_o[-24:]))
    _o2 = handoff._inline('测试：固化「整行 `**` 配平」，注意**这个**要保留', 400)
    check("行内代码里的 ** 不拆正常粗体（_inline）", "这个＊＊" not in _o2, repr(_o2))
    _t = "前面很多字" * 20 + "正文 `**` 行内 后面**真粗体**。收尾"
    check("_tail 不因行内 ** 误补开头 **", "…**正文" not in handoff._tail(_t, 40),
          repr(handoff._tail(_t, 40)))

    # ---- 42. 截断正好切在粗体中间：去掉语法标记，不留 ＊＊ 当错字 ----
    _cut = "确认 **4 个真实缺陷**，其中 2 个是真实 bug：**代码块补上的闭合围栏被长度兜底又截掉了**，后面还有很多字"
    _oc = handoff._clip(_cut, 60)
    check("截断切在粗体中间不留 ＊＊", "＊＊" not in _oc, repr(_oc[-20:]))

    # ---- 43. 短消息里落单的 ** 仍要保留（不能为了配平删内容）----
    check("短消息落单 ** 仍保留", "＊＊" in handoff._clip("note 里有落单 ** 结尾", 200),
          repr(handoff._clip("note 里有落单 ** 结尾", 200)))

    # ---- 44. 反引号配平会改变 ** 奇偶 → 必须反复处理到稳定 ----
    _mix = "`**```双反引号``   缩进`**`a**b**c"
    _om = handoff._clip(_mix, 80)
    check("配平后仍稳定（** 成对）", len(handoff._bold_pos(_om)) % 2 == 0, repr(_om))
    check("配平后仍稳定（围栏成对）", _om.count("```") % 2 == 0, repr(_om))

    # ---- 45. 并发写 last.json 不能互相覆盖 ----
    # record_change / touch / snapshot 都是「_read → 改 → _write」非原子序列，
    # 两个线程同时跑会互相覆盖。实测修前：record_change 与 touch 并发 **20/20 必丢**。
    import threading
    _old_last = handoff.LAST
    _tmpd = tempfile.mkdtemp(prefix="handoff_conc_")
    handoff.LAST = os.path.join(_tmpd, "last.json")
    try:
        handoff._write({"changes": [], "dialog": []})
        _ths = [threading.Thread(target=handoff.record_change,
                                 args=("c%02d.py" % i,), kwargs={"added": 1})
                for i in range(20)]
        for _t in _ths:
            _t.start()
        for _t in _ths:
            _t.join()
        _d = handoff._read()
        _rels = [c.get("rel") for c in (_d.get("changes") or [])]
        check("并发 20 条改动全部写入（不互相覆盖）", len(_rels) == 20, "实得 %d 条" % len(_rels))
        check("并发写入顺序不乱", _rels == sorted(_rels), str(_rels[:5]))
    finally:
        handoff.LAST = _old_last
        try:
            import shutil as _sh
            _sh.rmtree(_tmpd, ignore_errors=True)
        except Exception:
            pass

    # ---- 46. last.json 损坏时，绝不能拿空字典覆盖整份历史（数据丢失级）----
    # 修前实测：24 条对话 + 30 条改动 → 文件损坏 → 记录一条改动 → 全没。
    _old_last2 = handoff.LAST
    _tmpd2 = tempfile.mkdtemp(prefix="handoff_corrupt_")
    handoff.LAST = os.path.join(_tmpd2, "last.json")
    try:
        _full = {"session": "s1", "dialog": [{"role": "user", "text": "x%d" % i} for i in range(24)],
                 "changes": [{"rel": "f%d.py" % i, "note": "n"} for i in range(30)]}
        handoff._write(_full)                     # 正常写一次 → 生成 .bak
        with open(handoff.LAST, "w", encoding="utf-8") as _f:
            _f.write('{"session": "s1", "dialog": [{"role": "us')   # 截断损坏
        handoff.record_change("new.py", "改一处")
        _d2 = handoff._read()
        check("损坏后对话历史未被覆盖（有 .bak）", len(_d2.get("dialog") or []) == 24,
              "实得 %d 条" % len(_d2.get("dialog") or []))
        check("损坏后改动历史未被覆盖（有 .bak）", len(_d2.get("changes") or []) >= 30,
              "实得 %d 条" % len(_d2.get("changes") or []))
        _corrupt = [n for n in os.listdir(_tmpd2) if ".corrupt-" in n]
        check("坏文件被隔离保留（可人工抢救）", len(_corrupt) == 1, str(_corrupt))

        # ---- 从 .bak 恢复后必须**写回磁盘**，否则第 2 次读就读不到（数据丢失级）----
        # 上一版只修了「第一次读能从 .bak 恢复」，没测第二次：LAST 已被改名隔离、
        # 此刻并不存在 → 下次 _read 走 FileNotFoundError → return {} → 全丢。
        # 修前实测：24 条对话 / 30 条改动，第 2 次读取后全部消失。
        def _wipe(dirpath):
            for _n in os.listdir(dirpath):
                try:
                    os.remove(os.path.join(dirpath, _n))
                except Exception:
                    pass

        _wipe(_tmpd2)
        handoff._write(_full)
        with open(handoff.LAST, "w", encoding="utf-8") as _f:
            _f.write("{坏")
        handoff._read()                       # 只读一次（模拟 render 被调用）
        check("从 .bak 恢复后 last.json 被重建", os.path.exists(handoff.LAST),
              str(sorted(os.listdir(_tmpd2))))
        _second = handoff._read()             # 关键：第 2 次读
        check("恢复后第 2 次读仍有对话历史", len(_second.get("dialog") or []) == 24,
              "实得 %d 条" % len(_second.get("dialog") or []))
        check("恢复后第 2 次读仍有改动历史", len(_second.get("changes") or []) >= 30,
              "实得 %d 条" % len(_second.get("changes") or []))

        # ---- .bak 自己也坏时，必须隔离它（否则每次损坏都拿同一份坏备份去试）----
        _wipe(_tmpd2)
        handoff._write(_full)
        for _f2 in ("last.json", "last.json.bak"):
            with open(os.path.join(_tmpd2, _f2), "w", encoding="utf-8") as _f:
                _f.write("{都坏")
        handoff._read()
        _bad_bak = [n for n in os.listdir(_tmpd2) if n.startswith("last.json.bak.corrupt-")]
        check("坏 .bak 被隔离（不反复使用坏备份）", len(_bad_bak) == 1,
              str(sorted(os.listdir(_tmpd2))))

        # 没有 .bak 时也不能抛异常，且坏文件同样要留住
        for _n in os.listdir(_tmpd2):
            try:
                os.remove(os.path.join(_tmpd2, _n))
            except Exception:
                pass
        with open(handoff.LAST, "w", encoding="utf-8") as _f:
            _f.write("{坏掉了")
        _ok = True
        try:
            handoff.record_change("b.py", "x")
        except Exception:
            _ok = False
        check("损坏且无 .bak 时不抛异常", _ok and len(handoff._read().get("changes") or []) >= 1,
              str(len(handoff._read().get("changes") or [])))
    finally:
        handoff.LAST = _old_last2
        try:
            import shutil as _sh2
            _sh2.rmtree(_tmpd2, ignore_errors=True)
        except Exception:
            pass

    # ---- 50. render 过期判断：NaN / Infinity / 未来时间都不能永久注入 ----
    # 修前实测：ts=NaN、Infinity、10**20 时 `now - ts > KEEP_DAYS*86400` 恒为 False，
    # 卡片**永不过期**，无限期占上下文。
    _old_last3 = handoff.LAST
    _tmpd3 = tempfile.mkdtemp(prefix="handoff_ts_")
    handoff.LAST = os.path.join(_tmpd3, "last.json")
    try:
        def _render_ts(_v):
            _d = {"session": "s1", "restarted_at": "2026-11-18 03:08:47",
                  "dialog": [{"role": "user", "text": "你好"}],
                  "changes": [{"rel": "a.py", "note": "n"}]}
            if _v is not None:
                _d["ts"] = _v
            with open(handoff.LAST, "w", encoding="utf-8") as _f:
                json.dump(_d, _f, ensure_ascii=False)
            return handoff.render("s1")

        _bad = []
        for _n, _v in (("NaN", float("nan")), ("Infinity", float("inf")),
                       ("-Infinity", float("-inf")), ("10**20", 10 ** 20),
                       ("未来 10 天", __import__("time").time() + 10 * 86400)):
            if _render_ts(_v):
                _bad.append(_n)
        check("异常 ts 不会永久注入（NaN/inf/未来）", not _bad, "漏了：" + str(_bad))
        check("正常 ts 仍能注入", bool(_render_ts(__import__("time").time())), "")
        check("超过 KEEP_DAYS 不注入",
              not _render_ts(__import__("time").time() - (handoff.KEEP_DAYS + 1) * 86400), "")
    finally:
        handoff.LAST = _old_last3
        try:
            import shutil as _sh3
            _sh3.rmtree(_tmpd3, ignore_errors=True)
        except Exception:
            pass

    # ---- 50. 重写 markdown 安全层后新增的不变量（2026-11-18）----
    # 这一组全部来自「重写时自己引入 / 重写后模糊测试抓到」的真缺陷，
    # 每条都对应一个曾经真实发生过的输出错误。
    def _audit(text):
        """独立检查器（不复用 handoff 里的判断，避免自己验自己）：
           围栏成对 / 围栏外的行内反引号成对 / 围栏块内的 ** 成对。"""
        if text.count("```") % 2:
            return "围栏奇数"
        for _i, _p in enumerate(text.split("```")):
            if _i % 2:
                continue
            if _p.count("`") % 2:
                return "行内反引号奇数"
            _plain = "".join(_s if _j % 2 == 0 else "\x00"
                             for _j, _s in enumerate(_p.split("`")))
            if _plain.count("**") % 2:
                return "粗体奇数"
        return None

    # 50.1 粗体**不能跨代码围栏**配对（围栏会打断 markdown 解析）
    _x = "_[**\n```\\**```(🙂- |**#_"
    # ---- 围栏行本身的 `**` 不算正文粗体（否则围栏外落单的永远配不平）----
    # 修前实测：'```a\n```** x\n正文**' → 正文那个 `**` 一直是半角，
    # 输出后它把后面整段变粗体。根因：split("```") 把围栏行的剩余内容
    # （`** x`）归进了「围栏外」的块，和正文那个 `**` 凑成一对。
    for _src, _why in [
        ("```a\n```** x\n正文**", "围栏行带 ** + 围栏外落单 **"),
        ("```a\n```b\n🙂**", "普通围栏 + 围栏外落单 **"),
        ("```py\ncode\n```\n**粗体**", "正常粗体不能被误伤"),
    ]:
        _o = handoff.sanitize(_src, None)
        _bp = handoff._bold_pos(_o)
        if _why == "正常粗体不能被误伤":
            check("围栏行剔除 **：" + _why, "**粗体**" in _o, repr(_o))
        else:
            check("围栏行剔除 **：" + _why, len(_bp) % 2 == 0, repr(_o) + " pos=%r" % _bp)

    check("粗体不跨围栏配对", _audit(handoff.sanitize(_x, None)) is None,
          repr(handoff.sanitize(_x, None)))

    # 50.2 连写 ≥3 个星号降级成全角（`***` 落单会让后面整段变粗斜体）
    _x = "a***b"
    _o = handoff.sanitize(_x, None)
    check("连写 3 个星号降级", "***" not in _o and "＊＊＊" in _o, repr(_o))

    # 50.3 幂等：同一段文本处理两次，结果必须一样
    _idem_bad = []
    for _x in ("```", "````", "**", "***", "a\n\n```b", "`a` **b** ```c",
               "…＊＊ *\n\n*", "[#*\u3000*", "(* \u3000*"):
        for _cap in (None, 5, 9, 60):
            for _ml in (True, False):
                _a = handoff.sanitize(_x, _cap, multiline=_ml)
                if handoff.sanitize(_a, _cap, multiline=_ml) != _a:
                    _idem_bad.append((_x, _cap, _ml))
    check("sanitize 幂等（处理两次结果相同）", not _idem_bad, str(_idem_bad[:3]))

    # 50.4 单行上下文**不能补换行围栏**（补出来会把一行撕成两行）
    _o = handoff._inline("```python 代码", 200)
    check("单行上下文不补换行围栏", "\n" not in _o, repr(_o))

    # 50.5 长度正好等于 cap 时不该再截（否则第二次进来会多切一刀）
    _s = "x" * 60
    check("等长 cap 不重截", handoff.sanitize(_s, 60) == _s,
          repr(handoff.sanitize(_s, 60)[-10:]))

    # 50.6 配平之后长度不变（等长替换的前提，超长会顶破 cap）
    _ov = []
    for _x in ("```", "````", "**", "***", "`a`b", "a```b```c"):
        if len(handoff.sanitize(_x, None)) > len(_x) + 4:   # 只允许补一个 "\n```"
            _ov.append(_x)
    check("配平不无端加长", not _ov, str(_ov))

    # 50.7 drop_lone 删掉 ** 之后不能连出新的未闭合围栏
    _x = "``**``xx"
    _o = handoff.sanitize(_x, 8)
    check("删 ** 后不留新围栏", _audit(_o) is None, repr(_o))

    # 50.8 整卡：随机内容下围栏/粗体/行内反引号全部成对
    import random as _rnd
    _rnd.seed(2026)
    _tok = ["`", "``", "```", "````", "**", "***", "****", "中文", "\n", "\n\n", " ",
            "　", "｀", "＊＊", "```python\n", "\n```", "|", "- ", "#", "…", "*", "\\"]
    _orig_read = handoff._read
    _card_bad = []
    try:
        for _ in range(200):
            _dlg = [{"role": _rnd.choice(["user", "assistant"]),
                     "text": "".join(_rnd.choice(_tok) for _ in range(_rnd.randint(1, 15)))}
                    for _ in range(_rnd.randint(1, 10))]
            _d = {"ts": __import__("time").time(),
                  "restarted_at": "2026-11-18 03:11:37",
                  "dialog": _dlg, "dialog_session": "s1", "live_at": "x",
                  "changes": [{"rel": "a.py", "note": "".join(
                      _rnd.choice(_tok) for _ in range(5))}]}
            handoff._read = lambda _d=_d: _d
            if _audit(handoff.render("s1")):
                _card_bad.append(_audit(handoff.render("s1")))
    finally:
        handoff._read = _orig_read
    check("整卡随机 200 组无不变量违例", not _card_bad, str(_card_bad[:3]))

    # 51. 行内的 ``` 不是围栏（markdown 只认行首）
    #     实测 2026-11-18 的交接卡：助手原文里有 `仍会补 "\n```"` 这么一句，
    #     _tail 截出来后 count 判出 1 个围栏 → 补一个闭合 → 整卡围栏变奇数，
    #     后面 12 条改动清单全被吞进代码块。
    _x = '他补了 `"\\n```"` 这一段'
    _o = handoff.sanitize(_x, 200)
    _f = [l for l in _o.split("\n") if l.lstrip().startswith("```")]
    check("行内 ``` 不凭空补出围栏", not _f, repr(_o))

    # 52. ~~~ 在 markdown 里与 ``` 等价，未闭合一样吞掉整张卡
    _o = handoff.sanitize("前文\n~~~\n代码", 200)
    _f = [l for l in _o.split("\n") if l.lstrip().startswith(("```", "~~~"))]
    check("未闭合 ~~~ 被降级（不吞卡）", not _f, repr(_o))

    # 53. _tail 从末尾截，截出来含行内 ``` 时不能补围栏
    _o = handoff._tail("很长" * 200 + ' 仍会补 "\\n```" 这句')
    _f = [l for l in _o.split("\n") if l.lstrip().startswith("```")]
    check("_tail 不因行内 ``` 补围栏", not _f, repr(_o[-40:]))

    # 54. 整卡：内容含行内 ``` 与 ~~~ 时，围栏仍必须成对
    _d = {"ts": __import__("time").time(), "restarted_at": "2026-11-18 03:11:37",
          "dialog": [{"role": "user", "text": "他说 ``` 和 ~~~ 都会吞"},
                     {"role": "assistant", "text": "补 \"\\n```\" 之后\n~~~\n结束"}],
          "dialog_session": "s2", "live_at": "x", "changes": []}
    _orig_read = handoff._read
    try:
        handoff._read = lambda _d=_d: _d
        _c = handoff.render("s2")
    finally:
        handoff._read = _orig_read
    _f = [l for l in _c.split("\n") if l.lstrip().startswith(("```", "~~~"))]
    check("整卡含行内 ``` / ~~~ 时围栏成对", len(_f) % 2 == 0, repr(_f))

    # 55. 卡片里带**代码块**的对话 —— 正文第一行要加「用户：/ 你：」前缀，
    #     那行的 ``` 就不在行首了，可续行缩进两格后仍在行首 → 配对被打断。
    #     实测：body='```python\ncode\n```' 单独看围栏 2（偶），进卡变 1（奇），
    #     后面 12 条改动清单全被吞进代码块。所以进卡前必须把围栏全降级。
    _d = {"ts": __import__("time").time(), "restarted_at": "2026-11-18 04:04:16",
          "dialog": [{"role": "user", "text": "看这段"},
                     {"role": "assistant", "text": "```python\nprint(1)\n```\n完了"}],
          "dialog_session": "s3", "live_at": "x", "changes": []}
    _orig_read = handoff._read
    try:
        handoff._read = lambda _d=_d: _d
        _c = handoff.render("s3")
    finally:
        handoff._read = _orig_read
    _f = [l for l in _c.split("\n") if l.lstrip().startswith(("```", "~~~"))]
    check("卡片里带代码块不产生未闭合围栏", len(_f) % 2 == 0, repr(_f))

    # 56. _tail 拼在「那个会话最后停在这里：」后面，同样不能补围栏
    _o = handoff._tail("前文\n```\n代码块")
    _f = [l for l in _o.split("\n") if l.lstrip().startswith("```")]
    check("_tail 不补出闭合围栏", not _f, repr(_o))

    # 57. 单行吃掉全部预算 → 后面几十行全丢（内容丢失级）
    #     实测：一条回复第一段 2293 字，cap=1200 时第一行就吃满，
    #     46 行只剩 1 行；24 条对话合计只保留 38.7%。
    _long = "甲" * 2000 + "\n" + "\n".join("第%d行结论：通过" % i for i in range(1, 40))
    _no = handoff._clip(_long, 1200, per_line_cap=0)
    _yes = handoff._clip(_long, 1200, per_line_cap=400)
    check("不给单行上限时只剩第一行（旧行为）", _no.count("\n") == 0, "行数=%d" % (_no.count("\n") + 1))
    check("给单行上限后能保留多行结构", _yes.count("\n") >= 5, "行数=%d" % (_yes.count("\n") + 1))
    check("单行上限后仍能看到后面的结论", "结论" in _yes, repr(_yes[-80:]))
    check("单行上限后不超 cap", len(_yes) <= 1200, "len=%d" % len(_yes))
    check("单行上限不破坏幂等",
          _yes == handoff._clip(_yes, 1200, per_line_cap=400), "不稳定")
    check("单行上限超长的行带省略号", "…" in _yes, repr(_yes[:60]))

    # 58. digest 与 render 都要真的传了这个参数（否则改了等于没改）
    import inspect
    _src = inspect.getsource(handoff._dialog_digest)
    check("_dialog_digest 传了 per_line_cap", "per_line_cap" in _src, _src[:60])
    _src2 = inspect.getsource(handoff.render)
    check("render 传了 per_line_cap", "per_line_cap" in _src2, "")

    # 59. 标题注入：restarted_at / live_at 是**唯一**没走 sanitize 的入口。
    #     实测 2026-11-18：restarted_at = "2026-11-18\n```\n假代码" 时，标题被
    #     撕成两行、整卡围栏行变 1（奇数），后面整张卡全被当代码块吞掉。
    _orig_read = handoff._read
    _inj = []
    try:
        for _bad in ("2026-11-18\n```\n假代码", "~~~", "x`", "a\n\nb", "**粗"):
            for _k in ("restarted_at", "live_at"):
                _d = {"ts": __import__("time").time(),
                      "dialog": [{"role": "user", "text": "你好"}], "changes": []}
                _d[_k] = _bad
                handoff._read = lambda _d=_d: _d
                _o = handoff.render("")
                _head = _o.split("\n")[0]
                # 标题必须只有一行（以 】 收尾），且整卡不变量不能破
                if _audit(_o) or "】" not in _head:
                    _inj.append((_k, _bad, _head[:40]))
    finally:
        handoff._read = _orig_read
    check("标题里的 restarted_at / live_at 已 sanitize（不吞卡）", not _inj, str(_inj[:3]))

    # 60. 改动时间格式容错。原来 `t[5:16]` 硬切，time 是 "11-18 05:00"（没有
    #     年份）时切出 "[ 05:00]" —— 前面多一个空格，看着像时间丢了。
    _orig_read = handoff._read
    _times = {}
    try:
        for _t in ("2026-11-18 04:01:00", "11-18 05:00", "2026-11-18T04:01:00",
                   "2026-11-18", "", "垃圾"):
            _d = {"ts": __import__("time").time(),
                  "restarted_at": "2026-11-18 04:28:58",
                  "dialog": [{"role": "user", "text": "你好"}],
                  "changes": [{"rel": "a.py", "note": "x", "time": _t}]}
            handoff._read = lambda _d=_d: _d
            _times[_t] = [l for l in handoff.render("").split("\n") if "a.py" in l][0]
    finally:
        handoff._read = _orig_read
    check("时间格式：完整格式正确", "[11-18 04:01]" in _times["2026-11-18 04:01:00"],
          _times["2026-11-18 04:01:00"])
    check("时间格式：短格式不切出前导空格", "[11-18 05:00]" in _times["11-18 05:00"],
          _times["11-18 05:00"])
    check("时间格式：ISO 格式也认", "[11-18 04:01]" in _times["2026-11-18T04:01:00"],
          _times["2026-11-18T04:01:00"])
    _bad_t = [t for t in ("2026-11-18", "", "垃圾") if "[" in _times[t]]
    check("时间格式：解析不出就不显示时间", not _bad_t, str(_bad_t))

    # ── 超长单行的头尾截断（否则每条回复只剩开场白、结论全丢）──────────
    _opener = "我先核对当前版本是否已生效，并运行回归测试。" * 30   # 前半：客套话
    _concl = "结论：修好了 3 个 bug，回归 152 项全过。"               # 后半：真正的结论
    _long = _opener + _concl
    _o = handoff.sanitize(_long, cap=300, per_line_cap=200)
    check("超长行头尾都保留：尾部结论可见", "结论：修好了" in _o, _o[-40:])
    check("超长行头尾都保留：开头也在", _o.startswith("我先核对"), _o[:20])
    check("超长行截断后有省略号", "…" in _o, _o[:20])

    _tbl = "| 项目 | 结果 | " + "x" * 200
    _t = handoff.sanitize(_tbl, cap=300, per_line_cap=80)
    check("表格行只截前缀（不破坏列结构）", _t.lstrip().startswith("| 项目 | 结果 |"),
          _t[:30])

    _fence = "```python " + "y" * 200
    _f = handoff.sanitize(_fence, cap=300, per_line_cap=80)
    check("围栏行只截前缀", _f.lstrip().startswith("｀｀｀") or _f.lstrip().startswith("```"),
          _f[:20])

    _over = [len(x) for x in handoff.sanitize("长" * 999, cap=500, per_line_cap=200).split("\n")]
    check("头尾截断不超上限", max(_over) <= 500, str(max(_over)))

    _a = handoff.sanitize(_long, cap=300, per_line_cap=200)
    _b = handoff.sanitize(_a, cap=300, per_line_cap=200)
    check("头尾截断幂等", _a == _b, "%r vs %r" % (_a[-30:], _b[-30:]))

    # ── 本轮 3 个修复（2026-11-18）────────────────────────────────────
    # 1) 尾段必须从完整句子起：不能把 `03:05:45` 从冒号处切开
    _time_ln = ("我先核对当前版本是否已生效，再运行回归测试和真实卡片渲染，"
                "然后换新角度查找问题。109/109 通过；进程启动于 03:07:02，"
                "晚于 handoff.py 的 03:05:45，因此现在运行的就是最新代码。")
    _ts = handoff._smart_slice(_time_ln, 100)
    _tail_part = _ts.split("…", 1)[-1] if "…" in _ts else _ts
    check("尾段不从半截时间开始（03:05:45 不被切开）",
          not _tail_part[:6].strip()[:2].isdigit() or _tail_part.startswith("因此"),
          repr(_tail_part[:24]))

    # 2) 尾段不能从路径中间开始（`backend/...` 被切成 `s/...`）
    _path_ln = ("我先核对当前版本是否已生效，再运行回归测试。" * 3 +
                "请运行 python backend/_feature_tests/test_handoff_card.py 查看结果。")
    _ps = handoff._smart_slice(_path_ln, 120)
    _pt = _ps.split("…", 1)[-1] if "…" in _ps else _ps
    check("尾段不从路径中间开始（不出现 s/test_...）",
          not _pt.startswith("s/") and not _pt.startswith("andoff"),
          repr(_pt[:24]))

    # 3) 存储层不能再做头尾拼接（碎片一旦落盘就固化，render 救不回来）
    _frag_src = ("结论：通过\n通过 327 / 失败 0\n\n"
                 + "我先核对当前版本是否已生效，并运行回归测试。" * 120)
    _stored = handoff._clip(_frag_src, 8000, ellipsis=False, per_line_cap=0)
    check("存储层不做头尾拼接（长回复不出现中段省略号）",
          "…" not in _stored, repr(_stored[:60]))

    # 4) 改动清单按文件合并（交替改两个文件时，相邻合并从来不生效）
    _chs = []
    for i in range(6):
        _chs.append({"rel": "a.py", "note": "n%d" % i, "added": 1, "removed": 0,
                     "time": "2026-11-18 04:0%d:00" % i})
        _chs.append({"rel": "b.py", "note": "m%d" % i, "added": 2, "removed": 1,
                     "time": "2026-11-18 04:1%d:00" % i})
    _pre, _post = handoff._split_changes(_chs, "")
    check("改动清单按文件合并（12 条 → 2 个文件）", len(_pre) == 2, "len=%d" % len(_pre))
    _line = handoff._fmt_change(_pre[0])
    check("合并后标出「共 N 次」", "共 6 次" in _line, _line[:80])
    check("合并后累加增删行数", "+6 -0 行" in _line or "+6" in _line, _line[:80])

    print("")
    print("结果：%d 项，失败 %d" % (CHECKS[0], len(FAILS)))
    if FAILS:
        print("失败项：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
