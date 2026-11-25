# -*- coding: utf-8 -*-
"""终止要「秒停」：长操作必须能被终止打断 —— 2026-11-26 实测回归。

背景（用户实测报的问题，不是推测）：
    点「终止」后界面显示 cancelling，实测 4 分钟才真正结束。用真实接口复现后
    定位到三处**同步阻塞、中途不看终止标志**的长操作：
      ① iter_runner._run_regress_baseline  quick 回归约 50 秒
      ② self_iterate._precheck_actions   全项目静态扫描约 90 秒
      ③ 收尾 _emit_closeout 里的 RSI 采样（内部算分/扫描）
    本测试锁住修好后的行为：终止标志一置，这三处都立刻放弃等待。
    全程只跑离线函数，不联网、不动真实账本。
"""
import inspect
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import iter_runner as IR      # noqa: E402
from backend import self_iterate as SI    # noqa: E402

_ok = [0]
_fail = [0]


def chk(name, cond, extra=""):
    if cond:
        _ok[0] += 1
        print("  OK  ", name, extra)
    else:
        _fail[0] += 1
        print("  FAIL", name, extra)


print("[A] _run_with_cancel：终止时不等长操作")
t0 = time.time()
r = IR._run_with_cancel(lambda: time.sleep(5) or "done", lambda: True)
dt = time.time() - t0
chk("已终止 → 不执行、立刻返回 None", r is None and dt < 0.5, "%.2fs" % dt)

t0 = time.time()
r = IR._run_with_cancel(lambda: 42, lambda: False)
dt = time.time() - t0
chk("未终止 → 正常拿到结果", r == 42 and dt < 0.5, "%.2fs" % dt)

flag = {"v": False}
threading.Timer(0.4, lambda: flag.__setitem__("v", True)).start()
t0 = time.time()
r = IR._run_with_cancel(lambda: time.sleep(3) or "x", lambda: flag["v"])
dt = time.time() - t0
chk("跑到一半被终止 → 立刻放弃等待", r is None and dt < 1.5, "%.2fs" % dt)

print("[B] 基线回归：已终止就不跑")
t0 = time.time()
b = IR._run_regress_baseline(where="test", should_cancel=lambda: True)
dt = time.time() - t0
chk("已终止 → 返回空基线且秒回", b == {} and dt < 0.5, "%.2fs" % dt)

print("[C] 执行前复核：已终止就不扫全项目")
t0 = time.time()
pc, note, base = SI._precheck_actions(
    [{"id": "a1", "files": ["backend/x.py"]}], should_cancel=lambda: True)
dt = time.time() - t0
chk("已终止 → 跳过静态扫描并说明原因",
    pc == {} and "终止" in str(note) and dt < 0.5, "%.2fs / %s" % (dt, str(note)[:16]))

print("[D] 收尾：终止走极简报告，不跑回归/评分")
src = inspect.getsource(IR._worker)
chk("终止分支有独立收尾", "本轮已终止" in src)
chk("收尾回归只给未终止的轮次",
    "allow_run=bool(changed))" in src and "allow_run=bool(changed) and not cancelled" not in src)
chk("开工前会再查一次终止", "开工前被终止" in src)

print("[E] 取消标志本身仍然生效（原有语义没被改坏）")
chk("cancel() 仍返回 cancelling 状态",
    inspect.getsource(IR.cancel).count("cancelling") >= 2)

print("\n通过 %d / 失败 %d" % (_ok[0], _fail[0]))
sys.exit(0 if _fail[0] == 0 else 1)
