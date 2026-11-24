# -*- coding: utf-8 -*-
"""「已经跑完的计划不许再提交」的离线回归。

用户 2026-11-24 问「为什么失败没有重试」，查出来的真问题：
  11-24 20:25:28 有一条 failed（耗时 25.9s，错误「这次反思没有已批准的动作」），
  它对应的计划 8 条动作早已 7 done + 1 rejected —— 一条待办都没有。
  再提交一次必然 0 秒空跑，却会在时间线上留一张红卡，让人以为整套功能坏了。

修法：submit() 入队前先看计划还有没有 pending/approved 的动作，没有就直接拒绝，
      并说清「已经执行完了」。本文件把这条钉住。

安全约束（很重要）
------------------
本测试**绝不真的起任务、绝不写 task.json**：
  · 被拦的路径在 return 之前不会碰 _save()，可以直接测；
  · 需要放行的路径会把 Thread / _save / _emit / _live_add 全部换成替身，
    并备份还原 _STATE，跑完再复原。

跑法：python backend/_feature_tests/test_submit_finished.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import iter_runner as IR              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _plan(statuses):
    return {"plan": {"id": "p_test", "title": "测试计划",
                     "actions": [{"id": "a%d" % i, "status": s, "title": "动作%d" % i}
                                 for i, s in enumerate(statuses)]}}


class _FakeThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def is_alive(self):
        return True


def main():
    print("=" * 62)
    print("已跑完的计划不许再提交")
    print("=" * 62)

    orig_get = IR._si.get_plan
    orig_thread = IR.threading.Thread
    orig_save, orig_emit, orig_live = IR._save, IR._emit, IR._live_add
    orig_state = {k: (list(v) if isinstance(v, list) else v)
                  for k, v in IR._STATE.items()}
    try:
        # ---- 1. 全 done → 必须拒绝 ----
        IR._si.get_plan = lambda pid: _plan(["done"] * 8)
        r = IR.submit("p_test", source="auto", title="测试")
        check("全 done → ok=False", r.get("ok") is False, repr(r))
        check("全 done → state=finished", r.get("state") == "finished", repr(r))
        check("全 done → 原因写明「已经执行完了」",
              "已经执行完" in str(r.get("error") or ""), repr(r.get("error")))
        check("全 done → 不返回 tid（没入队）", not r.get("tid"), repr(r.get("tid")))

        # ---- 2. done + rejected 混合 → 也要拒绝 ----
        IR._si.get_plan = lambda pid: _plan(["done"] * 7 + ["rejected"])
        r2 = IR.submit("p_test", source="auto")
        check("done+rejected → 拒绝", r2.get("ok") is False, repr(r2))
        check("done+rejected → 原因里报出已完成条数",
              "7" in str(r2.get("error") or ""), repr(r2.get("error")))

        # ---- 3. 全 rejected → 拒绝 ----
        IR._si.get_plan = lambda pid: _plan(["rejected", "rejected"])
        r3 = IR.submit("p_test", source="auto")
        check("全 rejected → 拒绝", r3.get("ok") is False, repr(r3))

        # ---- 4. 手动来源同样被拦（不能只拦 auto）----
        IR._si.get_plan = lambda pid: _plan(["done"])
        r4 = IR.submit("p_test", source="manual")
        check("manual 来源同样拒绝（不是只拦 auto）", r4.get("ok") is False, repr(r4))

        # ---- 5. 计划读不到 / 结构异常 → 不拦，交给执行器 ----
        #     （这条只验「不因读不到就误拒」，用替身挡住后续入队）
        IR.threading.Thread = _FakeThread
        IR._save = lambda: None
        IR._emit = lambda *a, **k: None
        IR._live_add = lambda *a, **k: None
        IR._STATE["running"] = None
        IR._STATE["queue"] = []

        IR._si.get_plan = lambda pid: None
        r5 = IR.submit("p_test", source="auto")
        check("计划读不到 → 不拦（交给执行器判断）", r5.get("ok") is True, repr(r5))
        IR._STATE["running"] = None
        IR._STATE["queue"] = []

        # ---- 6. 有 pending → 放行（这是正常路径，不能被误拦）----
        IR._si.get_plan = lambda pid: _plan(["done", "pending"])
        r6 = IR.submit("p_test", source="auto")
        check("有 pending → 放行", r6.get("ok") is True, repr(r6))
        IR._STATE["running"] = None
        IR._STATE["queue"] = []

        # ---- 7. 有 approved → 放行 ----
        IR._si.get_plan = lambda pid: _plan(["done", "approved"])
        r7 = IR.submit("p_test", source="auto")
        check("有 approved → 放行", r7.get("ok") is True, repr(r7))
        IR._STATE["running"] = None
        IR._STATE["queue"] = []

        # ---- 8. get_plan 抛异常 → 不拦 ----
        def _boom(pid):
            raise RuntimeError("故意炸")
        IR._si.get_plan = _boom
        r8 = IR.submit("p_test", source="auto")
        check("读计划抛异常 → 不拦（不因检查失败误拒）", r8.get("ok") is True, repr(r8))
    finally:
        IR._si.get_plan = orig_get
        IR.threading.Thread = orig_thread
        IR._save, IR._emit, IR._live_add = orig_save, orig_emit, orig_live
        IR._STATE.clear()
        IR._STATE.update(orig_state)

    # ---- 9. 接线：这段预检必须真的在 submit 里，且在 auto/manual 分流之前 ----
    src = ""
    try:
        with open(os.path.join(_ROOT, "backend", "iter_runner.py"), encoding="utf-8") as fh:
            src = fh.read()
    except Exception as e:                               # noqa: BLE001
        check("能读 iter_runner.py", False, str(e)[:120])

    i_pre = src.find("这个计划已经执行完了")
    i_split = src.find('if str(source or "auto") != "auto":')
    check("submit 里有「已经执行完了」这段预检", i_pre > 0)
    check("预检在 auto/manual 分流之前（auto 也要拦）",
          0 < i_pre < i_split, "i_pre=%r i_split=%r" % (i_pre, i_split))
    check("预检认 pending / approved 两种可执行状态",
          '"pending", "approved"' in src)

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
