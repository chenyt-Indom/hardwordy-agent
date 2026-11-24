# -*- coding: utf-8 -*-
"""回归：2026-11-24「全面检验」查出的三处真问题。

真实故障史（都是实测复现出来的，不是推测）：

  ① 失败归因表**误报 degrade**
     `iterhealth._c_failattr` 的堆栈覆盖率分母是**全部**失败记录，
     但 score_regress / loop_regress 是「评分退步」——它们不是异常，
     天然没有 traceback。实测 31 条里 18 条 score_regress + 1 条 loop_regress，
     把覆盖率从真实的 11/12（92%）压到 11/31（35%），于是
     「失败归因表」长期挂 DEGRADE，用户看到「1 项需要关注」却无毛病可修。
     修法：分母只算异常/解析类（_NO_TB 之外的 kind）。

  ② apply_patch 的**降级路径必然失败**
     锚点没命中时会降级成「按行号定位 → 整段替换 → 整份重写」，
     但模型给的 replace 常常不带缩进，而锚点段在函数体内带 4 空格缩进 ——
     整段替换后缩进丢了，写盘前语法校验判
     「expected an indented block」→ 定位成功却写不进去，兜底等于白做。
     实测复现：find 少缩进 + replace 少缩进 → ok=False。
     修法：replace 所有非空行都无前导空白时，统一补上锚点首行的缩进。

  ③ 前端连着**不存在的 SSE 路由**
     `frontend/app.js` 里 `new EventSource("/api/featradar/stream")`，
     后端从没有这个路由 —— 实测 GET 返回 405（被 /api/featradar/{iid}
     的动态路由吞掉），featpanel.js 里也没有任何 dispatchEvent("featradar")。
     每次打开页面白打一次 405、连上即 error 关闭，纯死代码。

本测试守三条（改回去就会红）：
  1. 只有评分退步类失败时，失败归因表**不许**判 DEGRADE
  2. 异常类失败里堆栈覆盖不足一半时，**必须**判 DEGRADE（不能为了不误报而放过真问题）
  3. apply_patch 降级后：写盘成功 + 缩进正确 + 代码可编译；且不重复加缩进
  4. app.js 里不再出现 /api/featradar/stream
"""
import os
import sys
import tempfile
import shutil
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import iterhealth                          # noqa: E402

_OK = []
_BAD = []


def check(name, cond, extra=""):
    (_OK if cond else _BAD).append(name)
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  " + str(extra)) if (extra and not cond) else ""))


def _row(kind, tb=False):
    """造一条失败归因记录（只带 _c_failattr 会读的字段）。"""
    return {
        "kind": kind,
        "traceback": ("Traceback (most recent call last): ..." if tb else ""),
        "context": "上下文",
        "why_rollback": "回滚原因",
        "lesson": "教训",
    }


def _probe(rows):
    """用给定 rows 跑一次 _c_failattr，返回它的判定结果。"""
    from backend import failattr
    with mock.patch.object(failattr, "load", return_value=rows), \
            mock.patch.object(failattr, "stats",
                              return_value={"total": len(rows), "repeat": []}):
        return iterhealth._c_failattr()


def test_failattr_score_regress_not_degrade():
    """① 只有评分退步（天然无堆栈）时，不许判 DEGRADE。"""
    rows = [_row("score_regress") for _ in range(18)] + [_row("model_error", tb=True)]
    it = _probe(rows)
    check("评分退步类不压堆栈覆盖率", it.get("state") == iterhealth.OK, it)

    # 全是评分退步、一条异常都没有：分母为 0，不该因此判 degrade
    it2 = _probe([_row("score_regress") for _ in range(5)])
    check("全是评分退步也不判 degrade", it2.get("state") == iterhealth.OK, it2)

    # detail 要能看出分母只算了异常类（否则用户看不懂为什么 11/31 变成 11/12）
    check("detail 标明异常类分母", "异常类" in str(it.get("value")), it.get("value"))


def test_failattr_real_problem_still_flagged():
    """② 异常类里堆栈覆盖不足一半 → 必须判 DEGRADE（别为了不误报放过真问题）。"""
    rows = [_row("model_error", tb=True)] + [_row("model_error") for _ in range(3)]
    it = _probe(rows)
    check("异常类堆栈不足一半要报", it.get("state") == iterhealth.DEGRADE, it)

    # 异常类全覆盖 → 正常
    rows2 = [_row("model_error", tb=True) for _ in range(4)]
    it2 = _probe(rows2)
    check("异常类全覆盖判正常", it2.get("state") == iterhealth.OK, it2)


