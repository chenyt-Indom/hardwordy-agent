# -*- coding: utf-8 -*-
"""交接卡「临时脚本过滤」回归测试。

背景（2026-11-18 实测）：last.json 的 changes 里 30 条有 8 条是 _probe_ / _guard_
探针脚本，占了 27% 名额，而这些文件早就删了 —— 真实代码改动被挤出卡片。
根因是 _SCRATCH_PREFIXES 只认 _pending_ / _tmp_，_probe_ 和 _guard_ 都漏了。

跑法：python backend/_feature_tests/test_scratch_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import handoff

_ok = 0
_fail = 0


def check(name, cond, extra=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   " + name)
    else:
        _fail += 1
        print("  FAIL " + name + ("  " + str(extra) if extra else ""))


def main():
    print("=" * 60)
    print("临时脚本过滤 _is_scratch")
    print("=" * 60)

    # 1) 各种前缀的探针都必须被过滤
    probes = [
        "backend/_probe_w1.py",
        "backend/_probe_guard.py",
        "backend/_guard_probe_target.py",
        "backend/_tmp_maptest.py",
        "backend/_pending_某功能.py",
        "backend/_w1.py",            # 无已知前缀的单下划线变体
        "backend/_scratch3.py",
        "_probe_root.py",            # 不带 backend/ 前缀
        "backend\\_probe_win.py",    # 反斜杠路径
    ]
    for p in probes:
        check("过滤 " + p, handoff._is_scratch(p) is True, handoff._is_scratch(p))

    # 2) 真实代码文件一个都不许误伤
    real = [
        "backend/tools.py",
        "backend/handoff.py",
        "backend/main.py",
        "backend/deepseek_client.py",
        "backend/__init__.py",       # 双下划线正式文件
        "frontend/index.html",
        "run.py",
    ]
    for p in real:
        check("保留 " + p, handoff._is_scratch(p) is False, handoff._is_scratch(p))

    # 3) _feature_tests 下：test_ 是真测试，其余中文名 .py 是暂存脚本
    check("保留 test_handoff_card.py",
          handoff._is_scratch("backend/_feature_tests/test_handoff_card.py") is False)
    check("过滤 _feature_tests/画布对话.py",
          handoff._is_scratch("backend/_feature_tests/画布对话.py") is True)

    # 4) 空值不崩
    for v in (None, "", "   "):
        check("空值不崩 %r" % (v,), handoff._is_scratch(v) is False)

    # 5) 真实卡片里不许再出现探针（这条是本次 bug 的直接回归）
    try:
        import json
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(root, "data", "handoff", "last.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            dirty = [c.get("rel") for c in d.get("changes", [])
                     if handoff._is_scratch(c.get("rel"))]
            check("真实卡片无探针残留", not dirty, dirty[:5])
        else:
            print("  SKIP 真实卡片（文件不存在）")
    except Exception as e:
        print("  SKIP 真实卡片（%s）" % e)

    print()
    print("结果：%d 项，失败 %d" % (_ok + _fail, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
