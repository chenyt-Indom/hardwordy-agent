"""行为级回归测试 —— 补上静态检查永远看不到的那一类 bug。

为什么需要它
------------
code_audit 只能看「名字 / 结构」：调了不存在的函数、未定义变量、重复定义……
但真正让程序出错的，往往是**语法完全合法、名字全都存在、行为却错**的代码：

    def sum_all(xs):
        s = 0
        for i in range(len(xs) - 1):     # off-by-one，最后一个元素永远加不上
            s += xs[i]
        return s

    def grade(score):
        if score < 60:                   # 条件写反
            return "及格"

这类 bug 静态检查**一个都查不出来**（实测：6 类逻辑 bug，自检报 0 条）。
唯一可靠的检验方式是**真跑一遍、断言外部可见的行为**。所以这个模块把关键
流程的行为固化成断言，改动前后各跑一遍 —— 一旦改坏立刻能看出来。

判定口径
--------
    每条用例只断言**外部可见的行为**（返回值、长度、内容、几何位置），
    不碰内部实现细节 —— 否则一重构就全是误报，那还不如没有。
    不联网、不调 LLM、不加载模型：必须秒级、可重复。

用法
----
    from . import regress
    r = regress.run()                 # quick：约 1~3 秒
    r = regress.run(scope="full")     # 追加全量自检 + 全模块导入（约 10 秒）
    r["ok"]                           # False 表示有回归
    r["failed_items"]                 # [{'name':..., 'why':...}]

谁在用
------
    self_iterate.execute_approved()  每次自动改完代码 → 跑 quick →
                                     失败立刻回滚（这是「改坏自动撤销」的兜底）
"""
import ast
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND = Path(__file__).resolve().parent
_ROOT = _BACKEND.parent

_CASES = []


def case(name, scope="quick"):
    """注册一条用例。scope=quick 每次改完都跑；full 只在手动/定时全检时跑。"""
    def deco(fn):
        _CASES.append({"name": name, "scope": scope, "fn": fn})
        return fn
    return deco


# ---------------------------------------------------------------- 回复裁剪
def _clip_cases():
    from . import main as M
    return M


@case("clip.短文本原样返回")
def _c_clip_short():
    M = _clip_cases()
    s = "结论：已完成。"
    got = M._clip_reply(s, limit=6000)
    assert got == s, "短文本被改动了：%r" % got[:80]


@case("clip.超限时保留结论/改动/验证")
def _c_clip_keep():
    M = _clip_cases()
    body = "\n".join("这是第 %d 行推理过程，用来把回复撑长。" % i for i in range(400))
    s = "结论：已修好。\n改动：backend/x.py。\n验证：跑了一遍。\n" + body
    got = M._clip_reply(s, limit=2000)
    assert len(got) <= 2000, "裁剪后仍然超限：%d > 2000" % len(got)
    for h in ("结论：", "改动：", "验证："):
        assert h in got, "裁剪把「%s」这段丢了（用户最需要看的就是它）" % h


@case("clip.不在半句处硬切")
def _c_clip_cut():
    """切点必须落在句子中间，才能验出「有没有回退到句末」。

    ⚠️ 这条用例本身踩过坑：最早用「推理内容很长很长。」（9 字）重复，
    800 字的切点恰好落在句号上，于是「直接按长度硬切」的错误实现也能通过。
    现在换成 10 字一句、前置 8 字标题，切点必然落在句子中间。
    """
    M = _clip_cases()
    s = "结论：已完成。\n" + ("推理内容稍微长一点。" * 600)
    got = M._clip_reply(s, limit=800)
    assert got, "裁剪后成了空串"
    # 去掉实现自己追加的提示语，再看正文最后是不是一个完整句子
    body = got.split("\n…（回复过长")[0].split("…（已截断）")[0].rstrip()
    tail = body[-1:]
    assert tail in "。！？；.!?;…）)》", \
        "裁剪落在半句处（正文结尾是 %r），用户会看到断掉的话：…%s" % (tail, body[-30:])


# ---------------------------------------------------------------- 回复模板
@case("template.带闲聊豁免条款")
def _c_template():
    M = _clip_cases()
    t = M._reply_template_ctx()
    assert "寒暄" in t or "闲聊" in t, "模板里没有「闲聊不套模板」的豁免说明"
    assert "禁止出现" in t, "模板没有明确禁止在闲聊轮次出现三段式"
    assert "结论：" in t, "模板丢了「结论前置」这条核心要求"


# ---------------------------------------------------------------- 路径白名单
@case("path.白名单拦得住越界")
def _c_path():
    from . import self_iterate as SI
    ok = ["backend/tools.py", "frontend/app.js"]
    bad = ["data/secrets.json", "models/x.bin", "sessions/a.json",
           "../../etc/passwd", "backend/__pycache__/x.pyc",
           "C:/Windows/system32/x.py", "backend/x.exe"]
    for rel in ok:
        assert SI._norm_rel(rel), "该放行的路径被拦了：%s" % rel
    for rel in bad:
        assert not SI._norm_rel(rel), "该拦的路径被放行了：%s" % rel


