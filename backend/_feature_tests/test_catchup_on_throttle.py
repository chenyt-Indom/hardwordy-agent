# -*- coding: utf-8 -*-
"""节流挡掉触发后的补跑链路 —— 离线回归，只碰临时文件。

用户 2026-11-25 要求「把机制的逻辑缺口补上，然后反复验证」。

实测背景（真实数据，不是猜的）：
  · 01:55:27 之后 rsi_history 一直停在 40 个点，44 分钟没有新采样；
  · 追下去发现 02:39:12 那次前端重推被**节流**挡下（"距上次只过了 201 秒"），
    而节流分支原来只 `return`，**不重排** → 这次触发永久丢失；
  · 同时 boot_resume 的补跑延迟只按 boot_gap 算（180+3 秒），
    必然落在 300 秒节流窗口里 → 补跑被挡 → 也没人再来跑。
  两处叠起来 = 「重启后自动拉起」看起来没生效、曲线一动不动。

这里验五件事：
  1) 被节流挡掉的触发**不丢弃**：按剩余等待时间安排补跑；
  2) 补跑到点后**复核闸门**：还没开就按剩余时间再排（不是硬跑）；
  3) 重排最多 3 次，避免「排了被挡 → 再排」变成死循环；
  4) boot_resume 的延迟把节流算进去（取启动冷却/节流的较大值）；
  5) 自迭代每轮收尾会补采一次 RSI 样本（曲线才会跟着迭代走）。

跑法：python backend/_feature_tests/test_catchup_on_throttle.py
"""
import inspect
import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from pathlib import Path                             # noqa: E402

from backend import selfloop as SL                   # noqa: E402
from backend import evolve_budget as EB              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _reset(tmp_path, **kw):
    """写一份干净的临时状态文件，并把内存里的时间戳/队列复位。"""
    d = {
        "enabled": True, "daily_plans": 50, "min_gap_sec": 300, "task_fail_gap": 0,
        "boot_gap_sec": 0, "backlog_gate": 0, "backlog_free_hours": 24,
        "plans": 0, "triggers": 0, "day": time.strftime("%Y-%m-%d"),
        "last_at": 0, "seen": {},
    }
    d.update(kw)
    Path(tmp_path).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    SL._LAST_AT[0] = 0.0
    SL._BUSY[0] = False
    SL._CATCHUP.clear()
    SL._CATCHUP_TRIES.clear()
    # 缓存钉住：否则 TTL 一过就去读真实 plans.json，把用例污染成「被积压挡住」
    SL._BACKLOG_CACHE["at"] = time.time()
    SL._BACKLOG_CACHE["n"] = 0


FIRED = []
SYNC = {"n": 0}


class _FakeTimer:
    """同步执行第一次回调（便于断言），之后只记录延迟不执行（防递归）。"""

    def __init__(self, delay, func, args=(), kwargs=None):
        self.delay, self.func = delay, func

    def start(self):
        FIRED.append(round(self.delay, 1))
        if SYNC["n"] < 1:
            SYNC["n"] += 1
            self.func()


