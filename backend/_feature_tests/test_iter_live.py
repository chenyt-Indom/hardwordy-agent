"""iter_runner 的 worker 存活判定 + 终止意图 功能测试。

背景（2026-11-23 用户报）：任务每 20 秒重复一次「等待重试（另一个执行正在进行）」，
状态与实际执行对不上。根因是 _next() 拉起的 worker 没登记进 _LIVE，
20 秒宽限一过就被判成僵尸 → 回收 → 重起 → 撞锁 → 无限空转。

本测试直接调用真实函数（不 mock），覆盖：
  ① 同一 tid 多个线程：只摘掉自己，不误删别的
  ② _run_alive 对「登记过且还活着」的 worker 返回 True（即使 started_at 很久以前）
  ③ 真僵尸（无登记 + 很久以前）仍返回 False，保证不会永远卡住队列
  ④ 别的 tid 还有活 worker 时不误判当前任务死亡
  ⑤ 终止意图集合 _cancel_tids 与重试上限常量存在

跑法：python -m backend._feature_tests.test_iter_live
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import iter_runner as ir    # noqa: E402

_fail = []


def ck(name, cond, detail=""):
    if cond:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        _fail.append(name)


def _spawn(hold):
    """起一个线程，停在 hold 上直到被 set。返回 (thread, release_fn)。"""
    ev = threading.Event()

    def _body():
        ev.wait(5)

    th = threading.Thread(target=_body, daemon=True)
    th.start()
    return th, ev.set


class _NoOtherWorkers:
    """临时让 `_live_any()` 返回 False，模拟「本进程只有本用例这几个线程」。

    ⚠️ 2026-11-23 修（实测：quick 连跑第一轮过、第二轮挂）：
    `_live_any()` 是**全局**判断 —— 只要进程里还有任何 iter worker 在跑就返回 True。
    而回归是同进程连跑的，别处（别的 feature 用例、真实任务）随时可能有活 worker，
    于是「无活 worker 时才成立」的那几条断言会假失败。
    全局状态不是本用例的私有财产，这里只在断言期间屏蔽外部干扰，被测逻辑一行不动。
    """

    def __enter__(self):
        self._old = ir._live_any
        ir._live_any = lambda: False
        return self

    def __exit__(self, *exc):
        ir._live_any = self._old
        return False


def main():
    tid = "t_test_live_1"
    ir._live_drop(tid)

    print("[1] 同一 tid 两个线程：摘掉一个，另一个仍算活")
    th1, rel1 = _spawn(None)
    th2, rel2 = _spawn(None)
    ir._live_add(tid, th1)
    ir._live_add(tid, th2)
    ck("两个都登记后 tid 存活", ir._live_alive(tid) is True)
    ir._live_drop(tid, th1)
    ck("摘掉 th1 后 tid 仍存活（关键：不能整条清空）", ir._live_alive(tid) is True)
    ck("全局仍有活 worker", ir._live_any() is True)

    print("[2] _run_alive：登记过且活着 → True（即使 started_at 是 1 小时前）")
    run_old = {"tid": tid, "started_at": time.time() - 3600}
    ck("旧代码这里会返回 False，现在必须 True", ir._run_alive(run_old) is True)

    print("[3] 别的 tid 有活 worker 时，未登记的 running 不误判死亡")
    ir._live_drop(tid, th2)
    ck("本 tid 已无登记", ir._live_alive(tid) is False)
    th3, rel3 = _spawn(None)
    ir._live_add("t_test_other", th3)
    ck("别的 tid 活着 → _run_alive 判活（避免重复起 worker 撞锁）",
       ir._run_alive({"tid": "t_test_none",
                      "started_at": ir._BOOT_AT + 0.5}) is True)
    ir._live_drop("t_test_other", th3)

    print("[4] 真僵尸仍要被识别（否则队列永远卡住）")
    # ⚠️ 这两条只在「本进程一个活 worker 都没有」时才成立，必须屏蔽外部干扰，
    #   否则同进程连跑回归时会假失败（实测 ROUND2 挂在这里）。
    with _NoOtherWorkers():
        ck("无任何活 worker + 很久以前 → False",
           ir._run_alive({"tid": "t_test_none", "started_at": time.time() - 3600}) is False)
        ck("跨进程孤儿（started_at 早于本进程启动）→ False",
           ir._run_alive({"tid": "t_test_none", "started_at": ir._BOOT_AT - 100}) is False)
    # ⚠️ 2026-11-23 修（实测：quick 连跑第 3 轮必挂，第 1、2 轮却过）：
    #   这里原来拿 `ir._BOOT_AT + 0.5` 当「刚提交」的时刻 —— 那是**进程启动**
    #   时刻，不是提交时刻。进程一旦活过 20 秒，宽限判断
    #   `(now - started_at) < 20` 必然为 False，这条断言就挂了。
    #   看起来像「宽限逻辑坏了」，其实只是断言写错了时间基准。
    #   「刚提交」就该用**现在**：now - now ≈ 0，稳稳落在 20 秒宽限内。
    with _NoOtherWorkers():
        ck("刚提交（未登记完的窗口）给宽限 → True",
           ir._run_alive({"tid": "t_test_none",
                          "started_at": time.time()}) is True)
    ck("空 running → False", ir._run_alive(None) is False)

    print("[5] 终止意图与重试上限")
    ck("_cancel_tids 存在且是 set", isinstance(ir._cancel_tids, set))
    ck("_MAX_LOCK_RETRY 有上限且 >0", int(ir._MAX_LOCK_RETRY) > 0)
    ck("_LOCK_RETRY_DELAY 仍是数字", isinstance(ir._LOCK_RETRY_DELAY, int))

    print("[6] 线程全部退出后登记应自然清空")
    rel1(); rel2(); rel3()
    time.sleep(0.3)
    ir._live_drop(tid)
    ir._live_drop("t_test_other")
    ck("本用例 tid 已无活线程", ir._live_alive(tid) is False)
    ck("另一个测试 tid 也已无活线程", ir._live_alive("t_test_other") is False)
    # ★ 2026-11-23 修：原来断言的是**全局** `_live_any() is False` —— 那要求
    #   「整个进程没有任何 iter worker 在跑」，同进程连跑回归时别处随时可能有，
    #   于是第一轮过、第二轮挂（实测 quick 连跑 ROUND2 failed）。
    #   改成查登记表里本用例的两个 tid 是否真的清掉了：意图（防登记泄漏）不变，
    #   而且比看线程死活更严 —— 线程退了但条目残留也能抓到。
    with ir._LIVE_LOCK:
        left = [k for k in (tid, "t_test_other") if k in ir._LIVE]
    ck("登记表里本用例的 tid 已彻底清空", not left, "残留=%r" % (left,))

    print("")
    if _fail:
        print("结论：未通过\n失败 %d 项：%s" % (len(_fail), "、".join(_fail)))
        return 1
    print("结论：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
