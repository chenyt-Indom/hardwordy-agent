# -*- coding: utf-8 -*-
"""无进展检测（原地打转）· 离线回归。

对应 2026-11-24 用户拍板的方案：
  「更值得做的是无进展检测：连续 N 轮调同一个工具、同样参数、同样报错，
    就提前收手并说明原因 —— 这比一刀切降上限精准得多。」

要守住的两条边界（这两条任何一条破了都会变成真故障）：
  A. **必须真能停**：连续 4 轮工具/参数/返回分毫不差 → 判定空转，收手并说明。
  B. **绝不能误伤**：只要有任何一处变化（参数变了 / 返回变了 / 换了工具），
     计数就归零 —— 正常的「查A→改B→再查A」、轮询等文件更新，都不该被打断。

为什么不 import backend.main：那个模块会把整个后端拉起来（配置、客户端、
定时任务注册），跑个回归测试不该有这些副作用。这里用 ast 把**同一份源码里**
的几个纯函数抠出来执行，测的就是真正在跑的那份逻辑。

跑法：python backend/_feature_tests/test_no_progress.py
"""
import ast
import json
import os
import sys

_THIS = os.path.abspath(__file__)
_HERE = os.path.dirname(_THIS)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MAIN = os.path.join(_ROOT, "backend", "main.py")

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
    """从 backend/main.py 抠出 _noprog_* 纯函数和 NO_PROGRESS_LIMIT。"""
    src = src_text if src_text is not None else open(
        _MAIN, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"json": json}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_noprog_"):
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         _MAIN, "exec"), ns)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "NO_PROGRESS_LIMIT" in names:
                exec(compile(ast.Module(body=[node], type_ignores=[]),
                             _MAIN, "exec"), ns)
    return src, ns, found


def _g(*items):
    """构造 gathered：items 是 (idx, name, result)。events 用空列表。"""
    return [(i, n, r, []) for i, n, r in items]


def _step(ns, gathered, calls, prev, streak):
    sig = ns["_noprog_signature"](gathered, calls)
    streak, prev = ns["_noprog_next"](sig, prev, streak)
    return streak, prev