# ---------------------------------------------------------------- 黑窗防护
@case("nowin.子进程自动继承补丁")
def _c_nowin_child():
    """孙子进程也必须装补丁 —— 否则它起的进程会弹黑窗。

    2026-11-15 现场证据（data/procwin_watch.jsonl）：
        PseudoConsoleWindow | cmd.exe | 父链 pythonw(子进程) ← pythonw(后端)
    实测：PYTHONPATH 不带 _pysite → 起 3 次 cmd 出 5 个控制台窗口；
          带上 → 0 个。这条用例锁住「机制」，防止以后又退回去。
    """
    import os
    import subprocess

    pysite = str(_BACKEND / "_pysite")
    assert os.path.isdir(pysite), "backend/_pysite 不见了，子进程不会再有防护"

    env = dict(os.environ)
    cur = env.get("PYTHONPATH", "") or ""
    env["PYTHONPATH"] = pysite + (os.pathsep + cur if cur else "")
    code = ("import subprocess,json;"
            "print(json.dumps({'p':bool(getattr(subprocess.Popen,'_nowin_patched',False))}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       timeout=60, env=env, creationflags=0x08000000)
    txt = (r.stdout or b"").decode("utf-8", "replace").strip()
    assert '"p": true' in txt.lower(), "子进程没装上补丁（输出：%r）" % txt[:200]


@case("nowin.看门狗只动自己的窗口")
def _c_nowin_scope():
    """看门狗不能隐藏用户自己的终端窗口（2026-11-15 实际发生过）。"""
    import os

    from . import procwin

    assert procwin._is_descendant_of_me(os.getpid()), "连自己都判不出来"
    for foreign in (1, 4):          # 系统进程，一定不是我们的后代
        assert not procwin._is_descendant_of_me(foreign), \
            "把系统进程 %s 当成自己的后代了 → 会误隐藏用户窗口" % foreign


# ---------------------------------------------------------------- 逻辑型检查器
def _trees_of(src):
    return {"backend/_probe.py": (None, ast.parse(src))}


@case("audit.logic.可变默认参数")
def _c_l1():
    from . import code_audit as CA
    out = CA._check_mutable_default(_trees_of(
        "def f(a, b=[]):\n    b.append(a)\n    return b\n"))
    assert len(out) == 1, "没抓到可变默认参数（返回 %d 条）" % len(out)


@case("audit.logic.is 比字面量")
def _c_l2():
    from . import code_audit as CA
    out = CA._check_is_literal(_trees_of("def f(x):\n    return x is 'abc'\n"))
    assert len(out) == 1, "没抓到 is 比字面量（返回 %d 条）" % len(out)
    # is None / is True 是正确写法，绝不能误报
    out2 = CA._check_is_literal(_trees_of("def f(x):\n    return x is None or x is True\n"))
    assert not out2, "把 `is None` / `is True` 误报了：%s" % out2


@case("audit.logic.assert 元组恒真")
def _c_l3():
    from . import code_audit as CA
    out = CA._check_assert_tuple(_trees_of("def f(x):\n    assert (x, '必须非空')\n"))
    assert len(out) == 1, "没抓到 assert 元组（返回 %d 条）" % len(out)


@case("audit.logic.不可达代码")
def _c_l4():
    from . import code_audit as CA
    out = CA._check_unreachable(_trees_of(
        "def f():\n    return 1\n    return 2\n"))
    assert len(out) == 1, "没抓到 return 之后的死代码（返回 %d 条）" % len(out)
    # 正常分支不应误报
    out2 = CA._check_unreachable(_trees_of(
        "def f(x):\n    if x:\n        return 1\n    return 2\n"))
    assert not out2, "把正常分支误报了：%s" % out2


@case("audit.logic.finally 里 return")
def _c_l5():
    from . import code_audit as CA
    out = CA._check_return_in_finally(_trees_of(
        "def f():\n    try:\n        pass\n    finally:\n        return 1\n"))
    assert len(out) == 1, "没抓到 finally 里的 return（返回 %d 条）" % len(out)


@case("audit.logic.干净代码零误报")
def _c_l6():
    from . import code_audit as CA
    src = (
        "def add(a, b=None):\n"
        "    if b is None:\n"
        "        b = []\n"
        "    assert a is not None, 'a 不能为空'\n"
        "    if a > 0:\n"
        "        return a\n"
        "    return 0\n")
    t = _trees_of(src)
    for fn in (CA._check_mutable_default, CA._check_is_literal,
               CA._check_assert_tuple, CA._check_unreachable,
               CA._check_return_in_finally, CA._check_while_true,
               CA._check_self_compare, CA._check_range_len_minus,
               CA._check_index_shift):
        out = fn(t)
        assert not out, "%s 对干净代码误报了 %d 条：%s" % (
            fn.__name__, len(out), out[0].get("msg") if out else "")


@case("audit.logic.while True 无出口")
def _c_l7():
    from . import code_audit as CA
    # 纯计算死循环（体内没有任何调用）→ mid：必卡死，可以自动修
    out = CA._check_while_true(_trees_of(
        "def f():\n    n = 0\n    while True:\n        n += 1\n"))
    assert len(out) == 1 and out[0]["level"] == "mid", \
        "纯计算死循环没被判 mid：%s" % out
    # 队列 worker（`q.get()` 阻塞等待，跑在 daemon 线程里）是标准写法 → 不许报。
    # ⚠️ 2026-11-15：检查器加了这条豁免，回归断言必须同步，否则「改检查器 →
    #    回归挂 → 所有自动修复被回滚」会死锁（实测踩到 1/22 失败）。
    out2 = CA._check_while_true(_trees_of(
        "def f(q):\n    while True:\n        job = q.get()\n"))
    assert not out2, "队列 worker 循环被误报了：%s" % out2
    # 轮询循环（体内有 sleep）也是标准写法 → 不许报
    out2c = CA._check_while_true(_trees_of(
        "def f():\n    import time\n    while True:\n        time.sleep(1)\n"))
    assert not out2c, "轮询循环（sleep）被误报了：%s" % out2c
    # 被 try/except 包着（靠异常退出）→ 不许报
    out2d = CA._check_while_true(_trees_of(
        "def f(ws):\n    try:\n        while True:\n            ws.recv()\n"
        "    except Exception:\n        pass\n"))
    assert not out2d, "靠异常退出的循环被误报了：%s" % out2d
    # 其它带调用但确实无出口的 → 只报 low，不许自动改
    out2b = CA._check_while_true(_trees_of(
        "def f():\n    while True:\n        do_work()\n"))
    assert out2b and out2b[0]["level"] == "low", \
        "带调用的无出口循环没被判 low（信任模式会把它改坏）：%s" % out2b
    # 有 break 的正常循环不许报
    out3 = CA._check_while_true(_trees_of(
        "def f(q):\n    while True:\n        if not q:\n            break\n"))
    assert not out3, "有 break 的循环被误报了：%s" % out3


@case("audit.logic.自比较（NaN 惯用法除外）")
def _c_l8():
    from . import code_audit as CA
    out = CA._check_self_compare(_trees_of(
        "def f(o):\n    return o.x == o.x\n"))
    assert len(out) == 1, "没抓到属性自比较（返回 %d 条）" % len(out)
    # `v != v` 是标准 NaN 判断，真实代码里 3 处都是这个用途，绝不能报
    out2 = CA._check_self_compare(_trees_of(
        "def f(v):\n    return v != v\n"))
    assert not out2, "把 NaN 判断 `v != v` 误报了：%s" % out2


@case("audit.logic.range(len-1) 少遍历一个")
def _c_l9():
    from . import code_audit as CA
    out = CA._check_range_len_minus(_trees_of(
        "def f(xs):\n    s = 0\n"
        "    for i in range(len(xs) - 1):\n        s += xs[i]\n    return s\n"))
    # ⚠️ 2026-11-15：这条降为 info（实测真实代码 13 条全是正确写法），
    #    只作参考提示，不进 low 统计、不会变成可执行动作。
    assert len(out) == 1 and out[0]["level"] == "info", \
        "range(len(x)-1) 的级别/条数不对：%s" % out


@case("audit.logic.滑窗不报、真 off-by-one 仍报")
def _c_l9b():
    """降噪不能降成「瞎了」：2-gram 滑窗放过，真漏遍历必须还报。

    ⚠️ 必须用**真实文件**测：`_near_slide` 要读源码上下文，
    用 `_trees_of` 那种假路径读不到文件，会一律当成「没有滑窗特征」→ 测不出降噪。
    """
    import shutil
    import tempfile

    from . import code_audit as CA
    d = tempfile.mkdtemp(prefix="slide_")
    # ⚠️ 必须是 Path 对象：code_audit._read 调 p.read_text()，
    #    传字符串会 AttributeError，而 _near_slide 的 except 会把它当成「没有滑窗特征」，
    #    于是降噪**静默失效**、用例误报失败（踩过一次）。
    p = Path(d) / "_probe_slide.py"
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                "def bigram(ch):\n"
                "    out = []\n"
                "    for i in range(len(ch) - 1):\n"
                "        out.append(ch[i:i + 2])\n"
                "    return out\n"
                "\n"
                "def adj(x):\n"
                "    for i in range(len(x) - 1):\n"
                "        if x[i] > x[i + 1]:\n"
                "            return True\n"
                "    return False\n"
                "\n"
                "def real_offbyone(items):\n"
                "    s = 0\n"
                "    for i in range(len(items) - 1):\n"
                "        s += items[i]\n"
                "    return s\n")
        with open(p, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=p)
        out = CA._check_range_len_minus({"backend/_probe_slide.py": (p, tree)})
        lines = [x["line"] for x in out]
        assert len(out) == 1, "滑窗降噪不对：报了 %d 条（行 %s）" % (len(out), lines)
        assert lines[0] == 15, "该报第 15 行（真 off-by-one），实际报 %s" % lines
    finally:
        shutil.rmtree(d, ignore_errors=True)


