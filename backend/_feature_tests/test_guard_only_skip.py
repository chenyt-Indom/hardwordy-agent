# -*- coding: utf-8 -*-
"""回归：注定失败的「护栏文件动作」不再白扣分（2026-11-24）。

实测背景（actlog 2026-11-24 05:42 a1）：
    计划生成了「修 backend/_feature_tests/test_anchor_diagnose.py:116 的
    silent-except」—— 而 _feature_tests/test_*.py 是**护栏文件**，模型无权写，
    执行到写盘必然被判「护栏文件被修改（不允许）」→ 整轮回滚 + 记一次 fail。
    这类动作**从生成那一刻就注定失败**：白烧一次模型调用（几十秒 + 一次云端
    调用），还把「动作成功率」的分母拉大 —— 越诚实生成「修测试」的动作，
    RSI 越难看。

守六条：
  1. 目标全是护栏文件 → 判得出来（要跳过）
  2. 只要有一个可改的文件 → 不拦（别误杀真能做的动作）
  3. 没写目标文件 → 不拦（靠模型自己定位）
  4. `_pending_*.py` 不算护栏（临时断言，本来就会被创建/清理）
  5. RSI 口径：「护栏文件被修改」不计入成功率分母，与 `_classify` 的 guard 一致
  6. 接线：执行循环里**真的调用了**预检（防「函数写了没接上」—— 历史上踩过）
"""
import inspect
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# 控制台默认 GBK，中文断言信息会变成乱码；统一按 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

_TMP = tempfile.mkdtemp(prefix="mm_guard_skip_")


def run() -> int:
    """跑测试，数据目录隔离**限定在本函数内**（跑完恢复原值）。"""
    old = os.environ.get("MM_DATA_DIR")
    os.environ["MM_DATA_DIR"] = _TMP
    try:
        return _run_body()
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old