def test_patch_fallback_keeps_indent():
    """③ apply_patch 降级后：写盘成功、缩进正确、可编译。"""
    from backend import tools, selfdev as _sd

    tmpd = tempfile.mkdtemp(prefix="patchfb_")
    orig = _sd.project_root
    _sd.project_root = lambda: tmpd
    os.makedirs(os.path.join(tmpd, "backend"), exist_ok=True)
    p = os.path.join(tmpd, "backend", "demo.py")
    body = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    try:
        # find 少了缩进、replace 也少了缩进（模型最常见的写法）
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        r = tools.apply_patch("backend/demo.py", "a = 1\nb = 2", "a = 10\nb = 20")
        check("降级写盘成功", r.get("ok") is True, r)
        check("降级走的是 rewrite", r.get("fallback") == "rewrite", r)
        with open(p, encoding="utf-8") as f:
            out = f.read()
        check("缩进被补回（没丢函数体缩进）",
              "\n    a = 10\n    b = 20\n" in out, repr(out))
        try:
            compile(out, "demo.py", "exec")
            ok_compile = True
        except SyntaxError as e:
            ok_compile = False
            out += "  <- %s" % e
        check("降级结果可编译", ok_compile, repr(out))

        # replace 自带缩进 → 不许二次加缩进
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        r2 = tools.apply_patch("backend/demo.py", "a = 1\nb = 2",
                               "    a = 10\n    b = 20")
        with open(p, encoding="utf-8") as f:
            out2 = f.read()
        check("已带缩进的 replace 不被重复缩进",
              r2.get("ok") is True and "\n        a = 10" not in out2, repr(out2))

        # 顶层（无缩进）锚点不受影响
        with open(p, "w", encoding="utf-8") as f:
            f.write("X = 1\nY = 2\n")
        r3 = tools.apply_patch("backend/demo.py", "X = 1", "X = 9")
        with open(p, encoding="utf-8") as f:
            out3 = f.read()
        check("顶层锚点不被加缩进", r3.get("ok") is True and out3.startswith("X = 9"), repr(out3))

        # 完全对不上的锚点仍然拒绝（不能为了「能写进去」瞎改）
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        r4 = tools.apply_patch("backend/demo.py", "zzz = 99", "zzz = 100")
        with open(p, encoding="utf-8") as f:
            out4 = f.read()
        check("完全对不上仍拒绝且文件不变",
              r4.get("ok") is False and out4 == body, r4)
    finally:
        _sd.project_root = orig
        shutil.rmtree(tmpd, ignore_errors=True)


def test_no_dead_sse():
    """④ 前端不再连不存在的 /api/featradar/stream。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    js = os.path.join(root, "frontend", "app.js")
    with open(js, encoding="utf-8", errors="replace") as f:
        s = f.read()
    # ⚠️ 只查**非注释行**：修复时在注释里写了这条历史，
    #    直接把整份文本搜字符串会把自己的说明判成死连接（踩过）。
    live = []
    for ln in s.splitlines():
        t = ln.strip()
        if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
            continue
        live.append(ln)
    code = "\n".join(live)
    check("app.js 代码里不再连 featradar/stream",
          "/api/featradar/stream" not in code)
    # 后端确实没有这个路由（有的话前端就该保留连接，说明这条测试该改）
    main_py = os.path.join(root, "backend", "main.py")
    with open(main_py, encoding="utf-8", errors="replace") as f:
        m = f.read()
    check("后端确实没有 featradar/stream 路由",
          '"/api/featradar/stream"' not in m)


def main():
    for fn in (test_failattr_score_regress_not_degrade,
               test_failattr_real_problem_still_flagged,
               test_patch_fallback_keeps_indent,
               test_no_dead_sse):
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            import traceback
            traceback.print_exc()
            _BAD.append("%s 抛异常: %s" % (fn.__name__, e))

    print("\n通过 %d / 失败 %d" % (len(_OK), len(_BAD)))
    if _BAD:
        print("失败项：" + "；".join(_BAD))
    return 0 if not _BAD else 1


if __name__ == "__main__":
    sys.exit(main())
