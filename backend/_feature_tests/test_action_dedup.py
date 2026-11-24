# -*- coding: utf-8 -*-
"""动作跨计划去重的离线回归 —— 不跑模型、不联网、不改数据。

为什么要有（2026-11-24 实测）：
  用户问「积压 172 条为什么降不下来」。查真实数据发现模型**直接给的 actions
  从来没和历史未执行项比对过** —— 只有 _features_to_actions 那条路有去重。
  结果同一件事被反复提：
    「回复形态闸门」42 次（10 条至今 pending）
    「数字人」      25 次
    「答完给张卡」  13 次
  积压只增不减 → backlog_gate（30 条）永久封死对话触发。

这里验四类事：
  1) 真重复能认出来（用**真实历史标题**，不是编的）；
  2) 不同的事不被误杀（宁可漏拦，不能把正常动作全废掉）；
  3) _mark_dup_actions 标 dup 的数量对、且标完不再是 pending；
  4) 同一批内部重复也要拦（模型一轮可能把一件事写两遍）。

跑法：python backend/_feature_tests/test_action_dedup.py
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

from backend import self_iterate as SI             # noqa: E402

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
    print("动作跨计划去重回归")
    print("=" * 62)

    # ---------- 1) 真实重复对：必须认出来 ----------
    # 全部取自 plans.json 里的真实标题，逐字抄的
    dup_pairs = [
        ("回复形态闸门：结论前置 + 三段式收尾 + 长度上限",
         "回复形态闸门：结论前置 + 过程折叠"),
        ("回复形态闸门：结论前置 + 三段式收尾 + 长度上限",
         "回复结论前置 + 过程折叠 + 长度上限"),
        ("修 evolve_budget.py:101 静默吞异常（当前唯一压分点）",
         "修掉 evolve_budget 唯一压分点：静默吞异常改为具体异常+actlog"),
        ("落地「说一句就开工」悬浮球（前端入口+后端触发）",
         "说一句就开工：按住说话的悬浮球"),
        ("落地「答完给张卡」两张可交互结果卡",
         "答完给张卡：回答末尾附可交互小卡"),
    ]
    for a, b in dup_pairs:
        check("认出重复：%s ↔ %s" % (a[:16], b[:16]),
              SI._similar_title(a, b) is True)

    # ---------- 2) 不同的事：不许误杀 ----------
    diff_pairs = [
        ("修 fail-01：读不存在路径时回复措辞被判『假装读到了』",
         "幽灵修复闸门：改动前必须证明失败真实存在"),
        ("iter_runner 动作类型绑定校验",
         "计划层 status 回填：消除「待批准」常亮"),
        ("修 test_anchor_diagnose.py:141 静默吞异常",
         "加一条「改动前先查真实失败证据」的闸门"),
    ]
    for a, b in diff_pairs:
        check("不误杀：%s ↔ %s" % (a[:14], b[:14]),
              SI._similar_title(a, b) is False)

    # ---------- 3) _mark_dup_actions：标对、标完不进 pending ----------
    open_titles = [
        "回复形态闸门：结论前置 + 过程折叠",
        "修掉 evolve_budget 唯一压分点：静默吞异常",
    ]
    acts = [
        {"title": "回复形态闸门：结论前置 + 三段式收尾 + 长度上限",
         "status": "pending"},
        {"title": "修 evolve_budget.py:101 静默吞异常（当前唯一压分点）",
         "status": "pending"},
        {"title": "幽灵修复闸门：改动前必须证明失败真实存在",
         "status": "pending"},
    ]
    out, n = SI._mark_dup_actions(acts, open_titles)
    check("标 dup 数量 = 2（两条重复）", n == 2, "实际 %r" % (n,))
    check("重复项 status 变成 dup", out[0]["status"] == "dup"
          and out[1]["status"] == "dup")
    check("dup 项带说明（不是静默丢弃）",
          "重复" in str(out[0].get("note") or ""))
    check("新动作仍是 pending（没被误杀）",
          out[2]["status"] == "pending")
    check("dup 不在待批准口径里（统计只认 pending/need_review）",
          all(x["status"] not in ("pending", "need_review")
              for x in out[:2]))

    # ---------- 4) 同一批内部重复也要拦 ----------
    acts2 = [
        {"title": "落地「说一句就开工」悬浮球（前端入口+后端触发）",
         "status": "pending"},
        {"title": "说一句就开工：按住说话的悬浮球", "status": "pending"},
    ]
    out2, n2 = SI._mark_dup_actions(acts2, [])
    check("批内重复也拦（第 2 条标 dup）", n2 == 1 and out2[1]["status"] == "dup")

    # ---------- 5) 空输入不炸 ----------
    try:
        SI._mark_dup_actions([], [])
        SI._mark_dup_actions(None, None)
        ok_empty = True
    except Exception as e:                              # noqa: BLE001
        ok_empty = False
        check("空输入不抛异常", False, str(e)[:100])
    if ok_empty:
        check("空输入不抛异常", True)

    # ---------- 6) 真的接进主流程了（源码级） ----------
    try:
        with open(os.path.join(_ROOT, "backend", "self_iterate.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
    except Exception as e:                              # noqa: BLE001
        src = ""
        check("能读 self_iterate.py", False, str(e)[:100])
    check("生成 actions 处调用了 _mark_dup_actions",
          "_mark_dup_actions(actions, _open_action_titles())" in src)

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
