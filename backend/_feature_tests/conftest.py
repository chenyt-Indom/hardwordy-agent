# pytest 收集配置
#
# 为什么需要这个文件（2026-11-24 实测）：
#   backend/_feature_tests/ 下有 4 个「脚本式」测试文件 —— 整份代码都在模块顶层执行，
#   末尾直接 `sys.exit(1 if _fail else 0)`。它们本来是用
#       py -3 backend/_feature_tests/test_xxx.py
#   单独跑的，不是 pytest 用例。
#
#   一旦 pytest 收集到它们，模块级 sys.exit 会在 **收集阶段** 抛出 SystemExit，
#   pytest 直接 INTERNALERROR 整场退出 —— 结果就是「全量套件一个用例都没跑」，
#   之前反复验证时看到的 "0 通过 / N 失败" 正是这个假故障，不是代码有问题。
#
#   所以这里显式排除这 4 个脚本，让它们继续用「直接 py 跑」的方式验证；
#   pytest 负责其余用例。新增脚本式文件时，记得加进这个列表。
collect_ignore = [
    "test_llm_retry.py",
    "test_map_city_level.py",
    "test_reachability.py",
    "test_tool_registry.py",
]