@case("conclusion.闲聊/解释类不套结论引导")
def _c_conf():
    """结论前置只对「用户问确认句 + 模型以排查过程开头」生效，闲聊绝不能被改。"""
    M = _clip_cases()
    f = getattr(M, "_force_conclusion_first", None)
    assert f is not None, "_force_conclusion_first 不见了（它必须被接线）"
    body = "让我确认一下当前状态。\n\n检查完了，一切正常。"
    # ① 用户问确认句 + 过程话术开头 → 前置引导
    got = f(body, "好了吗")
    assert got != body and got.startswith("结论："), "确认句没有前置结论引导：%r" % got[:60]
    # ② 用户只是打招呼 → 一个字都不许改
    assert f(body, "你好") == body, "闲聊轮次被套上了结论引导（正是要避免的过度格式化）"
    # ③ 已有结论 → 不重复加
    s2 = "结论：正常。\n\n细节略。"
    assert f(s2, "好了吗") == s2, "已经有「结论：」却又加了一遍"


@case("audit.logic.索引偏移越界")
def _c_l10():
    from . import code_audit as CA
    out = CA._check_index_shift(_trees_of(
        "def f(xs):\n    for i in range(len(xs)):\n        print(xs[i + 1])\n"))
    assert len(out) == 1, "没抓到 xs[i+1] 越界（返回 %d 条）" % len(out)
    out2 = CA._check_index_shift(_trees_of(
        "def f(xs):\n    for i in range(len(xs)):\n        print(xs[i])\n"))
    assert not out2, "正常访问 xs[i] 被误报了：%s" % out2