def main():
    print("=" * 62)
    print("节流挡掉 → 补跑链路（逻辑缺口回归）")
    print("=" * 62)

    real_path = SL._path()
    real_timer = SL.threading.Timer
    real_cycle = SL._cycle
    real_maybe = SL._maybe_think
    real_gate_left = SL._gate_wait_left
    real_eb_path = EB._path()

    tmp = os.path.join(tempfile.gettempdir(), "_catchup_throttle_test.json")
    tmp_eb = os.path.join(tempfile.gettempdir(), "_catchup_throttle_eb.json")
    for p in (tmp, tmp_eb):
        if os.path.exists(p):
            os.remove(p)
    SL._path = lambda: Path(tmp)
    EB.set_path(tmp_eb)

    calls = []

    def fake_cycle(reason, focus_user="", source="chat"):
        calls.append({"reason": reason, "source": source})
        SL._BUSY[0] = False

    SL._cycle = fake_cycle

    class _NoThread:
        """别真开线程，直接同步执行 —— 结果可断言。"""

        def __init__(self, target=None, args=(), **kw):
            self._t, self._a = target, args

        def start(self):
            if self._t:
                self._t(*self._a)

    try:
        # ---------- 1. 被节流挡掉 → 必须安排补跑（原来直接丢弃） ----------
        print("\n[1] 节流挡掉不再丢弃")
        _reset(tmp, min_gap_sec=300, boot_gap_sec=0)
        SL._LAST_AT[0] = time.time() - 100          # 节流窗口内
        q = []
        real_sched = SL._schedule_catchup
        SL._schedule_catchup = lambda delay, reason, focus_user="", source="chat", sig="": (
            q.append(round(delay, 1)), True)[1]
        r = SL._maybe_think("节流窗口内的真实需求", source="chat", sig="thr1")
        check("节流挡掉，返回 skipped", r.get("skipped") is not None, str(r))
        check("★ 挡掉后安排了补跑", len(q) == 1 and 195 <= q[0] <= 210, "delay=%s" % q)
        check("跳过原因写明「已安排补跑」", "补跑" in str(r.get("skipped")),
              str(r.get("skipped")))
        SL._schedule_catchup = real_sched

        # ---------- 2. 补跑到点后复核闸门：没开就再排，不硬跑 ----------
        print("\n[2] 补跑自身复核闸门（自我纠偏）")
        _reset(tmp, min_gap_sec=300, boot_gap_sec=0)
        calls.clear()
        SL._maybe_think = lambda reason, focus_user="", source="chat", gap=None, sig="": (
            calls.append(reason), {"ok": True})[1]
        SL.threading.Timer = _FakeTimer
        FIRED.clear()
        SYNC["n"] = 0
        SL._gate_wait_left = lambda d=None: 1.2     # 到点了但闸门还差 1.2 秒
        SL._schedule_catchup(1.0, "补跑测试A", source="chat", sig="kA")
        check("闸门未开 → 不硬跑", len(calls) == 0, str(calls))
        check("闸门未开 → 按剩余时间再排一次", len(FIRED) == 2 and FIRED[1] >= 3.0,
              str(FIRED))
        check("闸门未开 → 记重排 1 次", SL._CATCHUP_TRIES.get("kA") == 1,
              str(SL._CATCHUP_TRIES))

        # ---------- 3. 闸门开了 → 真的执行 ----------
        print("\n[3] 闸门开了就真跑")
        calls.clear()
        FIRED.clear()
        SYNC["n"] = 0
        SL._gate_wait_left = lambda d=None: 0.0
        SL._schedule_catchup(1.0, "补跑测试B", source="chat", sig="kB")
        check("补跑真的执行了", len(calls) == 1, str(calls))
        check("执行后从队列摘掉", "kB" not in SL._CATCHUP, str(list(SL._CATCHUP)))
        check("执行后重试计数清空", "kB" not in SL._CATCHUP_TRIES,
              str(SL._CATCHUP_TRIES))

        # ---------- 4. 重排上限：3 次后不再排（防死循环） ----------
        print("\n[4] 重排次数上限")
        calls.clear()
        FIRED.clear()
        SYNC["n"] = 0
        SL._CATCHUP_TRIES["kC"] = 3
        SL._gate_wait_left = lambda d=None: 5.0
        SL._schedule_catchup(1.0, "补跑测试C", source="chat", sig="kC")
        check("已达上限 → 不再重排、直接执行", len(calls) == 1 and len(FIRED) == 1,
              "calls=%s fired=%s" % (len(calls), FIRED))
        check("已达上限 → 计数清掉", "kC" not in SL._CATCHUP_TRIES,
              str(SL._CATCHUP_TRIES))

        # ---------- 5. 重启自愈的延迟要把节流算进去 ----------
        print("\n[5] 重启自愈延迟含节流")
        _reset(tmp, min_gap_sec=300, boot_gap_sec=180, last_at=time.time() - 100)
        SL._gate_wait_left = real_gate_left
        SL._BOOT_TS[0] = time.time()                # 刚启动
        r = SL.boot_resume()
        # 节流还剩 200 秒 > 启动冷却 180 秒 → 应排 ~203 秒，而不是 183 秒
        check("★ 延迟取「启动冷却/节流」较大值（≈203 秒）",
              200 <= float(r.get("scheduled_in") or 0) <= 206, str(r))

        # ---------- 6. 迭代收尾会补采 RSI 样本 ----------
        print("\n[6] 迭代收尾补采 RSI（曲线跟着迭代走）")
        src = inspect.getsource(real_cycle)
        check("★ _cycle 收尾调用了 rsi_sample_throttled",
              "rsi_sample_throttled" in src, "源码里没找到采样调用")
        check("采样有节流，不会刷爆账本", "gap=600" in src.replace(" ", ""),
              "未见 gap 参数")

    finally:
        SL._path = real_path
        SL.threading.Timer = real_timer
        SL._cycle = real_cycle
        SL._maybe_think = real_maybe
        SL._gate_wait_left = real_gate_left
        SL._CATCHUP.clear()
        SL._CATCHUP_TRIES.clear()
        EB.set_path(real_eb_path)
        for p in (tmp, tmp_eb):
            try:
                os.remove(p)
            except OSError:
                pass

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败：%s" % f)
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
