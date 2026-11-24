# -*- coding: utf-8 -*-
"""RSI 统计窗口 + 写盘前语法校验 的离线回归 —— 不跑模型、不联网、不动真账本。

为什么要有（2026-11-24 实测）：
  1) 用户问「曲线好久没动了，是不是我看错了」。查真实数据：动作成功率取
     「最近 400 条」actlog，而这 400 条横跨 8 天，其中 11-16 一天就贡献了
     59 条真失败（当时在批量跑自动迭代）。旧事故永久留在分母里 → 成功率
     被钉在 0.725~0.733，8 天只涨 0.008，曲线自然是一条平线。
     修法：按时间取窗口（7 天）+ 样本不足回退，同时把旧口径写进 legacy_400。
  2) 最近 400 条里有 14 条「语法校验失败」。原来是**先写盘再校验、失败再回滚**，
     窗口期里磁盘上躺着一份坏 .py；进程若在这几毫秒崩掉，坏文件就留在源码里。
     修法：写盘前用 compile() 在内存里验一遍，不通过就不写。

这里验四类事：
  1) _py_syntax_error 能认出真语法错、不误伤好代码、不碰非 .py；
  2) _apply_find_replace 对「会改坏语法」的补丁**拒绝且文件不变**；
  3) 正常补丁照旧能落盘（别把功能改死）；
  4) _pick_window 按时间取、样本不足时回退，且 rsi_index 两个口径都在。

跑法：python backend/_feature_tests/test_rsi_window_and_syntax.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import self_iterate as SI             # noqa: E402
from backend import lessonloop as LL               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _iso(offset_sec=0):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() + offset_sec))


def test_syntax_helper():
    print("\n[1] 写盘前语法校验辅助函数")
    bad = SI._py_syntax_error("if x\n    pass\n", "t.py")
    check("缺冒号能认出来", bad is not None and "第 1 行" in str(bad), str(bad))
    bad2 = SI._py_syntax_error("x = (1,\n", "t.py")
    check("括号没闭合能认出来", bad2 is not None, str(bad2))
    check("好代码返回 None", SI._py_syntax_error("if x:\n    pass\n", "t.py") is None)
    check("中文注释不算错",
          SI._py_syntax_error('# 中文\nx = "你好"\n', "t.py") is None)
    check("非 .py 不管", SI._py_syntax_error("}}}{{{", "t.js") is None)
    check("缩进错能认出来",
          SI._py_syntax_error("def f():\npass\n", "t.py") is not None)


def test_apply_rejects_broken_patch():
    print("\n[2] 会改坏语法的补丁必须被拒绝，且文件不变")
    tmp = Path(tempfile.mkdtemp(prefix="rsi_syn_"))
    old_root = SI._ROOT
    try:
        SI._ROOT = tmp
        rel = "sample_mod.py"
        good = "# -*- coding: utf-8 -*-\ndef add(a, b):\n    return a + b\n"
        (tmp / rel).write_text(good, encoding="utf-8")
        # 把 return 那一行替换成「缺冒号的 if」→ 语法必坏
        ok, msg = SI._apply_find_replace(
            rel, "    return a + b", "    if a\n    return a + b")
        check("坏补丁被拒绝", ok is False, str(msg))
        check("拒绝原因说清是写盘前拦的",
              "写盘前拒绝" in str(msg) and "第" in str(msg), str(msg))
        check("文件一个字节都没动",
              (tmp / rel).read_text(encoding="utf-8") == good)
        # 正常补丁照旧能落盘
        ok2, msg2 = SI._apply_find_replace(
            rel, "    return a + b", "    return a + b + 0")
        check("好补丁照常落盘", ok2 is True, str(msg2))
        check("落盘内容正确",
              "return a + b + 0" in (tmp / rel).read_text(encoding="utf-8"))
        # 新建文件的路径也要校验
        ok3, msg3 = SI._apply_find_replace("new_bad.py", "", "def f(:\n")
        check("新建坏文件被拒绝", ok3 is False, str(msg3))
        check("新建坏文件没被创建", not (tmp / "new_bad.py").exists())
        ok4, _m4 = SI._apply_find_replace("new_ok.py", "", "x = 1\n")
        check("新建好文件照常", ok4 is True and (tmp / "new_ok.py").is_file())
    finally:
        SI._ROOT = old_root


def test_pick_window():
    print("\n[3] 统计窗口：按时间取、样本不足回退")
    now = time.time()
    old = [{"ts": _iso(-20 * 86400), "result": "fail", "detail": "很久以前的失败"}
           for _ in range(50)]
    fresh = [{"ts": _iso(-3600), "result": "ok", "detail": ""} for _ in range(50)]
    rows, why = LL._pick_window(old + fresh, 400)
    check("7 天内样本够 → 用时间窗口", why.startswith("最近7天"), why)
    check("只取到 7 天内的 50 条", len(rows) == 50, str(len(rows)))
    check("旧记录没被算进来",
          all(r.get("result") == "ok" for r in rows))
    # 7 天内只有 5 条 → 样本不足，回退
    rows2, why2 = LL._pick_window(old + fresh[:5], 400)
    check("样本不足时回退到最近 N 条", "样本不足回退" in why2, why2)
    check("回退后拿到的条数正确", len(rows2) == 55, str(len(rows2)))
    check("空账本不炸", LL._pick_window([], 400)[0] == [])


def test_rsi_reports_both():
    print("\n[4] rsi_index：两个口径都要报，不能只有新口径")
    r = LL.rsi_index(cache_sec=0)
    check("有 window 字段", bool(r.get("window")), str(r.get("window")))
    check("有 legacy_400 旧口径", isinstance(r.get("legacy_400"), dict),
          str(r.get("legacy_400")))
    lg = r.get("legacy_400") or {}
    check("旧口径带 ok/bad/rate",
          all(k in lg for k in ("ok", "bad", "rate")), str(lg))
    part = [p for p in (r.get("parts") or []) if p["name"] == "动作成功率"]
    check("动作成功率明细里标了口径",
          bool(part) and "口径" in str(part[0].get("detail")), str(part))
    check("分数在 0~100", 0 <= float(r.get("score") or 0) <= 100,
          str(r.get("score")))


def test_fail_kind():
    print("\n[5] 失败归因：真实原文必须归到具体原因，不能全掉进「未分类」")
    cases = [
        ("模型没有改动任何文件（它可能只是说了说）（已自动回滚：无改动可回滚）",
         "无实际改动"),
        ("LLM 评审拒绝（针对真实 diff）：需求与代码完全对不上：要求改 apply_patch "
         "加锚点降级，但 diff 改的是别的文件", "评审拒绝（diff 与需求不符）"),
        ("【回归】新增失败 1 条（改动前这些是过的，说明你改坏了）：· audit.全量自检无 high",
         "行为回归失败"),
        ("【功能验收】断言没过：AttributeError: module 'backend.hive' has no "
         "attribute 'list_cards'", "功能验收失败"),
        ("backend/reply_gate.py 语法校验失败：line 118", "语法校验失败"),
        ("backend/app.js：find 在文件里找不到（可能已被改过）", "锚点没命中"),
        ("反复修改熔断：frontend/app.js 在最近 60 分钟内已被自动改成功 10 次",
         "护栏拦截"),
    ]
    for text, want in cases:
        got = LL._fail_kind(text)
        check("归因→%s" % want, got == want, "实际得到 %s" % got)
    # 归因条数必须和「动作成功率」的分母对得上，否则同一屏两个数字打架
    fb = LL.failure_breakdown()
    r = LL.rsi_index(cache_sec=0)
    part = [p for p in (r.get("parts") or []) if p["name"] == "动作成功率"]
    if part:
        det = str(part[0].get("detail") or "")
        try:
            left, right = det.split("｜")[0].split("/")
            ok_n = int(left)
            tot_n = int(right.split("（")[0])
            check("归因条数 = 动作成功率的真失败数",
                  fb.get("n") == tot_n - ok_n,
                  "归因 %s vs 分母 %s" % (fb.get("n"), tot_n - ok_n))
        except Exception as e:                          # noqa: BLE001
            check("能解析动作成功率明细", False, "%s / %s" % (det, e))


def main():
    print("=" * 62)
    print("RSI 统计窗口 + 写盘前语法校验 回归")
    print("=" * 62)
    test_syntax_helper()
    test_apply_rejects_broken_patch()
    test_pick_window()
    test_rsi_reports_both()
    test_fail_kind()
    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ! %s" % f)
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
