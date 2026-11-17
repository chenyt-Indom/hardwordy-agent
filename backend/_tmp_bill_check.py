# -*- coding: utf-8 -*-
"""账单速览卡的手工体检脚本（不改数据、不联网、不调模型）。

用法：
    py backend/_tmp_bill_check.py

为什么留这个文件
----------------
「账单速览卡」的数据链路是 usage_ledger.summarize（纯本地聚合）→
/api/usage/summary → livecard.renderBillCard。断言式的回归在
backend/_feature_tests/ 里（护栏目录，只能由系统生成）；这里放的是一份
**能手动跑一遍、直接看到数字**的体检脚本：构造两天的假账本，验证
total_calls / total_tokens / by_tag / by_hour / delta_vs_prev / weighted_cost
的口径，(今日-昨日)/昨日 这类容易算错的地方一眼能看出来。

脚本只用临时目录，跑完自己删干净；真实账本（data/llm_usage）一个字节都不碰。
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile

_BACKEND = os.path.dirname(os.path.abspath(__file__))

# 昨天的 2 次调用 + 今天的 3 次调用 → delta_vs_prev 必须等于 (3-2)/2 = 0.5
_YESTERDAY = [
    {"ts": 1763300000, "at": "09:10:00", "tag": "chat", "p": 100, "c": 50, "t": 150},
    {"ts": 1763300100, "at": "09:11:00", "tag": "iter", "p": 200, "c": 100, "t": 300},
]
_TODAY = [
    {"ts": 1763380000, "at": "10:05:00", "tag": "chat", "p": 1000, "c": 400, "t": 1400},
    {"ts": 1763380100, "at": "10:06:00", "tag": "chat", "p": 500, "c": 500, "t": 1000},
    {"ts": 1763380200, "at": "10:07:00", "tag": "img", "p": 300, "c": 200, "t": 500},
]


def _load():
    spec = importlib.util.spec_from_file_location(
        "ul_probe", os.path.join(_BACKEND, "usage_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_day(folder, name, records):
    path = os.path.join(folder, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ul = _load()
    folder = tempfile.mkdtemp(prefix="bill_probe_")
    original = ul._dir
    try:
        _write_day(folder, "2026-11-16", _YESTERDAY)
        _write_day(folder, "2026-11-17", _TODAY)
        ul._dir = lambda: pathlib.Path(folder)
        got = ul.summarize(2)
        print(json.dumps(got, ensure_ascii=False, indent=1))
        assert got["total_calls"] == 5, "调用次数不对：%s" % got["total_calls"]
        assert got["total_tokens"] == 3350, "token 合计不对：%s" % got["total_tokens"]
        assert abs(got["delta_vs_prev"] - 0.5) < 1e-6, \
            "delta_vs_prev 不等于 (今日-昨日)/昨日：%s" % got["delta_vs_prev"]
        assert sum(got["by_tag"].values()) == got["total_calls"], "by_tag 合计对不上"
        assert sum(got["by_hour"].values()) == got["total_calls"], "by_hour 合计对不上"
        assert got["weighted_cost"] > 0, "加权成本没算出来"

        # 空账本必须降级成 0，而不是抛异常（记账是旁路，读账也得是旁路）
        empty = tempfile.mkdtemp(prefix="bill_probe_empty_")
        try:
            ul._dir = lambda: pathlib.Path(empty)
            zero = ul.summarize(2)
            assert zero["total_calls"] == 0 and zero["delta_vs_prev"] == 0, \
                "空账本没降级成 0：%s" % zero
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        ul._dir = original
        shutil.rmtree(folder, ignore_errors=True)
    print("结论行：", got["headline"], "｜加权成本：¥%.4f" % got["weighted_cost"])
    print("BILL CARD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
