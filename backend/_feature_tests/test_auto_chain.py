# -*- coding: utf-8 -*-
"""回归：自动迭代必须「一个接一个跑到底」，队列清空 + 全局终检通过才算收工。

用户 2026-11-24 要求（原话）：
  「自动迭代这些任务如果有多个任务能不能自动连贯下去并且做到反复验证修改
    再验证，遇到失败反复重试，一直到所有任务完成且一切正常且队列清空为止？」

用户选定的四条口径（问答里选的）：
  ① 一轮不再限 10 个 —— 队列不空就一直跑；
  ② 单个功能连续失败的上限提高到 5 轮，仍不过就**跳过、继续跑下一个**；
  ③ 队列清空后跑一次**全局终检**（语法 + 自检 + 全量回归）再收工；
  ④ 队列清空就真收工，不自动补新任务。

本测试锁住这几条，防止以后被悄悄改回去（比如又冒出一个「做满 N 个就停」）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass


def run() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    from backend import iter_runner as IR

    # ① 连跑上限：0 = 不限制
    ck("一轮不再限制功能数（_MAX_AUTO_ROUNDS=0）",
       int(IR._MAX_AUTO_ROUNDS) == 0, "实际=%r" % (IR._MAX_AUTO_ROUNDS,))
    ck("_budget_full 在 0/1/999 都不算满",
       not any([IR._budget_full(0), IR._budget_full(1),
                IR._budget_full(999)]))
    ck("队列有活时状态不再是 paused（会继续跑）",
       IR._state_name(None, [{"tid": "a"}, {"tid": "b"}]) == "queue_only",
       IR._state_name(None, [{"tid": "a"}]))

    # ② 失败重试上限 5，且超限后是「跳过继续」而不是卡住队列
    ck("单功能失败链上限 = 5", int(IR._MAX_FAIL_CHAIN) == 5,
       "实际=%r" % (IR._MAX_FAIL_CHAIN,))
    import inspect
    _src = inspect.getsource(IR)
    _seg = ""
    # 锚点用「验证链」那条原文（_final_check 里也有一句「已停止自动修复」，
    # 拿那句会找到收工分支，测不到这里）
    _i = _src.find("连续 %d 次修复后验证仍未通过")
    if _i > 0:
        _seg = _src[_i:_i + 900]
    ck("超限后仍然接着跑下一个（不是卡住整条队列）",
       "_next()" in _seg, "找到 %d 字上下文" % len(_seg))

    # ③ 队列长度不再限 10 个
    ck("队列不再限长（_MAX_QUEUE=0）", int(IR._MAX_QUEUE) == 0,
       "实际=%r" % (IR._MAX_QUEUE,))
    ck("提交时不再因为队列满而拒收",
       "if run and _MAX_QUEUE and len(_STATE[\"queue\"]) >= _MAX_QUEUE" in _src)

    # ④ 队列清空后的全局终检
    ck("有 _final_check（收工前最后一道闸）", hasattr(IR, "_final_check"))
    _fc = inspect.getsource(IR._final_check)
    ck("终检跑语法", "_syntax_ok" in _fc)
    ck("终检跑静态自检", "code_audit" in _fc)
    ck("终检跑**全量**回归（不是 quick）", 'scope="full"' in _fc)
    ck("终检跑不成也算没过（不许静默放行）",
       "没跑成" in _fc and "problems.append" in _fc)
    ck("队列清空后真的调了终检",
       "队列已空，开始全局终检" in _src and "_final_check(tid)" in _src)
    ck("终检没过不许宣布收工（会生成修复任务）",
       "全局终检没过" in _src and "修全局终检没过的问题" in _src)
    ck("终检期间来了新任务就先跑任务（不误报收工）",
       "_qn2" in _src)

    # ⑤ 前端：本轮完成数显示「不限」，不显示成 "?" 或 "0"
    _root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    _ij = os.path.join(_root, "frontend", "iterjournal.js")
    try:
        with open(_ij, encoding="utf-8") as fh:
            _js = fh.read()
    except Exception:                                   # noqa: BLE001
        _js = ""
    ck("前端 iterjournal.js 可读", bool(_js))
    ck("前端把 0 显示成「不限」", 'd.max_rounds ? d.max_rounds : "不限"' in _js)

    print("")
    if fails:
        print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("全部通过（失败 0 项）")
    return 0


if __name__ == "__main__":
    sys.exit(run())
