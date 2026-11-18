# -*- coding: utf-8 -*-
"""专家路由的行为（2026-11-18）。

背景：用户问「能不能加入 MoE，想加什么加什么那种」。真 MoE 加不进去
（它是模型骨架），能做的是**路由层**：一句话进来，按性质挑最合适的档位。
这一层就是 backend/expert_router.py。

这个文件钉住四件事：
  1. 分类对不对：闲聊→极速、代码→代码档、长文/权衡→深思、带图→看图档
  2. **绝不抛异常**：它跑在对话主链路上，抛一次整轮就废
  3. 接线安全：路由结果永远不是 "auto"，否则会把 "auto" 当模型名发给 API
  4. 可解释：每次都给 why，用户看得见为什么选它

跑法：python backend/_feature_tests/test_expert_router.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import expert_router as er    # noqa: E402
from backend import deepseek_client as dc  # noqa: E402

_ok = 0
_fail = 0


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


def main():
    print("=" * 60)
    print("专家路由 test_expert_router")
    print("=" * 60)

    # ---------- 1. 分类 ----------
    print("\n[1] 分类：这句话该走哪个专家")
    cases = [
        ("你好", "fast"),
        ("谢谢", "fast"),
        ("在吗", "fast"),
        ("算了", "fast"),
        ("帮我看看这段代码为什么报错", "code"),
        ("写个爬虫", "code"),
        ("这个函数有 bug", "code"),
        ("接口返回 500", "code"),
        ("帮我写一份6天旅行计划", "pro"),
        ("写一篇关于潮汕的文章", "pro"),
        ("为什么天空是蓝的", "pro"),
        ("这两个方案哪个更好", "pro"),
        ("帮我做个PPT", "pro"),
        ("广州到汕头多远", "balanced"),
        ("", "balanced"),
    ]
    for text, want in cases:
        got = er.route(text)["expert"]
        check("route(%r) -> %s" % (text[:14], want), got == want,
              "" if got == want else "got=%s" % got)

    # ---------- 2. 带图 ----------
    print("\n[2] 带图优先（纯文本模型收到图会直接报错）")
    r = er.route("这张图里是什么", has_images=True)
    check("has_images -> vision", r["expert"] == "vision", r["expert"])
    r = er.route("", has_images=True)
    check("空文本 + 带图 -> vision", r["expert"] == "vision", r["expert"])
    r = er.route("帮我看看这段代码", has_images=True)
    check("代码 + 带图 -> vision（图优先）", r["expert"] == "vision", r["expert"])

    # ---------- 3. 绝不抛异常 ----------
    print("\n[3] 绝不抛异常（主链路上抛一次整轮就废）")
    for bad in (None, 123, 4.5, {"a": 1}, ["x"], b"bytes", ""):
        try:
            r = er.route(bad)
            ok = isinstance(r, dict) and bool(r.get("model"))
        except Exception as e:                       # noqa: BLE001
            ok = False
            print("       抛了：%s" % type(e).__name__)
        check("route(%s) 不抛且返回 model" % type(bad).__name__, ok)
    try:
        n = er.note(None)
        check("note(None) 返回 str", isinstance(n, str), repr(n))
    except Exception:                                # noqa: BLE001
        check("note(None) 返回 str", False, "抛异常")

    # ---------- 4. 返回结构合法 ----------
    print("\n[4] 返回结构：model 必须是真模型，thinking/effort 必须合法")
    legal_models = {m["id"] for m in dc.MODELS} - {"deepseek-auto"}
    legal_effort = {None, "low", "medium", "high"}
    for text in ("你好", "写个爬虫", "写一篇报告", "为什么", "", "随便说点什么吧"):
        r = er.route(text)
        check("model 合法（%r）" % text[:8], r["model"] in legal_models, r["model"])
        check("thinking 是 bool（%r）" % text[:8], isinstance(r["thinking"], bool))
        check("effort 合法（%r）" % text[:8], r["effort"] in legal_effort,
              str(r["effort"]))

    # ---------- 5. 接线安全 ----------
    print("\n[5] 接线安全：路由结果永远不是 auto")
    for text in ("你好", "写个爬虫", "写一篇报告", "为什么", ""):
        r = er.route(text)
        check("route(%r) != deepseek-auto" % text[:8],
              r["model"] != "deepseek-auto", r["model"])

    ids = [m["id"] for m in dc.MODELS]
    check("MODELS 里有 deepseek-auto 档", "deepseek-auto" in ids)
    auto = next((m for m in dc.MODELS if m["id"] == "deepseek-auto"), {})
    check("auto 档带 wire_model（防漏到 chat_stream 被 400）",
          bool(auto.get("wire_model")), str(auto.get("wire_model")))
    check("auto 的 wire_model 是真模型",
          auto.get("wire_model") in legal_models, str(auto.get("wire_model")))

    # ---------- 6. 专家表本身 ----------
    print("\n[6] 专家表：id 唯一、model 存在、都写了 desc")
    eids = [e["id"] for e in er.EXPERTS]
    check("id 无重复", len(eids) == len(set(eids)), str(eids))
    for e in er.EXPERTS:
        check("专家 %s 的 model 合法" % e["id"], e["model"] in legal_models, e["model"])
        check("专家 %s 有 desc" % e["id"], bool(str(e.get("desc") or "")))
    check("默认专家存在", er.get(er.DEFAULT_ID) is not None, er.DEFAULT_ID)

    # ---------- 7. 可解释 ----------
    print("\n[7] 可解释：每次都给 why，note 能说清选了什么")
    for text in ("你好", "写个爬虫", "写一篇报告", "为什么", "广州到汕头多远"):
        r = er.route(text)
        check("why 非空（%r）" % text[:8], bool(r.get("why")), r.get("why"))
        n = er.note(r)
        check("note 含专家名（%r）" % text[:8], r["name"] in n)
    check("note 长度合理（<=80 字）", len(er.note(er.route("写个爬虫"))) <= 80,
          "len=%d" % len(er.note(er.route("写个爬虫"))))

    # ---------- 8. list_experts ----------
    print("\n[8] list_experts 给前端/文档用")
    lst = er.list_experts()
    check("返回条数 = 专家数", len(lst) == len(er.EXPERTS), str(len(lst)))
    check("每条都有 id/name", all(x.get("id") and x.get("name") for x in lst))

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (_ok, _fail))
    print("=" * 60)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
