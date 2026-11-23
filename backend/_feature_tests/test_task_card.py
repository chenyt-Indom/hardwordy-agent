# -*- coding: utf-8 -*-
"""任务结束卡片（重启后复核 → 弹卡「已验证」）的离线回归。

用户 2026-11-23 原话：
  「自动迭代重启之后没有弹出卡片并显示已验证然后结束，结束任务时卡片必须有弹出来」

这里验四件事（全部不联网、不重启、不改代码）：
  1) 待播报队列：push → pending → ack 出队；
  2) 复核通过 → 卡片 state=verified / 徽标「已验证」/ 带前后分数与 RSI；
  3) 复核退步 → 卡片 state=failed，并且**自动重排修复计划**被调用一次
     （_schedule_replan 用替身，绝不真跑 reflect —— 那会真去改源码）；
  4) 复核异常（量不到分）→ 同样是 failed 卡，写清原因。

跑法：python backend/_feature_tests/test_task_card.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import iterloop as IL          # noqa: E402
from backend import self_iterate as SI      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def main():
    print("=" * 62)
    print("任务结束卡片回归")
    print("=" * 62)

    # 替身：绝不真跑 reflect
    called = {"n": 0, "kw": None}
    orig = SI._schedule_replan

    def _fake_replan(**kw):
        called["n"] += 1
        called["kw"] = kw
        return {"ok": True, "scheduled": True, "in_seconds": 6}

    SI._schedule_replan = _fake_replan
    try:
        IL.ack_cards([])                     # 干净起点

        # ---- 1 队列
        r = IL.push_card({"kind": "t", "state": "verified", "badge": "已验证",
                          "title": "队列自检"})
        check("push_card 返回 ok", bool(r.get("ok")), str(r))
        items = IL.pending_cards()
        check("pending_cards 拿得到刚推的卡",
              len(items) == 1 and items[0].get("badge") == "已验证", str(items)[:200])
        cid = items[0].get("id")
        ack = IL.ack_cards([cid])
        check("ack 后出队", ack.get("left") == 0 and not IL.pending_cards(), str(ack))

        # ---- 2 复核通过
        IL.ack_cards([])
        IL._push_post_verify_card({"verdict": "ok", "total": 157.15,
                                   "prev": 155.09, "delta": 2.06,
                                   "plan_id": "p_ok"})
        card = (IL.pending_cards() or [{}])[-1]
        check("通过 → state=verified", card.get("state") == "verified", str(card)[:200])
        check("通过 → 徽标是「已验证」", card.get("badge") == "已验证")
        check("通过 → 带前后分数", card.get("total") == 157.15 and card.get("prev") == 155.09)
        check("通过 → 带 RSI 快照", isinstance(card.get("rsi"), dict)
              and card["rsi"].get("score") is not None, str(card.get("rsi"))[:160])
        check("通过 → 不重排修复计划", called["n"] == 0, "called=%d" % called["n"])

        # ---- 3 复核退步
        IL.ack_cards([])
        IL._push_post_verify_card({"verdict": "worse", "total": 150.0,
                                   "prev": 155.0, "delta": -5.0,
                                   "plan_id": "p_bad"})
        card = (IL.pending_cards() or [{}])[-1]
        check("退步 → state=failed", card.get("state") == "failed", str(card)[:200])
        check("退步 → 徽标是「复核未通过」", card.get("badge") == "复核未通过")
        check("退步 → 原因写清退了几分", "退步 5.00" in str(card.get("why")),
              str(card.get("why")))
        check("退步 → 自动重排了一次修复计划", called["n"] == 1, "called=%d" % called["n"])
        check("退步 → 重排带了失败上下文",
              isinstance(called["kw"], dict)
              and "post_restart" in str(called["kw"].get("context")),
              str(called["kw"])[:160])
        check("退步 → 卡片上标了重排结果",
              isinstance(card.get("replan"), dict) and card["replan"].get("ok") is True,
              str(card.get("replan")))

        # ---- 4 量不到分
        IL.ack_cards([])
        called["n"] = 0
        IL._push_post_verify_card({"verdict": "error", "error": "重启后评分拿不到",
                                   "plan_id": "p_err"})
        card = (IL.pending_cards() or [{}])[-1]
        check("异常 → state=failed", card.get("state") == "failed", str(card)[:200])
        check("异常 → 原因里带原文", "评分拿不到" in str(card.get("why")),
              str(card.get("why")))
        check("异常 → 也重排了修复计划", called["n"] == 1, "called=%d" % called["n"])

        # ---- 5 队列上限
        IL.ack_cards([])
        for i in range(30):
            IL.push_card({"kind": "t", "state": "verified", "title": "n%d" % i})
        n = len(IL.pending_cards())
        check("队列有上限（不会无限长）", n <= IL._CARD_KEEP, "n=%d" % n)

        # ---- 6 接线检查（静态）：本项目历史上最常犯的错是「加了函数没接线」，
        #      所以这里把「谁调谁」也当断言来验。
        _rd = lambda rel: open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        _si = _rd("backend/self_iterate.py")
        check("execute_approved 收尾会推结束卡",
              "任务结束 · 本轮迭代已完成" in _si)
        check("会自动重启的轮次不重复推卡（等复核卡收尾）",
              'if not _ar2.get("scheduled"):' in _si)
        _main = _rd("backend/main.py")
        check("取卡接口已注册", "/api/selfiter/cards" in _main)
        check("出队接口已注册", "/api/selfiter/cards/ack" in _main)
        check("重复的启动复核钩子已删",
              "def _loop_post_verify_on_boot" not in _main)
        _js = _rd("frontend/taskcard.js")
        check("前端轮询取卡", "/api/selfiter/cards" in _js)
        check("前端渲染后出队", "/api/selfiter/cards/ack" in _js)
        check("taskcard.js 已被 index.html 引入",
              "/static/taskcard.js" in _rd("frontend/index.html"))
    finally:
        SI._schedule_replan = orig
        IL.ack_cards([])                     # 收尾清空，别把测试卡留在真实队列里

    print()
    print("=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   !! " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
