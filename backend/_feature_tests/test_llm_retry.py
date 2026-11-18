# -*- coding: utf-8 -*-
"""chat_stream 的重试行为（2026-11-18）。

背景：2026-11-18 12:39 的定时体验评估报「反思没跑成」，原文是
`ConnectTimeoutError(api.deepseek.com:443) connect timeout=10`。
查下去发现 **chat_stream 一次都不重试** —— 一次 RequestException 就直接抛，
而带工具的 chat_stream_with_tools 早就有 3 次重试。纯文本这条纯属漏了。

这个文件钉住三条路径，避免以后又被改回去：
  1. 三次全失败 → 尝试 3 次，抛 DeepSeekError(code="timeout")
  2. 前两次失败、第三次成功 → 尝试 3 次，正常拿到内容
  3. 第一次 550（EdgeOne CDN）、第二次成功 → 尝试 2 次，正常拿到内容
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from backend import deepseek_client as dc  # noqa: E402

_ok = 0
_fail = 0


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


class FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"choices":[{"delta":{"content":"OK"}}]}'
        yield "data: [DONE]"

    def close(self):
        pass


def drive(script):
    """跑一次 chat_stream，返回 (attempts, text, exc)。"""
    state = {"n": 0}

    class S:
        def post(self, *a, **k):
            i = state["n"]
            state["n"] += 1
            return script(i)

    dc._session = lambda: S()
    got = []
    exc = None
    try:
        for kind, text in dc.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                model="deepseek-flash", api_key="dummy"):
            if kind == "content":
                got.append(text)
    except Exception as e:  # noqa: BLE001
        exc = e
    return state["n"], "".join(got), exc


def boom(i):
    raise requests.ConnectTimeout("connect timeout=10")


def fail_fail_ok(i):
    if i < 2:
        raise requests.ConnectionError("reset")
    return FakeResp(200)


def cdn_then_ok(i):
    return FakeResp(550) if i == 0 else FakeResp(200)


print("[1] 三次全失败：必须试满 3 次再抛")
n, text, exc = drive(boom)
check("尝试 3 次（原来只 1 次）", n == 3, "attempts=%d" % n)
check("抛 DeepSeekError", isinstance(exc, dc.DeepSeekError), type(exc).__name__)
check("错误码归类为 timeout", getattr(exc, "code", "") == "timeout",
      getattr(exc, "code", ""))
check("没有静默返回内容", text == "", repr(text))

print("[2] 前两次失败、第三次成功：必须救回来")
n, text, exc = drive(fail_fail_ok)
check("尝试 3 次", n == 3, "attempts=%d" % n)
check("拿到内容", text == "OK", repr(text))
check("没有抛异常", exc is None, type(exc).__name__ if exc else "-")

print("[3] 550（EdgeOne CDN）换连接：第二次成功")
n, text, exc = drive(cdn_then_ok)
check("尝试 2 次", n == 2, "attempts=%d" % n)
check("拿到内容", text == "OK", repr(text))
check("没有抛异常", exc is None, type(exc).__name__ if exc else "-")

print("[4] 第一次就成功：不该多余重试")
n, text, exc = drive(lambda i: FakeResp(200))
check("只尝试 1 次", n == 1, "attempts=%d" % n)
check("拿到内容", text == "OK", repr(text))

print("[5] 两条路径的重试次数必须一致")
import inspect  # noqa: E402

src_stream = inspect.getsource(dc.chat_stream)
src_tools = inspect.getsource(dc.chat_stream_with_tools)
check("chat_stream 有 for _attempt in range(3)",
      "for _attempt in range(3)" in src_stream)
check("chat_stream_with_tools 也是 range(3)",
      "for _attempt in range(3)" in src_tools)
check("chat_stream 也处理了 550",
      "resp.status_code == 550" in src_stream)

print()
print("%d 项，失败 %d" % (_ok + _fail, _fail))
sys.exit(1 if _fail else 0)