# ---------------------------------------------------------------- 修复质量闸门
@case("fix.占位式修补被拦下")
def _c_q1():
    from . import self_iterate as SI
    act = {"targets": [{"kind": "undefined-call", "file": "backend/x.py"}]}
    q = SI._fix_quality(act, [{
        "file": "backend/x.py",
        "find": "def f():\n    return this_missing_func(1, 2)\n",
        "replace": "def f():\n    return 0\n"}])
    assert q, "把「调用不存在的函数」换成 `return 0` 居然放过了"
    q2 = SI._fix_quality(act, [{
        "file": "backend/x.py",
        "find": "def g():\n    return undefined_variable_xyz + 1\n",
        "replace": "def g():\n    return 1\n"}])
    assert q2, "把「未定义变量」换成 `return 1` 居然放过了"
    # 换汤不换药：不是 return，而是补一句 `x = 0`（实测模型真这么修）
    q3 = SI._fix_quality(act, [{
        "file": "backend/x.py",
        "find": "def g():\n    return undefined_variable_xyz + 1\n",
        "replace": "def g():\n    undefined_variable_xyz = 0\n"
                   "    return undefined_variable_xyz + 1\n"}])
    assert q3, "给未定义变量赋常量 `= 0` 居然放过了"
    # 把调用整段删掉（return f(x) → return x）：计算没了，需人工确认
    q4 = SI._fix_quality(act, [{
        "file": "backend/x.py",
        "find": "def h(x):\n    return this_missing_func(x)\n",
        "replace": "def h(x):\n    return x\n"}])
    assert q4, "把不存在的调用整段删掉居然放过了"


@case("fix.正确修法不被误杀")
def _c_q2():
    from . import self_iterate as SI
    ok_cases = [
        ("默认参数改 None",
         "def f(a, b=[]):\n    b.append(a)\n",
         "def f(a, b=None):\n    if b is None:\n        b = []\n    b.append(a)\n"),
        ("is → ==", "    if x is 'admin':\n", "    if x == 'admin':\n"),
        ("删掉 return 之后的死代码", "    return 1\n    return 2\n", "    return 1\n"),
        ("finally 里的 return 改成 pass", "    finally:\n        return 2\n",
         "    finally:\n        pass\n"),
        ("裸 except 改成具体异常",
         "    try:\n        x()\n    except:\n        pass\n",
         "    try:\n        x()\n    except Exception as e:\n        print(e)\n"),
    ]
    for name, find, repl in ok_cases:
        q = SI._fix_quality({}, [{"file": "backend/x.py", "find": find,
                                  "replace": repl}])
        assert not q, "正确修法「%s」被误杀了：%s" % (name, q)
    # 目标里含 undefined-call 时，`assert (x, 'b')` → `assert x, 'b'` 不能被
    # 「删掉不存在的调用」规则误杀（assert 长得像函数调用，实测踩过这个坑）
    q = SI._fix_quality({"targets": [{"kind": "undefined-call"}]},
                        [{"file": "backend/x.py",
                          "find": "    assert (x, 'b')\n",
                          "replace": "    assert x, 'b'\n"}])
    assert not q, "assert 元组的正确修法被「删掉调用」规则误杀了：%s" % q


