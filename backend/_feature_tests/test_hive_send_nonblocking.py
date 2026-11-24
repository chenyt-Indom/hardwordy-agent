"""群聊「发消息卡 / 按终止没反应」的回归测试。

用户报的现象（2026-11-25）：
  · 群聊里给 LLM 发消息会卡；
  · 卡住以后按「■ 终止」也没有反应。

真根因（读代码 + 实测确认，不是猜）：
  1. `hive_relay_link._send_pkt()` 用 `sock.sendall()`，而中继 socket 在握手后
     是 `settimeout(None)` —— 对端不读时会**无限**阻塞；
  2. `RelayLink.send()` 是**持着 `_send_lock`** 调的，于是取消、心跳、后面
     所有帧全排在锁上；
  3. `_hd_emit()` 在模型的流式读取循环里**同步**调 `send_stream()` ——
     中继一慢，模型吐字就被网络拖住，界面看着就是卡住；
  4. 取消接口里 `send_cancel()` 也是同步的，取消请求自己先卡住；
  5. 取消后要等 worker 线程自己发现，才推 done 帧。

本测试覆盖 1~5 的修法，全部是**真跑**（socketpair 实测超时），
只有 3/4/5 是源码断言（它们涉及 HTTP 路由，起服务代价太大）。
"""
from __future__ import annotations

import ast
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backend.hive_relay_link as R          # noqa: E402


# ---------------------------------------------------------------- 真跑：发送超时

def test_send_pkt_normal_still_works():
    """加了超时之后，正常发送必须照旧成功。"""
    a, b = socket.socketpair()
    try:
        ok = R._send_pkt(a, 0, b"hello" * 100, timeout=2.0)
        assert ok is True, "正常发送应当成功"
        got = b.recv(65536)
        # 收到的字节 = 包头 + payload（包头长度由 HDR 格式决定，不写死）
        assert len(got) > 500 and got.endswith(b"hello" * 100), \
            "对端应当完整收到 payload，实测 %d 字节" % len(got)
    finally:
        a.close()
        b.close()


def test_send_pkt_times_out_instead_of_hanging():
    """对端不读、缓冲塞满 → 必须在超时后返回 False（原来会无限卡）。"""
    a, b = socket.socketpair()
    try:
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        payload = b"x" * (2 * 1024 * 1024)
        t0 = time.time()
        ok = R._send_pkt(a, 0, payload, timeout=0.6)
        dt = time.time() - t0
        assert ok is False, "塞满时应当放弃，而不是等下去"
        assert dt < 1.5, "应当在超时附近返回，实测 %.2fs" % dt
    finally:
        a.close()
        b.close()


def test_relay_send_lock_has_timeout():
    """另一个线程卡在慢发送上时，send() 不能陪着一起卡死。"""
    lk = R.RelayLink()
    a, b = socket.socketpair()
    old_key = R.H.group_key
    try:
        lk.sock = a
        lk.state = "online"
        lk.gid = "g_test"
        lk._all_direct = lambda: False
        R.H.group_key = lambda gid: b"0" * 32       # 只测锁，不测加密

        def hold():
            lk._send_lock.acquire()
            time.sleep(2.0)
            lk._send_lock.release()

        threading.Thread(target=hold, daemon=True).start()
        time.sleep(0.1)
        t0 = time.time()
        ok = lk.send(1, {"a": 1})
        dt = time.time() - t0
        assert ok is False, "锁被占时应当放弃这一帧"
        assert dt < 1.2, "应当在锁超时附近返回，实测 %.2fs" % dt
        assert lk.dropped >= 1

        time.sleep(2.0)          # 等锁释放
        assert lk.send(1, {"a": 2}) is True, "锁释放后应当恢复发送"
    finally:
        R.H.group_key = old_key
        a.close()
        b.close()


# ---------------------------------------------------------------- 源码断言

def _func_src(path: str, name: str) -> str:
    src = open(path, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("找不到函数 %s" % name)


MAIN = os.path.join(ROOT, "backend", "main.py")


def test_hd_emit_does_not_send_synchronously():
    """_hd_emit 里不能直接调 send_stream —— 那会把流式读取卡在网络发送上。"""
    body = _func_src(MAIN, "_hd_emit")
    assert "send_stream(" not in body, "_hd_emit 又同步发网络了"
    assert "_hd_net_send(" in body, "_hd_emit 应当走后台队列"


def test_cancel_emits_done_immediately():
    """取消接口要立刻推 done，不能等 worker 线程自己发现。"""
    body = _func_src(MAIN, "hive_hd_cancel")
    assert '_hd_emit(gid, mid, "done"' in body, "取消后应当立刻推 done 帧"


def test_cancel_broadcast_is_async():
    """取消的跨机广播不能在请求线程里同步做。"""
    body = _func_src(MAIN, "hive_hd_cancel")
    assert "threading.Thread(" in body, "send_cancel 应当放后台线程"


def test_exec_tool_checks_cancel():
    """点了终止之后，不要再启动新的工具调用。"""
    src = open(MAIN, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_hive_run_model_now":
            inner = [n for n in ast.walk(node)
                     if isinstance(n, ast.FunctionDef) and n.name == "_exec_tool"]
            assert inner, "找不到内层 _exec_tool"
            body = ast.get_source_segment(src, inner[0]) or ""
            assert "ev.is_set()" in body, "_exec_tool 应当在执行前检查取消"
            return
    raise AssertionError("找不到 _hive_run_model_now")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except AssertionError as e:
            bad += 1
            print("FAIL", name, "->", e)
        except Exception as e:                  # noqa: BLE001
            bad += 1
            print("ERROR", name, "->", type(e).__name__, e)
    print("通过 %d / 失败 %d（共 %d）" % (len(tests) - bad, bad, len(tests)))
    sys.exit(1 if bad else 0)
