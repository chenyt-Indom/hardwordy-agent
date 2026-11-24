# -*- coding: utf-8 -*-
"""动作状态键 / 临时探针过滤 回归（2026-11-24 清理的遗留问题）。

这一轮修的三处遗留缺陷，都在这里锁死，防止再退化：

1) create_plan 原来把动作状态写成 `state`，而全系统读的是 `status` ——
   后果是这类动作在统计里成 '?'、既不进「待批准」也不算「已完成」。
   实测代价：教训「跑分退步」的修复任务就这么卡了几天没被执行。
2) list_plans 的 actions_done / actions_need_review 也只读 `state`，
   于是列表接口的「已完成 / 待复核」永远显示 0（前端看板失真）。
3) 教训里要求「静态扫描跳过 _tmp_ / _probe_ 前缀」，代码只跳了 _pending_，
   残留探针一直被算进静态分。

全部离线、不联网、不读也不写用户真实的 plans.json。
跑法：python backend/_feature_tests/test_status_key_and_scratch.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import self_iterate as SI          # noqa: E402
from backend import code_audit as CA            # noqa: E402

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
    print("动作状态键 / 临时探针过滤 回归")
    print("=" * 62)

    real_load, real_save = SI.load_plans, SI.save_plans
    store = {"items": []}
    SI.load_plans = lambda: store
    SI.save_plans = lambda d: None
    try:
        # ---- 1) create_plan 必须写 status ----
        r = SI.create_plan("回归：状态键", actions=[
            "字符串动作",
            {"title": "字典动作", "state": "approved"},
            {"title": "已带状态", "status": "failed"},
        ])
        acts = ((r or {}).get("plan") or {}).get("actions") or []
        check("create_plan 建出 3 条动作", len(acts) == 3, acts)
        check("字符串动作 status=pending",
              acts and acts[0].get("status") == "pending", acts[:1])
        check("字符串动作不留 state 键",
              acts and "state" not in acts[0], acts[:1])
        check("旧 state 迁移成 status",
              len(acts) > 1 and acts[1].get("status") == "approved"
              and "state" not in acts[1], acts[1:2])
        check("已有 status 不被覆盖",
              len(acts) > 2 and acts[2].get("status") == "failed", acts[2:3])

        # ---- 2) list_plans 统计口径 ----
        store["items"] = [{"id": "p1", "summary": "回归", "actions": [
            {"id": "a1", "title": "x", "status": "done"},
            {"id": "a2", "title": "y", "state": "done"},        # 老数据兼容
            {"id": "a3", "title": "z", "status": "need_review"},
        ]}]
        lp = SI.list_plans(limit=5)
        row = ((lp or {}).get("items") or [{}])[0]
        check("actions_done 统计 = 2（status + 旧 state）",
              row.get("actions_done") == 2, row)
        check("actions_need_review 统计 = 1",
              row.get("actions_need_review") == 1, row)
        check("actions_total = 3", row.get("actions_total") == 3, row)

        # ---- 3) 跨计划未完成动作标题 ----
        store["items"] = [
            {"id": "p1", "actions": [
                {"id": "a1", "title": "还没做的功能", "status": "pending"},
                {"id": "a2", "title": "做完了的功能", "status": "done"},
                {"id": "a3", "title": "失败过的功能", "status": "failed"},
            ]},
            {"id": "p2", "actions": [
                {"id": "a1", "title": "待批准的功能", "status": "approved"},
                {"id": "a2", "title": "老数据功能", "state": "pending"},
            ]},
        ]
        titles = SI._open_action_titles()
        check("收集 pending 标题", "还没做的功能" in titles, titles)
        check("收集 approved 标题", "待批准的功能" in titles, titles)
        check("兼容旧 state 键", "老数据功能" in titles, titles)
        check("已完成的不收集", "做完了的功能" not in titles, titles)
        check("失败过的允许重排", "失败过的功能" not in titles, titles)
    finally:
        SI.load_plans, SI.save_plans = real_load, real_save

    # ---- 3.5) 复核判「无需修复」的条目不许进待办 ----
    keep, drop = SI._drop_noop_actions([
        {"title": "无需修复：subprocess 全局补丁已覆盖", "status": "pending"},
        {"title": "修 backend/x.py 里的 1 处问题", "status": "pending"},
    ])
    check("「无需修复」被剔除",
          len(drop) == 1 and len(keep) == 1
          and not str(keep[0].get("title")).startswith("无需修复"), (keep, drop))
    check("真待办保留",
          len(keep) == 1 and str(keep[0].get("title")).startswith("修 backend/"), keep)
    k2, d2 = SI._drop_noop_actions([])
    check("空列表不崩", k2 == [] and d2 == [])

    # ---- 4) 静态扫描跳过临时探针 ----
    names = [os.path.basename(str(p)) for p in CA._py_files()]
    check("现有扫描里没有 _tmp_ / _probe_",
          not any(n.startswith(("_tmp_", "_probe_")) for n in names),
          [n for n in names if n.startswith(("_tmp_", "_probe_"))][:5])
    check("真实代码仍在扫描范围（tools.py）", "tools.py" in names)
    check("真实代码仍在扫描范围（code_audit.py）", "code_audit.py" in names)

    probe = os.path.join(_ROOT, "backend", "_tmp_regress_probe.py")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        names2 = [os.path.basename(str(p)) for p in CA._py_files()]
        check("新建的 _tmp_ 文件被跳过", "_tmp_regress_probe.py" not in names2, names2[-5:])
        refs = [os.path.basename(str(p)) for p in CA._ref_files()]
        check("引用计数也跳过 _tmp_", "_tmp_regress_probe.py" not in refs)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass

    print()
    print("结果：%d 项，失败 %d" % (len(PASS) + len(FAIL), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