# ---------------------------------------------------------------- 边界（防 off-by-one）
@case("clip.长度恰好等于上限时不裁剪")
def _c_clip_edge():
    M = _clip_cases()
    s = ("结论：已完成。\n" + ("正文内容。" * 200))[:600]
    got = M._clip_reply(s, limit=600)
    assert got == s, "刚好等于上限也被裁了（%d → %d 字）" % (len(s), len(got))



# ---------------------------------------------------------------- 全量（full）
@case("audit.全量自检无 high", scope="full")
def _c_audit():
    from . import code_audit as CA
    r = CA.audit()
    highs = [f for f in (r.get("findings") or []) if f.get("level") == "high"]
    assert not highs, "自检报出 %d 条 high：%s" % (
        len(highs), "；".join("%s:%s" % (f["file"], f["line"]) for f in highs[:5]))


@case("imports.全模块可导入", scope="full")
def _c_imports():
    import importlib
    bad = []
    for p in sorted(_BACKEND.glob("*.py")):
        nm = p.stem
        if nm.startswith("_") or nm in ("__init__", "__main__"):
            continue
        try:
            importlib.import_module("backend.%s" % nm)
        except Exception as e:                              # noqa: BLE001
            bad.append("%s: %s: %s" % (nm, type(e).__name__, e))
    assert not bad, "有 %d 个模块导入失败：%s" % (len(bad), "；".join(bad[:4]))


# ---------------------------------------------------------------- 运行
_FEATURE_DIR = _BACKEND / "_feature_tests"


class _KeepOpenBytesIO(io.BytesIO):
    """永不真正关闭的字节流，专门当「假 stdout」的底座。

    ⚠️ 2026-11-22 实测踩到的坑：断言文件里有这种写法

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    赋值那一刻，**原来那个** TextIOWrapper 引用归零被回收，它的 __del__
    会顺手 close 底层 buffer。于是新包出来的 wrapper 一 print 就抛
    `ValueError: I/O operation on closed file` —— 看起来像是测试文件坏了，
    其实是这个「谁关了 buffer」的连锁反应。
    把 close() 变成空操作，buffer 就一直活着，两边都能写。
    """

    def close(self):
        try:
            self.flush()
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    def __del__(self):
        pass


def _bad_fn(fname, why):
    """造一个「加载期就失败」的用例：跑起来直接抛 AssertionError。"""
    def _bad():
        raise AssertionError("断言文件加载失败 %s：%s" % (fname, why))
    return _bad


def _pass_fn():
    """造一个「加载期已经跑完且全过」的用例（纯脚本式断言用）。"""
    def _ok():
        return None
    return _ok