def _run_body() -> int:
    # ⚠️⚠️ 2026-11-24 实测事故：本函数会**写** actlog.jsonl（造两条假账本验证 RSI 口径）。
    #   最初隔离只写在 run() 里，于是任何「绕过 run() 直接调 _run_body()」的路径
    #   （当时的反证脚本就是这么调的）都会把**真实账本覆盖成 2 条** ——
    #   539 条历史一次写没。所以隔离必须下沉到这里，且**幂等**：
    #   无论从哪个入口进来，只要没隔离就自己建一个临时目录。
    if not os.environ.get("MM_DATA_DIR"):
        os.environ["MM_DATA_DIR"] = tempfile.mkdtemp(prefix="mm_guard_skip_body_")
    from backend import iter_agent as IA
    from backend import lessonloop as LL
    from backend import self_iterate as SI

    fails = []

    def chk(cond, name):
        if not cond:
            fails.append(name)

    # ---------- 0. 前提：这几类文件到底算不算护栏 ----------
    chk(IA._is_guard("backend/_feature_tests/test_anchor_diagnose.py") is True,
        "前提：_feature_tests/test_*.py 必须是护栏文件（否则本测试没有意义）")
    chk(IA._is_guard("backend/_feature_tests/_pending_盯梢.py") is False,
        "前提：下划线开头的 _pending_*.py 不算护栏")
    chk(IA._is_guard("backend/jsonfix.py") is False,
        "前提：普通源码不是护栏")

    # ---------- 1. 目标全是护栏文件 → 判得出来 ----------
    got = SI._guard_only_targets(["backend/_feature_tests/test_rsi.py",
                                 "backend/regress.py",
                                 "backend/_feature_tests/test_anchor_diagnose.py"])
    chk(len(got) == 3, "全护栏目标应被判定出来（实际 %r）" % (got,))

    # ---------- 1b. 路径写法不同也要认（Windows 反斜杠 / ./ 前缀）----------
    got_bs = SI._guard_only_targets(["backend\\_feature_tests\\test_rsi.py"])
    chk(len(got_bs) == 1, "反斜杠路径也要认得出来（实际 %r）" % (got_bs,))
    got_dot = SI._guard_only_targets(["./backend/_feature_tests/test_rsi.py"])
    chk(len(got_dot) == 1, "./ 前缀路径也要认得出来（实际 %r）" % (got_dot,))

    # ---------- 2. 只要有一个能改 → 不拦 ----------
    got2 = SI._guard_only_targets(["backend/_feature_tests/test_rsi.py",
                                   "backend/jsonfix.py"])
    chk(got2 == [], "混合目标（有一个可改）不许拦（实际 %r）" % (got2,))

    # ---------- 3. 没写目标文件 → 不拦 ----------
    for empty in (None, [], ["", "  "]):
        chk(SI._guard_only_targets(empty) == [],
            "未指定目标文件时不许拦（%r）" % (empty,))

    # ---------- 4. 下划线临时文件不误伤 ----------
    got4 = SI._guard_only_targets(["backend/_feature_tests/_pending_盯梢.py"])
    chk(got4 == [], "_pending_*.py 不是护栏，不许拦（实际 %r）" % (got4,))

    # ---------- 5. RSI 口径：护栏拦截不进成功率分母 ----------
    row_gate = {"ts": "2026-11-24 05:42:53", "action": "a1",
                "files": ["backend/_feature_tests/test_rsi.py"],
                "result": "fail",
                "detail": "护栏文件被修改（不允许）：backend/_feature_tests/test_rsi.py"
                          "（已自动回滚）"}
    chk(LL._is_gate_block(row_gate) is True,
        "「护栏文件被修改」必须算护栏拦截（不算能力失败）")
    chk(SI.classify_fail(row_gate["detail"]) == "guard",
        "与 self_iterate.classify_fail 口径一致：同一条文案必须归 guard")

    dd = LL._data_dir()
    # 最后一道保险：无论环境变量怎么被设，只要落到项目真实的 data/self_iterate
    # 就立刻报错退出 —— 宁可测试失败，也绝不覆盖真账本（2026-11-24 已踩过一次）。
    _real = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "self_iterate")
    if os.path.abspath(dd) == os.path.abspath(_real):
        raise RuntimeError("测试拒绝写真实数据目录：%s" % dd)
    os.makedirs(dd, exist_ok=True)
    with open(os.path.join(dd, "actlog.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-11-24 05:40:00", "action": "a1",
                            "files": ["backend/jsonfix.py"], "result": "ok",
                            "detail": "改好了"}, ensure_ascii=False) + "\n")
        f.write(json.dumps(row_gate, ensure_ascii=False) + "\n")
    LL._RSI_CACHE["val"] = None
    LL._RSI_CACHE["ts"] = 0.0
    r = LL.rsi_index(limit=50, cache_sec=0)
    part = {p["name"]: p for p in (r.get("parts") or [])}.get("动作成功率") or {}
    chk(part.get("value") == 1.0,
        "1 条成功 + 1 条护栏拦截 → 成功率必须是 1.0（实际 %r，%r）"
        % (part.get("value"), part.get("detail")))

    # ---------- 6. 接线：执行循环里真的调了预检 ----------
    try:
        # ⚠️ execute_approved 外面套了进程级互斥装饰器 `_exec_guard`，它只复制了
        #    __name__/__doc__，**没有** __wrapped__ —— inspect.getsource 只会拿到
        #    那个十几行的 wrapper。所以用 AST 定位真正的函数体源码段。
        import ast
        import pathlib
        _p = pathlib.Path(SI.__file__)
        _txt = _p.read_text(encoding="utf-8")
        _tree = ast.parse(_txt)
        _node = next((n for n in _tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "execute_approved"),
                     None)
        src = (ast.get_source_segment(_txt, _node) or "") if _node else ""
    except Exception as e:                              # noqa: BLE001
        src = ""
        fails.append("读不到 execute_approved 源码：%s" % e)
    chk("_guard_only_targets(a.get(\"files\"))" in src,
        "execute_approved 里必须真的调用预检（函数写了没接上等于没做）")
    chk("已跳过（不计失败）" in src, "跳过时要说明「不计失败」，别让人误以为是故障")

    if fails:
        print("[FAIL] 护栏动作预检回归：%d 项未通过" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("[OK] 护栏动作预检回归：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
