# -*- coding: utf-8 -*-
"""可达性检查 _fe_calls 的回归断言。

背景（2026-11-18）：用户报「点了迭代说成功，功能却点不到」。
查出来 f6 那轮新建 backend/canvas.py + POST /api/canvas/command，
前端一处调用都没有，但语法/导入/回归全过 → 被判成功。

第一版修法用「路径前三段出现在前端就算有调用方」，自查发现仍然漏：
`/api/canvas/command` 的键是 `/api/canvas`，同一模块下再加一个
`/api/canvas/other` 也会被当成「已接上」。这里把正确判据钉死。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from backend.iter_agent import _fe_calls, _is_scratch  # noqa: E402

PASS = FAIL = 0


def ck(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s" % name)


print("=" * 60)
print("可达性检查：新接口有没有前端调用方")
print("=" * 60)

print("\n[1] 引号里的完整路径才算数")
ck('双引号完整路径命中', _fe_calls("/api/canvas/command",
                            'fetch("/api/canvas/command", {method:"POST"})'))
ck('单引号完整路径命中', _fe_calls("/api/canvas/command",
                            "fetch('/api/canvas/command')"))
ck('反引号完整路径命中', _fe_calls("/api/canvas/command",
                            "fetch(`/api/canvas/command`)"))

print("\n[2] ★ 同一模块下的新接口不能被前面那个带过（第一版的漏洞）")
ck('/api/canvas/other 未调用 → 必须判无调用方',
   not _fe_calls("/api/canvas/other", 'fetch("/api/canvas/command")'))
ck('/api/canvas 前缀相同但路径不同 → 判无调用方',
   not _fe_calls("/api/canvas/del", 'fetch("/api/canvas/command")'))
ck('完全无关的路径 → 判无调用方',
   not _fe_calls("/api/brand/new", 'fetch("/api/canvas/command")'))
ck('裸前缀（不带引号）不算调用',
   not _fe_calls("/api/canvas/other", '// 说明：/api/canvas 下面还有接口'))

print("\n[3] 明确的拼接写法不算误杀")
ck('引号里正好是 "/api/canvas/" → 认作有调用方',
   _fe_calls("/api/canvas/anything", 'fetch("/api/canvas/" + cmd)'))
ck("单引号拼接同样认", _fe_calls("/api/canvas/x", "fetch('/api/canvas/' + c)"))
ck('拼接前缀不一致时不认', not _fe_calls("/api/brand/x", 'fetch("/api/canvas/" + c)'))

print("\n[4] 空输入不炸")
ck('空前端正文 → False', not _fe_calls("/api/x", ""))
ck('空路径不抛异常', isinstance(_fe_calls("", "abc"), bool))

print("\n[5] 临时脚本判据（配套）")
ck('_probe_ 前缀算临时', _is_scratch("backend/_probe_w1.py"))
ck('_guard_ 前缀算临时', _is_scratch("backend/_guard_x.py"))
ck('__init__.py 不算临时', not _is_scratch("backend/__init__.py"))
ck('正常模块不算临时', not _is_scratch("backend/canvas.py"))
ck('测试文件不算临时', not _is_scratch("backend/_feature_tests/test_reachability.py"))

print("\n" + "=" * 60)
print("通过 %d / 失败 %d" % (PASS, FAIL))
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