def _load_dynamic():
    """加载「新功能自带的断言」—— backend/_feature_tests/*.py。

    为什么需要这一层
    ----------------
    智能体自主加的新功能，通用回归里**没有它的用例**，改坏了根本发现不了。
    用户明确要求「需要验证所有功能是否正常」，所以每条新功能必须把自己的
    验收断言写成一个独立文件：模块里提供 run()，断言失败就抛 AssertionError。

    为什么放独立目录而不是往本文件里塞
    --------------------------------
    自动生成的内容一旦混进人工维护的用例里，出问题很难分辨是谁的锅、
    删也删不干净。放独立目录：坏了直接删那个文件，不用动 regress.py。

    约定：文件里**不联网、不调 LLM、不加载模型**，必须秒级可重复。
    单个文件加载失败只记一条失败用例，绝不让整个回归崩掉。
    """
    out = []
    try:
        if not _FEATURE_DIR.is_dir():
            return out
        files = sorted(_FEATURE_DIR.glob("*.py"))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return out
    import importlib.util
    import io
    for p in files:
        if p.name.startswith("_"):
            continue
        name = "feature.%s" % p.stem
        # ⚠️ 2026-11-22 修：断言文件是**独立脚本**写法，有三种形态
        #   (a) 提供 run()
        #   (b) 只有 main()（返回 0=通过、非 0=失败）
        #   (c) 纯模块级脚本，靠 sys.exit(0/1) 表达结果
        # 原来只认 (a)，(b)(c) 全被记成「缺少 run() 函数」—— 实测 11 条
        # 回归全挂、白丢 8.68 分。那 11 条**不是功能坏了**，是断言文件
        # 加载不进来，等于这些功能长期没人守着。
        # 另外 test_tool_registry.py 会重新包装 sys.stdout，把真实 stdout
        # 的 buffer 一起关掉，导致**后面所有文件**报
        # `ValueError: I/O operation on closed file`。所以每次加载都换成
        # 独立的、带 buffer 的假 stdout，跑完立刻恢复。
        saved_out = sys.stdout
        sys.stdout = io.TextIOWrapper(_KeepOpenBytesIO(), encoding="utf-8",
                                      errors="replace")
        mod = None
        executed_ok = False
        load_err = None
        try:
            spec = importlib.util.spec_from_file_location(
                "backend._feature_tests.%s" % p.stem, str(p))
            if spec is None or spec.loader is None:
                raise ImportError("无法构建模块规格")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            executed_ok = True          # 模块级执行完毕、没有抛异常
        except SystemExit as e:
            # 脚本式断言：exit(0)/exit(None) = 全部通过，其余算失败
            code = getattr(e, "code", None)
            if code in (0, None):
                executed_ok = True
            else:
                load_err = "脚本式断言未通过（sys.exit(%r)）" % (code,)
        except BaseException as e:                      # noqa: BLE001
            # ⚠️ 2026-11-22 实测致命缺陷：_feature_tests/test_llm_retry.py 在
            #    **模块级**调用了 sys.exit(0)，而 exec_module 会把 SystemExit
            #    抛给调用方。原来这里只捕 Exception，SystemExit 继承自
            #    BaseException 捕不到 —— 于是整个进程（连同 Web 服务）被静默
            #    杀掉，连外层 except 都接不到（实测：进程内跑 regress.run()
            #    后 rc=0 但后续代码一行都不执行）。
            #    这里必须捕 BaseException；KeyboardInterrupt 要放回去，
            #    否则用户 Ctrl+C 会被当成「断言文件加载失败」吞掉。
            if isinstance(e, KeyboardInterrupt):
                raise
            load_err = "%s: %s" % (type(e).__name__, e)
        finally:
            sys.stdout = saved_out
        if load_err:
            out.append({"name": name, "scope": "quick",
                        "fn": _bad_fn(p.name, load_err)})
            continue
        fn = getattr(mod, "run", None)
        if not callable(fn):
            fn = getattr(mod, "main", None)
        if callable(fn):
            out.append({"name": name, "scope": "quick", "fn": fn})
        elif executed_ok:
            out.append({"name": name, "scope": "quick", "fn": _pass_fn()})
        else:
            out.append({"name": name, "scope": "quick",
                        "fn": _bad_fn(p.name, "既没有 run() 也没有 main()")})
    return out


def run(scope="quick", only=None):
    """跑一遍回归。scope=quick 只跑快用例；full 追加全量自检与导入检查。"""
    t0 = time.time()
    base = _CASES + _load_dynamic()
    if only:
        picked = [c for c in base if c["name"] in only]
    elif scope == "full":
        picked = list(base)
    else:
        picked = [c for c in base if c["scope"] == "quick"]

    items, passed, failed, skipped = [], 0, 0, 0
    import io
    for c in picked:
        # 断言文件里有会重写 sys.stdout 的（test_tool_registry.py），跑完会
        # 把真实 stdout 弄坏，后面所有用例全报
        # `ValueError: I/O operation on closed file`。每个用例都换独立的
        # 假 stdout，跑完立刻恢复。
        saved_out = sys.stdout
        sys.stdout = io.TextIOWrapper(_KeepOpenBytesIO(), encoding="utf-8",
                                      errors="replace")
        try:
            r = c["fn"]()
            if r == "skip":
                skipped += 1
                items.append({"name": c["name"], "ok": True, "skipped": True})
            elif isinstance(r, int) and not isinstance(r, bool) and r != 0:
                # 脚本式断言约定：main() 返回 0=通过、非 0=失败
                failed += 1
                items.append({"name": c["name"], "ok": False,
                              "why": "main() 返回 %d（非 0 表示失败）" % r})
            else:
                passed += 1
                items.append({"name": c["name"], "ok": True})
        except SystemExit as e:
            # main() 里可能 sys.exit(0/1)：0 或 None 算通过
            code = getattr(e, "code", None)
            if code in (0, None):
                passed += 1
                items.append({"name": c["name"], "ok": True})
            else:
                failed += 1
                items.append({"name": c["name"], "ok": False,
                              "why": "sys.exit(%r)" % (code,)})
        except AssertionError as e:
            failed += 1
            items.append({"name": c["name"], "ok": False, "why": str(e)[:400]})
        except Exception as e:                              # noqa: BLE001
            failed += 1
            items.append({"name": c["name"], "ok": False,
                          "why": "%s: %s" % (type(e).__name__, e)})
        finally:
            sys.stdout = saved_out

    failed_items = [i for i in items if not i["ok"]]
    return {
        "ok": failed == 0,
        "scope": scope,
        "total": len(picked),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "seconds": round(time.time() - t0, 2),
        "items": items,
        "failed_items": failed_items,
        "summary": ("全部通过（%d 条，跳过 %d 条）" % (passed, skipped)) if failed == 0
                   else ("%d/%d 条回归失败：%s" % (
                       failed, len(picked),
                       "；".join("%s（%s）" % (i["name"], i["why"][:80])
                                for i in failed_items[:3]))),
    }


