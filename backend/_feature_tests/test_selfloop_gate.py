# -*- coding: utf-8 -*-
"""selfloop 触发闸门的离线回归 —— 只碰临时文件，绝不动真实状态。

用户 2026-11-24 要求「修 A（冷却后补跑），然后反复验证直到完全正常」。

实测背景（真实数据，不是猜的）：
  · `data/selfloop.json` 的 last_trigger 停在 2026-11-16 17:57，
    之后 8 天对话触发一次都没开跑；
  · 真因不是冷却，是 **backlog 168 条 > 阈值 30** 把闸门堵死；
  · 而定时任务照样在生成计划（11-23 82 条 / 11-24 86 条）→ 积压只增不减，
    永远解不了封 = 死锁；
  · 更糟的是跳过原因**从不落盘**，事后完全查不出是谁挡的。

这里验五类事：
  1) 冷却挡掉的触发**不丢弃**：排进补跑队列，冷却一过真的补跑；
  2) 同一需求不会重复排队（防定时器堆积）；
  3) backlog 死锁保险：停摆够久就放行一次，且**一天只放行一次**；
  4) 跳过原因写进 skip_log（可复盘）；
  5) 并发安全：两个线程同时来，只有一个能开跑。

跑法：python backend/_feature_tests/test_selfloop_gate.py
"""
import os
import sys
import time
import threading
import tempfile

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


def _reset_state(tmp_path, **kw):
    """写一份干净的临时状态文件，并把内存里的时间戳复位。"""
    import json
    d = {
        "enabled": True, "daily_plans": 50, "min_gap_sec": 0, "task_fail_gap": 0,
        "boot_gap_sec": 0, "backlog_gate": 30, "backlog_free_hours": 24,
        "plans": 0, "triggers": 0, "day": time.strftime("%Y-%m-%d"),
        "last_at": 0, "seen": {},
    }
    d.update(kw)
    Path(tmp_path).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    SL._LAST_AT[0] = 0.0
    SL._BUSY[0] = False
    SL._CATCHUP.clear()
    # ★ 必须把缓存**钉住**（at=现在、n=0），否则 60 秒 TTL 一过就会去读真实
    #   plans.json（实测 168 条），把测试用例污染成「被积压挡住」。
    SL._BACKLOG_CACHE["at"] = time.time()
    SL._BACKLOG_CACHE["n"] = 0


