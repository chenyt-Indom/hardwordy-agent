# -*- coding: utf-8 -*-
"""自动迭代侧「无进展检测」· 离线回归。

背景（2026-11-24 用户拍板）：
  对话主循环已经有了无进展检测（见 test_no_progress.py），但**自动迭代这条循环
  没有** —— 那边只有 fail_chain，它只数「连续失败几次」，不看每轮做的事是不是
  同一件。于是「改同一批文件、报同一条错」能一路烧到第 5 轮才停。

要守住的三条边界（任何一条破了都是真故障）：
  A. **必须真能停**：连续 3 轮「同一批文件 + 同一条问题」⇒ 判定原地打转。
  B. **绝不能误伤**：文件集或问题集有任何变化 ⇒ 计数归零。正常的
     「修 A 文件 → 修 B 文件 → 又回到 A」不该被打断。
  C. **计数必须跨重启**：迭代每轮都重启，只存内存等于没有护栏。

为什么不 import backend.iter_runner：它会读 task.json、起线程、依赖
self_iterate 的一堆运行时状态。这里用 ast 把**同一份源码里**的几个纯函数
抠出来执行 —— 测的就是真正在跑的那份逻辑，而不是抄一份副本。

跑法：python backend/_feature_tests/test_iter_no_progress.py
"""
import ast
import hashlib
import os
import re
import sys
import threading

