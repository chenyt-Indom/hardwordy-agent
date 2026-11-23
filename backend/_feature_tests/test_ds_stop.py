"""验证「云端通道终止」真的能立刻停下（2026-11-23）。

背景：用户报「点了终止没有立刻停」。查日志发现真实点击一次都没生效 ——
`/api/chat/deepseek` 完全没接终止令牌，只有本地 `/api/chat` 接了。

本测试用假的上游流（无限吐 chunk）验证三件事：
  1. 令牌一置位，生成器在**一个 chunk 内**结束（不跑满）；
  2. 上游连接被 close（云端不再计费）；
  3. 令牌一开始就是置位的 → **一次请求都不发**。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import deepseek_client as dc   # noqa: E402


class FakeResp:
    def __init__(self, total=100000):
        self.status_code = 200
        self.text = ""
        self.closed = False
        self.sent = 0
        self.total = total

    def iter_lines(self, decode_unicode=True):
        for _ in range(self.total):
            time.sleep(0.01)          # 模拟真实网络：每片 10ms
            if self.closed:
                return
            self.sent += 1
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return self.resp


def run_case(stop_now: bool, set_after: float = 0.5):
    resp = FakeResp()
    sess = FakeSession(resp)
    dc._session = lambda: sess          # 屏蔽真实网络
    ev = threading.Event()
    if stop_now:
        ev.set()
    else:
        threading.Timer(set_after, ev.set).start()
    t0 = time.time()
    chunks, notes = [], []
    for kind, val in dc.chat_stream_with_tools(
            [{"role": "user", "content": "hi"}], "deepseek-flash", "fake-key",
            [], lambda n, a: "ok", max_rounds=5, should_stop=ev.is_set):
        if kind == "content":
            chunks.append(val)
        elif kind == "note":
            notes.append(val)
    return time.time() - t0, len(chunks), notes, resp, sess


def main():
    ok = True

    # —— 场景 1：跑到一半点终止 ——
    dt, n, notes, resp, sess = run_case(False, 0.5)
    print("[场景1] 0.5s 时置位令牌 → 总耗时 %.2fs，收到 %d 片，notes=%r，上游已关=%s"
          % (dt, n, notes, resp.closed))
    if dt > 1.5:
        print("  [失败] 终止没生效：应该 <1.5s 结束，实际 %.2fs" % dt)
        ok = False
    elif not resp.closed:
        print("  [失败] 上游连接没关，云端还在计费")
        ok = False
    elif n > 200:
        print("  [失败] 令牌置位后还在继续收片（%d 片）" % n)
        ok = False
    else:
        print("  [通过] 一个 chunk 内就停了，上游连接已关闭")

    # —— 场景 2：一开始就是终止状态 ——
    dt, n, notes, resp, sess = run_case(True)
    print("[场景2] 开跑前就置位 → 总耗时 %.2fs，请求次数 %d，收到 %d 片，notes=%r"
          % (dt, sess.calls, n, notes))
    if sess.calls != 0:
        print("  [失败] 已终止却还发了 %d 次请求 —— 就是「点了停还继续烧钱」" % sess.calls)
        ok = False
    elif n != 0:
        print("  [失败] 已终止却还吐了内容")
        ok = False
    else:
        print("  [通过] 一次请求都没发，直接收场")

    # —— 场景 3：不终止时行为不变（回归） ——
    resp = FakeResp(total=30)
    sess = FakeSession(resp)
    dc._session = lambda: sess
    ev = threading.Event()          # 永不置位
    t0 = time.time()
    got = []
    for kind, val in dc.chat_stream_with_tools(
            [{"role": "user", "content": "hi"}], "deepseek-flash", "fake-key",
            [], lambda n, a: "ok", max_rounds=2, should_stop=ev.is_set):
        if kind == "content":
            got.append(val)
    print("[场景3] 不终止 → 收到 %d 片，请求 %d 次，耗时 %.2fs"
          % (len(got), sess.calls, time.time() - t0))
    if len(got) < 20:
        print("  [失败] 正常流程被改坏了：内容少了")
        ok = False
    else:
        print("  [通过] 正常流程未受影响")

    print("\n结论：", "通过" if ok else "未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