def main():
    print("=" * 62)
    print("selfloop 触发闸门（补跑 / 死锁保险 / 可观测）")
    print("=" * 62)

    real_path = SL._path()
    tmp = os.path.join(tempfile.gettempdir(), "_selfloop_gate_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    SL._path = lambda: Path(tmp)          # 全部读写都落到临时文件
    # ★ 2026-11-24 实测踩到：闸门里还有 evolve_budget 这一道，测试若放行真开跑，
    #   会往**真实**账本里记消耗 —— 我第一次跑测试就把当天 plans 花到 12/12，
    #   结果后面所有用例全被「预算不够」挡住。必须一并注入临时账本。
    real_eb_path = EB._path()
    tmp_eb = os.path.join(tempfile.gettempdir(), "_selfloop_gate_eb.json")
    if os.path.exists(tmp_eb):
        os.remove(tmp_eb)
    EB.set_path(tmp_eb)

    started = []

    def fake_cycle(reason, focus_user="", source="chat"):
        started.append({"reason": reason, "source": source})
        SL._BUSY[0] = False               # 真 _cycle 在 finally 里复位，这里照做

    real_cycle = SL._cycle
    real_thread = threading.Thread
    SL._cycle = fake_cycle

    class _NoThread:
        """别真开线程，直接同步执行 —— 结果可断言。"""

        def __init__(self, target=None, args=(), **kw):
            self._t, self._a = target, args

        def start(self):
            if self._t:
                self._t(*self._a)

    try:
        # ---------- 1. 冷却挡掉 → 排补跑 → 冷却结束真的补跑 ----------
        print("\n[1] 冷却后补跑（用户选的 A 方案）")
        _reset_state(tmp, boot_gap_sec=1)
        SL._BOOT_TS[0] = time.time()      # 刚启动，正在冷却期
        SL._BACKLOG_CACHE["n"] = 0
        r = SL._maybe_think("冷却期来的真实需求", source="chat", sig="sigA")
        check("冷却期被挡，返回 skipped",
              r.get("ok") and r.get("skipped"), repr(r))
        check("skipped 里明确写了会补跑",
              "补跑" in str(r.get("skipped") or ""), repr(r.get("skipped")))
        check("补跑已进队列", "sigA" in SL._CATCHUP, repr(SL._CATCHUP))
        check("此刻还没真开跑", len(started) == 0, repr(started))

        # ★ 2026-11-24：原来写死 sleep(6) —— 单跑必过，但 `run_all.py` 并发跑
        #   49 个测试时 CPU 被抢，定时器可能晚个几秒才触发，于是误判成「没补跑」。
        #   改成轮询等待：最多 20 秒，等到就立刻往下走。
        _deadline = time.time() + 20
        while time.time() < _deadline and len(started) < 1:
            time.sleep(0.2)
        if len(started) != 1:
            print("    诊断 skip_log:", (SL.load().get("skip_log") or [])[-4:])
            print("    诊断 CATCHUP:", SL._CATCHUP, " BUSY:", SL._BUSY[0])
        check("冷却结束后**自动补跑**了一轮", len(started) == 1, repr(started))
        check("补跑原因带「补跑」字样，能看出是补偿的",
              started and "补跑" in started[0]["reason"], repr(started[:1]))
        check("补跑完成后队列已清空", "sigA" not in SL._CATCHUP, repr(SL._CATCHUP))

        # ---------- 2. 同一需求不重复排队 ----------
        print("\n[2] 重复排队去重")
        _reset_state(tmp, boot_gap_sec=180)
        SL._BOOT_TS[0] = time.time()
        for _ in range(3):
            SL._maybe_think("同一条需求", source="chat", sig="sigB")
        check("同一 sig 连排 3 次只占 1 个补跑位",
              len(SL._CATCHUP) == 1, repr(SL._CATCHUP))
        SL._CATCHUP.clear()

        # ---------- 3. backlog 死锁保险 ----------
        print("\n[3] backlog 死锁保险（真因修复）")
        _reset_state(tmp, boot_gap_sec=0, last_at=time.time() - 30 * 3600)
        SL._BACKLOG_CACHE["n"] = 168      # 实测积压量
        SL._BACKLOG_CACHE["at"] = time.time()   # 钉住缓存，别去读真账本
        _n0 = len(started)
        r = SL._maybe_think("停摆很久后的触发", source="chat", sig="sigC")
        check("停摆 30 小时 + 积压 168 条 → **放行**", r.get("started") is True, repr(r))
        check("放行后真的开跑了", len(started) == _n0 + 1, repr(started[_n0:]))
        d = SL.load()
        check("开跑后 last_at 被更新（天然限流靠它）",
              float(d.get("last_at") or 0) > time.time() - 30, repr(d.get("last_at")))

        # 紧接着再来一次：last_at 刚更新 → stale_h 远小于 24 → 必须挡住
        r = SL._maybe_think("紧接着第二次", source="chat", sig="sigD")
        check("开跑后紧接着再触发被挡住（不会连环烧 token）",
              r.get("skipped") and not r.get("started"), repr(r))

        # ★ 回归：冷却期那次触发**不能**白吃放行名额（预演实测踩到的真 bug）
        _reset_state(tmp, boot_gap_sec=180, last_at=time.time() - 30 * 3600)
        SL._BACKLOG_CACHE["n"] = 168
        SL._BACKLOG_CACHE["at"] = time.time()
        SL._BOOT_TS[0] = time.time()
        SL._maybe_think("冷却期内的触发", source="chat", sig="sigF")
        SL._BOOT_TS[0] = time.time() - 200          # 冷却已过
        SL._BACKLOG_CACHE["at"] = time.time()
        _n1 = len(started)
        r = SL._maybe_think("补跑（冷却结束）", source="chat", sig="sigF")
        check("冷却期那次不白吃名额 → 补跑仍能放行", r.get("started") is True, repr(r))
        check("补跑确实开跑了", len(started) == _n1 + 1, repr(started[_n1:]))
        SL._CATCHUP.clear()

        _reset_state(tmp, boot_gap_sec=0, last_at=time.time() - 3600)
        SL._BACKLOG_CACHE["n"] = 168
        SL._BACKLOG_CACHE["at"] = time.time()
        r = SL._maybe_think("刚跑过一小时", source="chat", sig="sigE")
        check("只停摆 1 小时 → 照旧被挡（保险不能变成没闸门）",
              r.get("skipped") and not r.get("started"), repr(r))

        # ---------- 4. 跳过原因落盘 ----------
        print("\n[4] 跳过原因可复盘")
        d = SL.load()
        logs = d.get("skip_log") or []
        check("skip_log 有记录", len(logs) >= 1, repr(logs[-2:]))
        check("记录里写清了为什么（积压条数/阈值）",
              any("积压" in str(x.get("why")) for x in logs), repr(logs[-2:]))
        check("记录带时间戳", all(x.get("at") for x in logs), repr(logs[-1:]))

        # ---------- 5. 并发只有一个能开跑 ----------
        print("\n[5] 并发安全（一把锁）")
        _reset_state(tmp, boot_gap_sec=0, min_gap_sec=300, last_at=0)
        SL._BACKLOG_CACHE["n"] = 0
        SL._BACKLOG_CACHE["at"] = time.time()
        SL._cycle = fake_cycle
        results = []

        def worker(i):
            results.append(SL._maybe_think("并发 %d" % i, source="chat", sig="sigP%d" % i))

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        okn = sum(1 for r in results if r.get("started"))
        check("8 个并发只开跑 1 轮（不会并行改代码）", okn == 1, "开跑 %d 次" % okn)

        # ---------- 6. 手动触发永远放行 ----------
        print("\n[6] 手动触发不受闸门影响")
        _reset_state(tmp, boot_gap_sec=999, min_gap_sec=999, last_at=time.time())
        SL._BACKLOG_CACHE["n"] = 999
        SL._BACKLOG_CACHE["at"] = time.time()
        SL._BOOT_TS[0] = time.time()
        r = SL._maybe_think("立即反思", source="manual", sig="sigM")
        check("冷却 + 节流 + 积压全开也照样放行手动", r.get("started") is True, repr(r))

    finally:
        SL._path = real_path
        try:
            EB.set_path(real_eb_path)
            if os.path.exists(tmp_eb):
                os.remove(tmp_eb)
        except Exception:                               # noqa: BLE001
            pass
        SL._cycle = real_cycle
        SL._BOOT_TS[0] = time.time()
        SL._BUSY[0] = False
        SL._CATCHUP.clear()
        SL._BACKLOG_CACHE["at"] = 0.0
        SL._BACKLOG_CACHE["n"] = 0
        try:
            os.remove(tmp)
        except Exception:                               # noqa: BLE001
            pass

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