_MARK = "@@REGRESS@@"
# ⚠️ 2026-11-22 实测修掉的致命 bug：
#   regress.run() 内部有测试会 **关掉 sys.stdout**，所以子进程末尾的
#   print('@@REGRESS@@' + json) 必然抛
#   `ValueError: I/O operation on closed file` ——
#   run_disk() 因此**一直是坏的**（永远返回「回归子进程没跑起来」，total=0）。
#   改成把结果**写进临时文件**，完全不依赖 stdout。
_DISK_RUNNER = (
    "import io,sys\n"
    "sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')\n"
    "sys.path.insert(0,'.')\n"
    "import json\n"
    "from backend import regress\n"
    "out=sys.argv[2]\n"
    "r=regress.run(scope=sys.argv[1])\n"
    "open(out,'w',encoding='utf-8').write(json.dumps(r,ensure_ascii=False))\n"
)


def run_disk(scope="quick", timeout=300):
    """在**子进程**里跑回归 —— 读的是磁盘上刚写完的最新代码。

    为什么必须是子进程（这轮实测踩到的坑）
    ------------------------------------
    execute_approved 把 patch 写到文件之后，当前进程里的模块还是**改动前**的
    旧代码 —— Python 不会因为文件变了就自动重新加载。所以在进程内跑回归，
    等于在测旧代码：实测把 `_norm_rel` 的路径校验改坏（放行 data/secrets.json），
    进程内回归**照样全过**，于是「改坏了自动回滚」这道兜底形同虚设。

    子进程从磁盘重新 import，验的才是刚改的代码。代价约 1~2 秒，值得。
    """
    import subprocess
    t0 = time.time()
    err_box = {"ok": False, "scope": scope, "total": 0, "passed": 0, "failed": 1,
               "skipped": 0, "seconds": 0.0, "items": [], "failed_items": []}
    # 结果走临时文件：regress.run() 会关掉 stdout，print 传不出来（见 _DISK_RUNNER 注释）
    import tempfile
    _fd, _out_path = tempfile.mkstemp(prefix="regress_", suffix=".json")
    os.close(_fd)
    try:
        os.unlink(_out_path)                 # 先删，跑完再看有没有重新写出来
    except OSError:                                     # noqa: BLE001 删临时结果文件失败不影响后续流程
        pass
    try:
        r = subprocess.run([sys.executable, "-c", _DISK_RUNNER, scope, _out_path],
                           cwd=str(_ROOT), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        err_box["seconds"] = round(time.time() - t0, 2)
        err_box["failed_items"] = [{"name": "regress", "why": "子进程超时"}]
        err_box["summary"] = "回归子进程超时（%d 秒）" % timeout
        return err_box

    line = ""
    try:
        with open(_out_path, encoding="utf-8") as _fh:
            line = _fh.read().strip()
    except Exception:                                       # noqa: BLE001
        line = ""
    try:
        os.unlink(_out_path)
    except OSError:                                     # noqa: BLE001 清理临时结果文件失败不影响结果
        pass
    if not line:
        err = (r.stderr or b"").decode("utf-8", "replace")[-500:]
        err_box["seconds"] = round(time.time() - t0, 2)
        err_box["failed_items"] = [{"name": "regress", "why": err or "无输出"}]
        err_box["summary"] = "回归子进程没跑起来（rc=%s）：%s" % (r.returncode, err[:200])
        return err_box
    try:
        out = json.loads(line)
    except Exception as e:                                  # noqa: BLE001
        err_box["seconds"] = round(time.time() - t0, 2)
        err_box["failed_items"] = [{"name": "regress", "why": str(e)}]
        err_box["summary"] = "回归结果解析失败：%s" % e
        return err_box
    out["via"] = "subprocess"
    return out


def as_text(r):
    """给聊天界面/日志用的单行摘要。"""
    lines = ["回归 %s：%s（%.2fs）" % (r.get("scope"), r.get("summary"), r.get("seconds") or 0)]
    for i in r.get("failed_items") or []:
        lines.append("  ✗ %s —— %s" % (i["name"], i.get("why") or ""))
    return "\n".join(lines)


def dump_clip_failures(scope="quick", timeout=300):
    """跑一遍回归，把 clip.* 用例的真实失败信息 dump 出来。

    为什么单独开一个（2026-11-17 用户问「为什么又失败了」）：
      上一轮只查到「clip 6 条断言失败」的位置，没真跑 —— 于是只能猜原因。
      这里直接执行 run_disk()，把每条 clip.* 用例的 name + why 原样返回，
      调用方写进 actlog 的 fail_kind，用户下次能看到真实报错而不是猜测。

    返回：
      {
        "ok": bool,              # 所有 clip.* 用例是否全过
        "total": int,            # clip.* 用例总数
        "failed": int,           # 失败条数
        "items": [{"name":..., "why":...}, ...],   # 失败明细（why 已截断）
        "summary": str,          # 一行摘要，可直接写进 fail_kind
        "raw": dict,             # run_disk 的完整结果（便于排查）
      }
    任何异常都返回 ok=False 且 summary 带异常信息 —— 不能因为 dump 失败
    就让调用方以为「全过了」。
    """
    try:
        raw = run_disk(scope=scope, timeout=timeout)
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "total": 0, "failed": 1,
                "items": [{"name": "regress", "why": "%s: %s" % (
                    type(e).__name__, e)}],
                "summary": "回归没跑起来：%s: %s" % (type(e).__name__, e),
                "raw": {}}
    all_items = raw.get("items") or []
    clip_items = [i for i in all_items if str(i.get("name") or "").startswith("clip.")]
    failed = [i for i in clip_items if not i.get("ok")]
    if not clip_items:
        # 一条 clip 用例都没跑到 —— 说明回归本身没跑起来，不能当成「全过」
        return {"ok": False, "total": 0, "failed": 1,
                "items": [{"name": "clip.*", "why": raw.get("summary") or "没跑到 clip 用例"}],
                "summary": "没跑到 clip 用例：%s" % (raw.get("summary") or "未知原因"),
                "raw": raw}
    if not failed:
        return {"ok": True, "total": len(clip_items), "failed": 0,
                "items": [],
                "summary": "clip %d 条全过" % len(clip_items),
                "raw": raw}
    detail = [{"name": i.get("name"), "why": str(i.get("why") or "")[:400]}
              for i in failed]
    summary = "clip %d/%d 失败：%s" % (
        len(failed), len(clip_items),
        "；".join("%s（%s）" % (d["name"], d["why"][:80]) for d in detail[:3]))
    return {"ok": False, "total": len(clip_items), "failed": len(failed),
            "items": detail, "summary": summary, "raw": raw}