def main():
    print("=" * 62)
    print("无进展检测 · 能停住空转 / 不误伤正常任务")
    print("=" * 62)

    src, ns, found = _load()
    for fn in ("_noprog_signature", "_noprog_next", "_noprog_tools"):
        check("源码里找得到 %s" % fn, fn in found)

    # ---- 阈值 ----
    limit = ns.get("NO_PROGRESS_LIMIT")
    check("NO_PROGRESS_LIMIT 是模块级常量", isinstance(limit, int),
          "limit=%r" % (limit,))
    check("阈值 >= 3（太小会误伤正常任务）",
          isinstance(limit, int) and limit >= 3, "limit=%r" % (limit,))

    # ---- A. 真能停：连续相同 ----
    run_g = _g((0, "workspace_run", "SyntaxError: invalid syntax"))
    run_c = [("workspace_run", {"rel": "app.py"})]
    prev, streak = None, 0
    seen = []
    for _ in range(6):
        streak, prev = _step(ns, run_g, run_c, prev, streak)
        seen.append(streak)
    check("连续相同：计数逐轮递增", seen[:4] == [1, 2, 3, 4], "seen=%r" % (seen,))
    check("连续相同：第 %d 轮达到阈值" % limit,
          seen[limit - 1] >= limit, "seen=%r limit=%r" % (seen, limit))
    check("连续相同：之后一直停在阈值以上（不会回落）",
          min(seen[limit - 1:]) >= limit, "seen=%r" % (seen,))

    # ---- B1. 参数变了 → 归零 ----
    prev, streak = None, 0
    for i in range(limit - 1):
        streak, prev = _step(ns, run_g, run_c, prev, streak)
    streak, prev = _step(ns, _g((0, "workspace_run", "SyntaxError: invalid syntax")),
                         [("workspace_run", {"rel": "other.py"})], prev, streak)
    check("参数变了 → 计数归零重来", streak == 1, "streak=%r" % (streak,))

    # ---- B2. 返回变了（轮询等到更新）→ 归零 ----
    prev, streak = None, 0
    for i in range(limit - 1):
        streak, prev = _step(ns, run_g, run_c, prev, streak)
    streak, prev = _step(ns, _g((0, "workspace_run", "all tests passed")),
                         run_c, prev, streak)
    check("返回变了 → 计数归零（轮询类任务不被打断）",
          streak == 1, "streak=%r" % (streak,))

    # ---- B3. 换了工具 → 归零 ----
    prev, streak = None, 0
    for i in range(limit - 1):
        streak, prev = _step(ns, run_g, run_c, prev, streak)
    streak, prev = _step(ns, _g((0, "workspace_list", "app.py\nreadme.md")),
                         [("workspace_list", {})], prev, streak)
    check("换了工具 → 计数归零", streak == 1, "streak=%r" % (streak,))

    # ---- B4. 真实形态：查A→改B→再查A 永远不该触发 ----
    prev, streak = None, 0
    hit = 0
    seq = [
        (_g((0, "workspace_read", "print(1)")), [("workspace_read", {"rel": "a.py"})]),
        (_g((0, "workspace_write", "已写入 a.py")), [("workspace_write", {"rel": "a.py", "text": "print(2)"})]),
        (_g((0, "workspace_run", "2")), [("workspace_run", {"rel": "a.py"})]),
        (_g((0, "workspace_read", "print(1)")), [("workspace_read", {"rel": "a.py"})]),
        (_g((0, "workspace_write", "已写入 a.py")), [("workspace_write", {"rel": "a.py", "text": "print(2)"})]),
        (_g((0, "workspace_run", "2")), [("workspace_run", {"rel": "a.py"})]),
    ]
    for g, c in seq:
        streak, prev = _step(ns, g, c, prev, streak)
        if streak >= limit:
            hit += 1
    check("正常循环（查/改/跑 交替）全程不触发", hit == 0,
          "hit=%d streak=%r" % (hit, streak))

    # ---- 指纹稳定性 ----
    a = ns["_noprog_signature"](
        _g((0, "search", "r1"), (1, "time", "r2")),
        [("search", {"q": "x", "n": 2}), ("time", {})])
    b = ns["_noprog_signature"](
        _g((0, "search", "r1"), (1, "time", "r2")),
        [("search", {"n": 2, "q": "x"}), ("time", {})])
    check("参数键顺序不同 → 指纹相同（不是靠字面量比）", a == b)

    c1 = ns["_noprog_signature"](
        _g((0, "search", "r1"), (1, "time", "r2")),
        [("search", {"q": "x"}), ("time", {})])
    c2 = ns["_noprog_signature"](
        _g((1, "time", "r2"), (0, "search", "r1")),      # 返回顺序颠倒
        [("search", {"q": "x"}), ("time", {})])
    check("gathered 顺序颠倒 → 指纹相同（按 idx 归位）", c1 == c2)

    d1 = ns["_noprog_signature"](_g((0, "a", "r")), [("a", {})])
    d2 = ns["_noprog_signature"](_g((0, "a", "r"), (1, "b", "r")),
                                 [("a", {}), ("b", {})])
    check("一轮少一个工具 → 指纹不同", d1 != d2)

    # ---- 空轮：没有工具调用时不该被判定为"空转" ----
    e_sig = ns["_noprog_signature"]([], [])
    check("空轮指纹是空串", e_sig == "")
    streak, prev = 0, None
    for _ in range(6):
        streak, prev = _step(ns, [], [], prev, streak)
    check("空轮不会累加到阈值（没工具时由别处收尾）",
          streak == 1, "streak=%r" % (streak,))

    # ---- 长返回：长度不同必须能区分（否则截断后会把"有新内容"看漏） ----
    l1 = ns["_noprog_signature"](_g((0, "web_read", "x" * 4000)), [("web_read", {})])
    l2 = ns["_noprog_signature"](_g((0, "web_read", "x" * 5000)), [("web_read", {})])
    check("长返回长度不同 → 指纹不同", l1 != l2)

    # ---- 接线：主循环必须真的在用这几个函数 ----
    check("主循环调用了 _noprog_signature(gathered, calls)",
          "_noprog_signature(gathered, calls)" in src)
    check("主循环调用了 _noprog_next",
          "_noprog_next(" in src and "streak, _noprog_sig = _noprog_next" in src)
    check("判定用的是模块级 NO_PROGRESS_LIMIT",
          "_noprog_streak >= NO_PROGRESS_LIMIT" in src)

    i_if = src.find("_noprog_streak >= NO_PROGRESS_LIMIT")
    i_break = src.find("break", i_if) if i_if >= 0 else -1
    check("判定为真之后确实 break（真的收手）",
          0 <= i_if < i_break <= i_if + 900,
          "i_if=%r i_break=%r" % (i_if, i_break))
    check("收手时会向用户说明原因",
          i_if >= 0 and "连续 %d 轮在做" in src[i_if:i_if + 900])

    # 判据必须在"工具结果已追加进 working"之后，才拿得到真实返回
    i_append = src.find('working.append({"role": "tool"')
    i_detect = src.find("# 2.4) 【无进展检测】")
    check("检测块位于工具结果追加之后（拿的是真实返回）",
          0 <= i_append < i_detect,
          "i_append=%r i_detect=%r" % (i_append, i_detect))

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
