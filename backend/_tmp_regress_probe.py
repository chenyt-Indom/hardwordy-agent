# -*- coding: utf-8 -*-
"""回归探针（临时诊断脚本，`_tmp_` 前缀，工具链会当作暂存文件跳过）。

为什么需要它
------------
`backend/_feature_tests/` 里有几个历史断言文件是在**模块级**直接执行脚本的
（`test_llm_retry.py` 结尾就是 `sys.exit(...)`，`test_map_city_level.py`、
`test_reachability.py`、`test_tool_registry.py` 同样如此，另有几个只提供 `main()`
没有 `run()`）。`regress._load_dynamic()` 只捕获 `Exception`，`SystemExit` 会穿透
把整个回归进程带走 —— 于是 `regress.run_disk()` 拿不到 `@@REGRESS@@` 结果，
表现为「回归子进程没跑起来 / 基线不可用」。

这里把 `sys.exit` 临时换成抛普通异常，让 regress 把那几条记成「加载失败」，
其余用例照常跑完，从而能看清**自己这次改动**有没有引入回归。
跑法：

    python backend/_tmp_regress_probe.py

注意：这是诊断用的，不是验收断言；验收断言在 backend/_feature_tests/ 下。
"""
import json
import sys

sys.path.insert(0, ".")


class _FakeExit(Exception):
    """替代 SystemExit：让 regress 的 `except Exception` 能接住。"""


def _patched_exit(*a, **k):
    raise _FakeExit("exit")


def main(scope="quick"):
    _orig = sys.exit
    sys.exit = _patched_exit
    try:
        from backend import regress as R
        r = R.run(scope)
    finally:
        sys.exit = _orig
    bad = [(i["name"], str(i.get("why"))[:300]) for i in r.get("failed_items", [])]
    print(json.dumps({
        "total": r.get("total"),
        "passed": r.get("passed"),
        "failed": r.get("failed"),
        "skipped": r.get("skipped"),
        "bad": bad,
    }, ensure_ascii=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "quick"))
