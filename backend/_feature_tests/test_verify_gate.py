# -*- coding: utf-8 -*-
"""回归：结束卡片的绿色必须等于「任务真的全部做完」。

真实误报（2026-11-24 用户报）：
  「结果卡片化」那条计划 8 条动作只做完 1 条、其余 7 条还待批准，
  但重启后复核卡照样显示绿色「已验证 / 任务结束 · 重启后复核已验证」。

根因：旧判据是 `ok = (verdict == "ok")`，而 verdict 只看
  「重启后总分没有退步」—— 那只证明程序起得来，证明不了任务做完了。

用户给的判定标准（原话）：
  「绿色已验证是验证到完全没问题且功能已经能够完全正常运转才显示的啊，
    标示着任务成功结束；如果不能正常运作显示红色字迭代失败即将修复，
    然后重新反复验证修复直到完全正常为止。」

本测试锁住：
  ① _judge_task：全部 done 才算成功；有 pending / failed 一律失败；
  ② _push_post_verify_card：任务没做完时**不许**给 verified，必须 failed
     且徽标写明「迭代失败 · 即将修复」；
  ③ 真坏了（退步/量不到分/有 failed）才重排修复计划；单纯待批准不重复造计划；
  ④ self_iterate 的「已完成」同样要求 0 失败。
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

    from backend import iterloop as IL
    from backend import self_iterate as SI

    # ---------- 假计划（不碰真实数据） ----------
    real_load = SI.load_plans
    SI.load_plans = lambda: {"items": [
        {"id": "p_all", "actions": [{"id": "a1", "state": "done"},
                                    {"id": "a2", "state": "done"}]},
        {"id": "p_half", "actions": [{"id": "a1", "state": "done"},
                                     {"id": "a2", "state": "pending"},
                                     {"id": "a3", "state": "pending"}]},
        {"id": "p_bad", "actions": [{"id": "a1", "state": "done"},
                                    {"id": "a2", "state": "failed"}]},
        {"id": "p_empty", "actions": []},
    ]}

    # ---------- ① _judge_task ----------
    try:
        r = IL._judge_task("p_all")
        ck("全部 done → 成功", r.get("ok") is True and r.get("verdict") == "success",
           str(r.get("why")))
        r = IL._judge_task("p_half")
        ck("有 pending → 失败", r.get("ok") is False
           and r.get("verdict") == "failed", str(r.get("why")))
        ck("失败原因说清还剩几条", "2" in str(r.get("why")), str(r.get("why")))
        r = IL._judge_task("p_bad")
        ck("有 failed → 失败", r.get("ok") is False and r.get("failed") == 1)
        r = IL._judge_task("p_empty")
        ck("计划无动作 → 失败", r.get("ok") is False)
        r = IL._judge_task("")
        ck("无任务上下文 → ok=None（不硬判）", r.get("ok") is None)
        r = IL._judge_task("no-such-plan")
        ck("计划不存在 → 失败", r.get("ok") is False)

        # ---------- ② 卡片绿/红 ----------
        pushed = []
        replans = []
        real_push, real_rsi = IL.push_card, IL._rsi_snapshot
        real_replan = getattr(SI, "_schedule_replan", None)
        IL.push_card = lambda c: pushed.append(c) or {"ok": True, "id": "x"}
        IL._rsi_snapshot = lambda: None
        SI._schedule_replan = lambda **kw: replans.append(kw) or {"ok": True}

        try:
            # 分数没退步 + 任务全部做完 → 绿
            IL._push_post_verify_card({"verdict": "ok", "total": 216, "prev": 210,
                                       "delta": 6, "plan_id": "p_all"})
            c = pushed[-1]
            ck("全做完 → verified", c.get("state") == "verified",
               "%s / %s" % (c.get("state"), c.get("badge")))
            ck("绿色徽标是「已验证」", c.get("badge") == "已验证", str(c.get("badge")))

            # 分数没退步、但任务没做完 → 必须红（这就是用户报的那次）
            IL._push_post_verify_card({"verdict": "ok", "total": 216, "prev": 210,
                                       "delta": 6, "plan_id": "p_half"})
            c = pushed[-1]
            ck("没做完 → 不许绿", c.get("state") != "verified", str(c.get("state")))
            ck("没做完 → failed", c.get("state") == "failed")
            ck("徽标写「迭代失败 · 即将修复」",
               "迭代失败" in str(c.get("badge")) and "修复" in str(c.get("badge")),
               str(c.get("badge")))
            ck("卡片带 task 明细", isinstance(c.get("task"), dict)
               and c["task"].get("pending") == 2, str(c.get("task")))
            ck("纯待批准不重复造修复计划",
               (c.get("replan") or {}).get("skipped") is True, str(c.get("replan")))
            ck("纯待批准没调 _schedule_replan", len(replans) == 0, str(len(replans)))

            # 分数退步 → 红 + 要修
            IL._push_post_verify_card({"verdict": "worse", "total": 200,
                                       "prev": 210, "delta": -10,
                                       "plan_id": "p_all"})
            c = pushed[-1]
            ck("分数退步 → failed", c.get("state") == "failed")
            ck("分数退步 → 重排修复", len(replans) == 1, str(len(replans)))

            # 量不到分 → 红 + 要修
            IL._push_post_verify_card({"verdict": "error", "error": "boom",
                                       "plan_id": "p_all"})
            c = pushed[-1]
            ck("量不到分 → failed", c.get("state") == "failed")
            ck("量不到分 → 重排修复", len(replans) == 2, str(len(replans)))

            # 有动作失败 → 红 + 要修
            IL._push_post_verify_card({"verdict": "ok", "total": 216, "prev": 210,
                                       "delta": 6, "plan_id": "p_bad"})
            c = pushed[-1]
            ck("有动作失败 → failed", c.get("state") == "failed")
            ck("有动作失败 → 重排修复", len(replans) == 3, str(len(replans)))
        finally:
            IL.push_card, IL._rsi_snapshot = real_push, real_rsi
            if real_replan is not None:
                SI._schedule_replan = real_replan
    finally:
        SI.load_plans = real_load

    # ---------- ④ 源码口径：self_iterate 的「已完成」也要求 0 失败 ----------
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "self_iterate.py"), encoding="utf-8").read()
    ck("self_iterate 已完成要求 _bad2 == 0",
       "_done2 > 0 and _bad2 == 0" in src)
    ck("self_iterate 有「迭代失败 · 即将修复」分支",
       "迭代失败 · 即将修复" in src)
    isrc = open(os.path.join(root, "iterloop.py"), encoding="utf-8").read()
    ck("iterloop 绿色判定带了 task_ok",
       "score_ok and task_ok is not False" in isrc)

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    if fails:
        print("失败项：" + "、".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
