# -*- coding: utf-8 -*-
"""回归：一键拉起必须能真的跑起来，且任务名要和「要执行的动作」一致。

真实故障（2026-11-24 用户报，两条都在真机上复现）：
  ① 点「▶ 拉起」永远失败 —— 「这个计划没有已批准的动作（5 条待批准）」。
     老计划的动作全是 pending，而「批准」按钮在另一个抽屉里、这条老计划
     往往已经不在那儿了 → 死循环：拉起让你先批准，却找不到批准入口。
  ② 迭代卡片上的名字和用户批准的不是同一个 —— 原来取「第一条动作」，
     用户批的可能是第 5 条；计划被合并/改写后更是完全对不上。

本测试直接跑 `iter_runner.revive` / `_plan_title`（不是只测常量），
用临时数据目录，不碰生产数据、不真的提交任务。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass


def run() -> int:
    old = os.environ.get("MM_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="mm_revive_")
    os.environ["MM_DATA_DIR"] = tmp
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    def write_plans(items):
        p = str(SI._PLANS)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False)

    try:
        from backend import iter_runner as IR
        from backend import self_iterate as SI

        calls = {"submit": None}
        orig_submit = IR.submit

        def fake_submit(plan_id, source="auto", title="", max_n=0):
            calls["submit"] = {"plan_id": plan_id, "source": source,
                               "title": title}
            return {"ok": True, "state": "running", "tid": "t_x"}

        IR.submit = fake_submit
        try:
            # ① 全 pending（用户遇到的场景）→ 应代批并提交成功
            write_plans([{"id": "p_revive", "summary": "测试计划", "_upd": 1.0,
                          "actions": [
                              {"id": "a1", "title": "动作一",
                               "status": "pending", "files": []},
                              {"id": "f5", "title": "答完给张卡",
                               "status": "pending", "files": []}]}])
            r = IR.revive(kind="failed", tid="t_old", plan_id="p_revive")
            ck("全 pending 时拉起成功（不再报『没有已批准的动作』）",
               bool(r.get("ok")), str(r)[:140])
            ck("确实代批了 2 条", int(r.get("auto_approved") or 0) == 2,
               "auto_approved=%s" % r.get("auto_approved"))
            acts = (SI.get_plan("p_revive").get("plan") or {}).get("actions") or []
            ck("批准真的落盘了（读回是 approved）",
               bool(acts) and all(a.get("status") == "approved" for a in acts),
               str([a.get("status") for a in acts]))
            ck("提交用的是 source=approved",
               (calls["submit"] or {}).get("source") == "approved",
               str(calls["submit"]))
            ck("返回的任务名＝要执行的动作（不是旧卡片名）",
               r.get("title") == "动作一（等 2 条）", "title=%s" % r.get("title"))

            # ② 只批准一条时，名字必须就是那一条（用户最核心的抱怨）
            write_plans([{"id": "p2", "summary": "s", "_upd": 1.0,
                          "actions": [
                              {"id": "a1", "title": "动作一", "status": "pending"},
                              {"id": "f5", "title": "答完给张卡",
                               "status": "approved"}]}])
            ck("只批 f5 时，标题＝答完给张卡",
               IR._plan_title("p2") == "答完给张卡", IR._plan_title("p2"))
            ck("只批一条时不该出现『等 N 条』",
               "等" not in IR._plan_title("p2"), IR._plan_title("p2"))

            # ③ 熔断项（need_review）绝不能被「拉起」自动放行
            write_plans([{"id": "p3", "summary": "s", "_upd": 1.0,
                          "actions": [{"id": "a1", "title": "熔断项",
                                       "status": "need_review"}]}])
            calls["submit"] = None
            r3 = IR.revive(kind="failed", tid="t", plan_id="p3")
            ck("熔断动作不会被自动放行", r3.get("ok") is False, str(r3)[:110])
            ck("熔断场景没有提交任务", calls["submit"] is None)

            # ④ 已经全 done 的计划：不能假装提交成功（真 submit 会拒）
            write_plans([{"id": "p4", "summary": "s", "_upd": 1.0,
                          "actions": [{"id": "a1", "title": "已做",
                                       "status": "done"}]}])
            r4 = IR.revive(kind="failed", tid="t", plan_id="p4")
            ck("全 done 时不代批（auto_approved=0）",
               int(r4.get("auto_approved") or 0) == 0, str(r4)[:110])
        finally:
            IR.submit = orig_submit
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old

    print("\n%s（失败 %d 项）" % ("通过" if not fails else "未通过", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
