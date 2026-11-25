# -*- coding: utf-8 -*-
"""终止速度的真实回归：模型还在吐字时点终止，必须在 1 秒内停下。

用户 2026-11-25 报：「点击终止还无法停下，需要等好久才结束」（实测 297 秒）。
根因是终止只在「一次工具调用结束」时检查，而一次模型生成要好几分钟；
终止后还会让模型再跑一轮 finish 收尾。本文件用一个**慢速假模型**复现：
每 0.05 秒吐一块、共 100 块（≈5 秒），第 3 块之后把 stop 置 True，
断言 _run_local 在 1 秒内抛 _Cancelled 并且不再读后面的块。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import iter_agent  # noqa: E402

_OK = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _FAIL += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


class _Ctx(object):
    def __init__(self, stop_after=3):
        self.stopped = False
        self.finished = False
        self.n_calls = 0
        self.steps = 20
        self.emitted = []
        self._stop_after = stop_after
        self._chunks = 0

    def stop(self):
        self._chunks += 1
        return self._chunks > self._stop_after

    def over(self):
        return ""

    def emit(self, text, kind="step"):
        self.emitted.append((kind, text))

    def record(self, *a, **k):
        pass


class _FakeResp(object):
    def __init__(self, n=100, delay=0.05):
        self.n = n
        self.delay = delay
        self.closed = False
        self.read = 0

    def iter_lines(self):
        import json
        for i in range(self.n):
            if self.closed:
                return
            time.sleep(self.delay)
            self.read += 1
            yield json.dumps({"message": {"thinking": "想第 %d 步" % i}}).encode("utf-8")

    def close(self):
        self.closed = True


def main():
    resp = _FakeResp()

    def _mk_client(r):
        class _Client(object):
            def chat(self, *a, **k):
                return r
        return _Client()

    # _run_local 里是 `from . import main as _main`，直接替换模块属性即可
    import backend.main as _real_main
    _old_client = getattr(_real_main, "client", None)
    _real_main.client = _mk_client(resp)
    try:
        ctx = _Ctx(stop_after=3)
        t0 = time.time()
        raised = False
        try:
            iter_agent._run_local(ctx, "sys", "task", 20, "fake-model", on_note=None)
        except iter_agent._Cancelled:
            raised = True
        cost = time.time() - t0
        check("生成中命中终止 → 抛 _Cancelled", raised)
        check("1 秒内就停下（不再等整段生成）", cost < 1.0, "实测 %.2fs" % cost)
        check("ctx.stopped 置位", ctx.stopped is True)
        check("只读了少数几块就掐断", resp.read <= 8, "读了 %d 块" % resp.read)
        check("连接被关闭", resp.closed is True)
    finally:
        _real_main.client = _old_client

    # 对照：不终止时应把整段读完（证明上面的快不是因为假模型本来就快）
    resp2 = _FakeResp(n=10, delay=0.01)
    _real_main.client = _mk_client(resp2)
    try:
        ctx2 = _Ctx(stop_after=10 ** 6)
        t0 = time.time()
        iter_agent._run_local(ctx2, "sys", "task", 20, "fake-model", on_note=None)
        cost2 = time.time() - t0
        check("不终止时正常读完整段（没被提前掐断）",
              resp2.read >= 10 and resp2.closed is False,
              "读了 %d 块 / %.2fs / closed=%s" % (resp2.read, cost2, resp2.closed))
        check("不终止时不抛异常", ctx2.stopped is False)
    finally:
        _real_main.client = _old_client

    print("\n结果：通过 %d / 失败 %d" % (_OK, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