_THIS = os.path.abspath(__file__)
_HERE = os.path.dirname(_THIS)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SRC = os.path.join(_ROOT, "backend", "iter_runner.py")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                       # noqa: BLE001
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _load(src_text=None):
    """从 backend/iter_runner.py 抠出无进展检测的常量与纯函数。"""
    src = src_text if src_text is not None else open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re, "hashlib": hashlib, "threading": threading,
          "_LOCK": threading.RLock(), "_STATE": {}}
    want_f = {"_norm_issue", "_round_fingerprint", "_note_round", "_reset_round"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_f:
            exec(compile(ast.Module(body=[node], type_ignores=[]), _SRC, "exec"), ns)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for nm in ("_NO_PROG_STRIP", "_NO_PROGRESS_LIMIT"):
                if nm in names:
                    exec(compile(ast.Module(body=[node], type_ignores=[]),
                                 _SRC, "exec"), ns)
                    found.add(nm)
    return ns, found, src


print("== 0. 源码里确实有这些定义（不是抄的空壳）")
NS, FOUND, SRC = _load()
check("_NO_PROGRESS_LIMIT 已定义", "_NO_PROGRESS_LIMIT" in FOUND)
check("_NO_PROG_STRIP 已定义", "_NO_PROG_STRIP" in FOUND)
for _fn in ("_norm_issue", "_round_fingerprint", "_note_round", "_reset_round"):
    check("%s 已定义" % _fn, _fn in FOUND)
check("阈值是 3（迭代一轮成本高，三次重复足够判死）",
      NS.get("_NO_PROGRESS_LIMIT") == 3, "实际=%r" % NS.get("_NO_PROGRESS_LIMIT"))

print("\n== 1. 接线检查：三处失败分支都调了，通过处会清零")
check("_note_round 调用 ≥ 3 处（分数退步 / 全局终检 / 验证没过）",
      SRC.count("_note_round(") >= 4,          # 含 1 处定义
      "实际=%d" % SRC.count("_note_round("))
check("_reset_round 调用 ≥ 1 处（验证通过时清零）",
      SRC.count("_reset_round()") >= 1, "实际=%d" % SRC.count("_reset_round()"))
check("no_prog 会落盘（_save 里写了这个键）", '"no_prog"' in SRC)
check("no_prog 会从磁盘恢复（_load 里读这个键）",
      SRC.count('"no_prog"') >= 2, "实际=%d" % SRC.count('"no_prog"'))

print("\n== 2. 归一化：每轮都会变的计数/耗时/地址必须被抹掉")
norm = NS["_norm_issue"]
_a = norm("第 3 次修复后验证仍未通过：test_a.py:123 报 TypeError（耗时 12.5s）")
_b = norm("第 4 次修复后验证仍未通过：test_a.py:456 报 TypeError（耗时 31.2s）")
check("第 N 次 被抹平", "第N次" in _a and _a == _b, "%r vs %r" % (_a, _b))
check("行号被抹平", ":123" not in _a and ":LINE" in _a, repr(_a))
check("耗时被抹平", "12.5" not in _a, repr(_a))
check("连续 N/M 被抹平",
      norm("连续 2/5 次") == norm("连续 4/5 次"),
      "%r vs %r" % (norm("连续 2/5 次"), norm("连续 4/5 次")))
check("时间戳被抹平",
      norm("2026-11-24 18:33:07 失败") == norm("2026-11-25 09:01:02 失败"),
      "%r vs %r" % (norm("2026-11-24 18:33:07 失败"),
                    norm("2026-11-25 09:01:02 失败")))
check("traceback 地址被抹平",
      norm("at 0x7ff8ab12cd 崩溃") == norm("at 0x7ff8ff99aa 崩溃"),
      "%r vs %r" % (norm("at 0x7ff8ab12cd 崩溃"), norm("at 0x7ff8ff99aa 崩溃")))
check("实质内容不被抹掉", "TypeError" in _a, repr(_a))
check("空输入返回空串", norm("") == "" and norm(None) == "")

print("\n== 3. 指纹：同构才算同一轮")
fp = NS["_round_fingerprint"]
F1 = ["backend/a.py", "backend/b.py"]
P1 = ["test_x.py:12 报 KeyError", "代码自检：3 个 mid"]
_f0 = fp("修 A 功能", F1, P1)
check("完全相同 → 指纹相同", fp("修 A 功能", F1, P1) == _f0)
check("文件顺序不同 → 指纹相同（排序去重）",
      fp("修 A 功能", list(reversed(F1)), P1) == _f0)
check("问题顺序不同 → 指纹相同",
      fp("修 A 功能", F1, list(reversed(P1))) == _f0)
check("路径分隔符不同 → 指纹相同",
      fp("修 A 功能", ["backend\\a.py", "backend/b.py"], P1) == _f0)
check("换一个文件 → 指纹不同",
      fp("修 A 功能", ["backend/a.py", "backend/c.py"], P1) != _f0)
check("换一条问题 → 指纹不同",
      fp("修 A 功能", F1, ["test_y.py:12 报 ValueError"]) != _f0)
check("换标题 → 指纹不同", fp("修 B 功能", F1, P1) != _f0)
check("只有行号不同 → 指纹相同（不算进展，但也不算新问题）",
      fp("修 A 功能", F1, ["test_x.py:999 报 KeyError",
                           "代码自检：3 个 mid"]) == _f0)
check("问题多一条 → 指纹不同",
      fp("修 A 功能", F1, P1 + ["新增：语法错误"]) != _f0)

print("\n== 4. 计数：连续 3 轮才停，中间有任何变化就归零")
note = NS["_note_round"]
rst = NS["_reset_round"]
n1, t1 = note("修 A", F1, P1)
check("第 1 轮不触发（n=1）", n1 == 1 and not t1, "n=%r t=%r" % (n1, t1))
n2, t2 = note("修 A", F1, P1)
check("第 2 轮不触发（n=2）", n2 == 2 and not t2, "n=%r t=%r" % (n2, t2))
n3, t3 = note("修 A", F1, P1)
check("第 3 轮触发（n=3）", n3 == 3 and t3, "n=%r t=%r" % (n3, t3))
n4, t4 = note("修 A", F1, P1)
check("第 4 轮仍触发", n4 == 4 and t4, "n=%r t=%r" % (n4, t4))

rst()
n5, t5 = note("修 A", F1, P1)
check("清零后重新从 1 数", n5 == 1 and not t5, "n=%r t=%r" % (n5, t5))

rst()
note("修 A", F1, P1)
note("修 A", F1, P1)
n6, t6 = note("修 A", ["backend/a.py", "backend/b.py", "backend/z.py"], P1)
check("第 3 轮换了文件 → 归零，不触发", n6 == 1 and not t6, "n=%r t=%r" % (n6, t6))

rst()
note("修 A", F1, P1)
note("修 A", F1, P1)
n7, t7 = note("修 A", F1, P1 + ["新出现的问题"])
check("第 3 轮多了一条问题 → 归零，不触发", n7 == 1 and not t7, "n=%r t=%r" % (n7, t7))

rst()
note("修 A", F1, P1)
note("修 B", F1, P1)
n8, t8 = note("修 C", F1, P1)
check("每轮换标题（各修各的）→ 不误伤", n8 == 1 and not t8, "n=%r t=%r" % (n8, t8))

rst()
for _ in range(3):
    note("全局终检", [], ["全局回归：2 个文件失败"])
n9, t9 = note("全局终检", [], ["全局回归：2 个文件失败"])
check("全局终检同构重复 → 也会被判原地打转", t9 and n9 == 4, "n=%r t=%r" % (n9, t9))

print("\n== 5. 状态位确实落在 _STATE 里（不是函数内局部变量）")
check("_STATE 里有 no_prog 且带 fp/n",
      isinstance(NS["_STATE"].get("no_prog"), dict)
      and "fp" in NS["_STATE"]["no_prog"] and "n" in NS["_STATE"]["no_prog"],
      repr(NS["_STATE"].get("no_prog")))

print("\n== 6. 阈值边界：正好 3 轮停，2 轮绝不停")
rst()
note("X", ["a.py"], ["boom"])
_n2, _t2 = note("X", ["a.py"], ["boom"])
check("2 轮相同 → 不停（防误伤）", _t2 is False, "t=%r" % _t2)
_n3, _t3 = note("X", ["a.py"], ["boom"])
check("3 轮相同 → 停", _t3 is True, "t=%r" % _t3)

print("\n---- 合计 %d 通过 / %d 失败 ----" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAIL: %s" % f)
    sys.exit(1)
sys.exit(0)
