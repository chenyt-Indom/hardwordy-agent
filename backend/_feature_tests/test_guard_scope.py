# -*- coding: utf-8 -*-
"""护栏范围：_feature_tests/ 下只有真正的验收断言才算护栏。

背景（2026-11-24 实测）：
  `_GUARD_DIRS = ("backend/_feature_tests/",)` 让**整个目录**都算护栏，
  包括 `_pending_*.py` 这种待办/临时断言。它们会被正常创建和清理，
  一没文件 `_sha()` 就返回 ""≠快照 sha，`_final_verify` 便判
  「护栏文件被修改（不允许）」→ **整轮回滚**（11-23 18:46 实际发生过）。

跑法：py -3 -m backend._feature_tests.test_guard_scope
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import iter_agent as A  # noqa: E402


def run():
    ok = []

    # ① 硬护栏：这几个文件必须仍是护栏（改坏了就退不回来 / 自己判自己及格）
    for rel in ("backend/selfdev.py", "backend/regress.py",
                "backend/iter_agent.py", "backend/brand.py"):
        assert A._is_guard(rel), "硬护栏被漏掉了：%s" % rel
    ok.append("4 个硬护栏文件仍是护栏")

    # ② 真正的验收断言仍是护栏（模型不许改它来让自己及格）
    for rel in ("backend/_feature_tests/test_rsi_landed_visible.py",
                "backend/_feature_tests/test_guard_scope.py"):
        assert A._is_guard(rel), "验收断言不算护栏了：%s" % rel
    ok.append("验收断言（非下划线开头）仍是护栏")

    # ③ 临时/待办文件**不算**护栏（它们会被正常清理）
    for rel in ("backend/_feature_tests/_pending_盯梢任务哨兵.py",
                "backend/_feature_tests/_probe_now.py",
                "backend/_feature_tests/_tmp_plan.py"):
        assert not A._is_guard(rel), "临时文件仍被当护栏（清理即误判违规）：%s" % rel
    ok.append("下划线开头的临时/待办文件不算护栏")

    # ④ 普通源码不是护栏（否则迭代什么都不能改）
    for rel in ("backend/main.py", "frontend/app.js",
                "backend/_feature_tests_other/test_x.py"):
        assert not A._is_guard(rel), "普通源码被误判成护栏：%s" % rel
    ok.append("普通源码不被误判")

    # ⑤ 路径分隔符 / 前缀写法要能规范化
    assert A._is_guard("backend\\_feature_tests\\test_a.py"), "反斜杠路径没规范化"
    assert not A._is_guard("backend\\_feature_tests\\_pending_a.py"), \
        "反斜杠路径下临时文件仍被当护栏"
    assert A._is_guard("./backend/regress.py"), "开头的 ./ 没被规范化"
    ok.append("路径写法（反斜杠 / ./ 前缀）规范化正确")

    # ⑥ 空值不炸
    assert not A._is_guard(""), "空路径不该算护栏"
    assert not A._is_guard(None), "None 不该算护栏"
    ok.append("空值不抛异常")

    for line in ok:
        print("  [OK] " + line)
    print("全部通过（%d 项）" % len(ok))
    return 0


if __name__ == "__main__":
    sys.exit(run())
