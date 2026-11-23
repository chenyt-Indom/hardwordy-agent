# -*- coding: utf-8 -*-
"""回归：safeio 安全写盘 —— 文件锁重试 / 代理字符清洗 / 降级不丢数据。

真实故障史：
  · 2026-11-24 本轮实测：`test_task_card.py` 在
    `os.replace(tmp, pending_cards.jsonl)` 抛
    `PermissionError: [WinError 5] 拒绝访问`。
    根因：Windows 上只要目标文件此刻被任何进程打开（前端轮询读卡片、
    杀毒扫描、另一线程读账本），rename 就被拒；POSIX 上这种替换是原子的、
    不会被拒 —— 所以同一份代码 Linux 全绿、Windows 偶发红，且只在并发时出现。
  · 更早那次：孤立代理字符（半个 emoji）让 json.dump 抛
    UnicodeEncodeError，被 try/except 吞成**静默丢写**。

本测试守四条：
  1. scrub_surrogates 真能把孤立代理换掉，且**不动正常字符**
  2. os.replace 偶发被拒 → 重试后**成功**（不是直接报错）
  3. os.replace 一直失败 → 降级原地覆盖，**内容仍然完整落盘**（不丢数据）
  4. 端到端：目标文件被占用时，iterloop.push_card / ack_cards 仍然成功
"""
import json
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import safeio                            # noqa: E402

# ⚠️ 孤立代理必须用 chr() 构造：字面量会让 Python 编译本文件时就抛
#    UnicodeEncodeError，测试根本跑不起来（踩过）。
_LONE_LOW = chr(0xDBC2)
BAD = "前" + _LONE_LOW + "后"

_OK = []
_BAD = []


def check(name, cond, extra=""):
    (_OK if cond else _BAD).append(name)
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  " + str(extra)) if (extra and not cond) else ""))


def test_scrub():
    """孤立代理被换掉，正常中文/emoji 一个不动。"""
    out = safeio.scrub_surrogates(BAD)
    check("清洗后不含代理字符",
          not any(0xD800 <= ord(c) <= 0xDFFF for c in out), repr(out))
    check("清洗保留前后正常字符", out.startswith("前") and out.endswith("后"), repr(out))

    good = "正常文本 😀 中文"
    check("正常 emoji 不被误伤", safeio.scrub_surrogates(good) == good)

    # 嵌套结构也要清到
    nested = {"a": [BAD], "b": {"c": _LONE_LOW}}
    out2 = safeio.scrub_surrogates(nested)
    flat = json.dumps(out2, ensure_ascii=False)
    check("嵌套结构也被清洗",
          not any(0xD800 <= ord(c) <= 0xDFFF for c in flat), flat)


def test_retry_recovers():
    """os.replace 前两次被拒 → 重试后成功，且内容正确。"""
    d = tempfile.mkdtemp(prefix="safeio_")
    p = os.path.join(d, "x.json")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "拒绝访问（模拟前端正读文件）")
        return real(a, b)

    with mock.patch("backend.safeio.os.replace", side_effect=flaky):
        how = safeio.atomic_write_json(p, {"v": 1}, indent=1)

    check("被拒后重试成功", how == "replace", how)
    check("重试次数符合预期（第 3 次成功）", calls["n"] == 3, calls["n"])
    with open(p, encoding="utf-8") as fh:
        check("重试后内容正确", json.load(fh) == {"v": 1})


def test_fallback_keeps_data():
    """os.replace 一直失败 → 降级原地覆盖，**数据不能丢**。"""
    d = tempfile.mkdtemp(prefix="safeio_")
    p = os.path.join(d, "y.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"old": true}')

    with mock.patch("backend.safeio.os.replace",
                    side_effect=PermissionError(5, "拒绝访问")):
        how = safeio.atomic_write_json(p, {"v": 2})

    check("全部失败时走降级", how == "overwrite", how)
    with open(p, encoding="utf-8") as fh:
        check("降级后内容是新数据（没丢）", json.load(fh) == {"v": 2})
    check("降级后不留 .tmp 残留", not os.path.exists(p + ".tmp"))


def test_concurrent_writers():
    """多线程并发写同一文件：不崩、最终文件仍是合法 JSON。"""
    d = tempfile.mkdtemp(prefix="safeio_")
    p = os.path.join(d, "z.json")
    errs = []

    def w(i):
        try:
            for _ in range(20):
                safeio.atomic_write_json(p, {"i": i})
        except Exception as e:                          # noqa: BLE001
            errs.append("%s: %s" % (type(e).__name__, e))

    ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    check("并发写不抛异常", not errs, errs[:3])
    ok = True
    try:
        with open(p, encoding="utf-8") as fh:
            json.load(fh)
    except Exception as e:                              # noqa: BLE001
        ok = False
        check("并发后文件仍是合法 JSON", False, "%s: %s" % (type(e).__name__, e))
    if ok:
        check("并发后文件仍是合法 JSON", True)


def test_end_to_end_cards():
    """端到端：文件被占用时 push_card / ack_cards 仍然成功。

    这是本轮真实故障的原点 —— ack_cards 抛 WinError 5，卡片永远清不掉。
    """
    from backend import iterloop

    d = tempfile.mkdtemp(prefix="safeio_")
    p = os.path.join(d, "pending_cards.jsonl")

    with mock.patch.object(iterloop, "_cards_path", return_value=p):
        r1 = iterloop.push_card({"kind": "done", "text": "测试卡"})
        check("push_card 成功", r1.get("ok") is True, r1)
        check("卡片真的落盘", len(iterloop.pending_cards()) == 1)

        # 模拟「前端正在读」+「替换被拒」同时发生
        with mock.patch("backend.safeio.os.replace",
                        side_effect=PermissionError(5, "拒绝访问")):
            r2 = iterloop.ack_cards([])
            check("文件被占用时 ack 仍成功", r2.get("ok") is True, r2)
            check("被占用时卡片仍能清空", len(iterloop.pending_cards()) == 0)


def main():
    for fn in (test_scrub, test_retry_recovers, test_fallback_keeps_data,
               test_concurrent_writers, test_end_to_end_cards):
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            import traceback
            traceback.print_exc()
            _BAD.append("%s 抛异常: %s" % (fn.__name__, e))

    print("\n通过 %d / 失败 %d" % (len(_OK), len(_BAD)))
    if _BAD:
        print("失败项：" + "；".join(_BAD))
    return 0 if not _BAD else 1


if __name__ == "__main__":
    sys.exit(main())