# ---------------------------------------------------------------- snapdiff（2026-11-15）
@case("snapdiff.相同行为不报差异")
def _c_snap_same():
    from . import snapdiff
    import types
    m = types.ModuleType("m")
    m.add = lambda a, b: a + b
    before = snapdiff.snapshot(m, ["add"])
    after = snapdiff.snapshot(m, ["add"])
    d = snapdiff.diff(before, after)
    assert d == [], "同样实现却报出差异：%r" % (d,)


@case("snapdiff.行为变化必须报出来")
def _c_snap_changed():
    from . import snapdiff
    import types
    m = types.ModuleType("m")
    m.add = lambda a, b: a + b
    before = snapdiff.snapshot(m, ["add"])
    m.add = lambda a, b: str(a + b)      # 返回值类型变了
    after = snapdiff.snapshot(m, ["add"])
    d = snapdiff.diff(before, after)
    assert len(d) >= 1, "返回值类型变了却没报差异"
    assert d[0]["name"] == "add", d


# ---------------------------------------------------------------- reply_gate（2026-11-15）
@case("reply-gate.长过程折叠且 details 闭合")
def _c_gate_fold():
    from .reply_gate import format_reply
    out = format_reply("结论：已修好。", "过程。" * 500, "产物：a.diff", 4000)
    assert "结论：已修好。" in out, "结论被弄丢了"
    assert "<details" in out and "</details>" in out, "长过程应折叠"
    assert out.count("<details") == out.count("</details>"), "details 没闭合"
    assert len(out) <= 4000, "折叠后仍超限：%d" % len(out)


@case("reply-gate.短过程不折叠")
def _c_gate_short():
    from .reply_gate import format_reply
    out = format_reply("结论：ok", "短过程", "", 4000)
    assert "<details" not in out, "短过程不该折叠"


@case("reply-gate.超长时不留未闭合 details")
def _c_gate_unbalanced():
    from .reply_gate import format_reply
    # 过程长到连折叠块都放不下：必须整块换成一行提示，不能截半截
    out = format_reply("结论：短。", "过程" * 9000, "", 800)
    assert out.count("<details") == out.count("</details>"), \
        "留下了未闭合的 <details>（浏览器会把后面内容全吞掉）"
