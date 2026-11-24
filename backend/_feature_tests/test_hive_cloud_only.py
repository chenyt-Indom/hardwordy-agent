"""群聊「一律走云端 LLM」的回归测试。

用户报的现象（2026-11-25）：
  · 群聊里给 LLM 发消息会卡；
  · 卡住以后按「■ 终止」也没有反应。

真根因之一（读代码 + 读本机 config.json 确认，不是猜）：
  `_hive_run_model_now()` 里只有一句
      model = cfg.get("default_model") or ""
  而本机 `default_model = "qwen3-vl:8b"` —— **本地 Ollama**。
  于是群里每一条 @hd 都跑在本地 8B 上；再叠加 `num_ctx_override=362496`
  （35 万上下文）和 12GB 显存，大部分层落到 CPU：首字几十秒、一轮几分钟。
  单人模式走云端 DeepSeek（首字 1~2 秒），所以只有群聊特别慢。

现在的规则（`_hive_pick_model`）：
  hive_model（显式，可填本地） > 云端默认（hive_cloud_model / code_model_ds /
  deepseek-flash，前提是有 Key） > default_model（没 Key 才退，且群里明说）。

本测试**不 import backend.main**（969KB，导入代价大且会拉起一堆初始化）：
直接用 ast 把 `_hive_pick_model` 的源码抠出来 exec，喂假 cfg / 假 `_ds_key`。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAIN = os.path.join(ROOT, "backend", "main.py")


def _load_picker():
    """从 main.py 抠出 `_hive_pick_model` 并执行，返回可调用对象。"""
    src = open(MAIN, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_hive_pick_model":
            seg = ast.get_source_segment(src, node) or ""
            ns = {}
            exec(compile(seg, MAIN, "exec"), ns)      # noqa: S102 —— 只执行本仓库源码
            return ns["_hive_pick_model"]
    raise AssertionError("找不到 _hive_pick_model —— 群聊云端改造被回退了？")


def _pick(cfg, has_key=True):
    fn = _load_picker()
    fn.__globals__["_ds_key"] = lambda h="": ("sk-test" if has_key else "")
    return fn(cfg)


# ---------------------------------------------------------------- 选模型

def test_local_default_still_goes_cloud():
    """核心：default_model 是本地模型时，群聊也必须走云端。"""
    m, src = _pick({"default_model": "qwen3-vl:8b", "code_model_ds": "deepseek-flash"})
    assert m == "deepseek-flash", "群聊应当走云端，实测 %r" % (m,)
    assert src == "cloud", "来源应当是 cloud，实测 %r" % (src,)


def test_cloud_model_name_prefers_configured():
    """hive_cloud_model / code_model_ds 指定了就用它，不写死 deepseek-flash。"""
    m, _ = _pick({"default_model": "qwen3-vl:8b",
                  "hive_cloud_model": "deepseek-flash-think"})
    assert m == "deepseek-flash-think", "实测 %r" % (m,)
    m2, _ = _pick({"default_model": "qwen3-vl:8b",
                   "code_model_ds": "deepseek-flash-think"})
    assert m2 == "deepseek-flash-think", "实测 %r" % (m2,)
    m3, _ = _pick({"default_model": "qwen3-vl:8b"})
    assert m3 == "deepseek-flash", "没有云端配置时应当用 deepseek-flash，实测 %r" % (m3,)


def test_explicit_hive_model_wins():
    """hive_model 显式配置优先 —— 用户想指定本地模型也得尊重。"""
    m, src = _pick({"default_model": "qwen3-vl:8b", "hive_model": "qwen3-vl:30b"})
    assert m == "qwen3-vl:30b", "实测 %r" % (m,)
    assert src == "config:local", "实测 %r" % (src,)
    m2, src2 = _pick({"hive_model": "deepseek-flash-think"})
    assert (m2, src2) == ("deepseek-flash-think", "config:cloud"), (m2, src2)


def test_no_key_falls_back_to_local_with_marker():
    """没配 Key 才允许退回本地，并且必须带 local:no-key 标记（调用方要提示）。"""
    m, src = _pick({"default_model": "qwen3-vl:8b"}, has_key=False)
    assert m == "qwen3-vl:8b", "实测 %r" % (m,)
    assert src == "local:no-key", "实测 %r" % (src,)


def test_empty_config_does_not_crash():
    """cfg 为空 / None 时不能抛异常（否则整轮任务直接报废）。"""
    assert _pick({})[0] == "deepseek-flash"
    assert _pick(None)[0] == "deepseek-flash"


# ---------------------------------------------------------------- 接线

def test_run_model_uses_picker():
    """`_hive_run_model_now` 必须真的用了 _hive_pick_model，且不再直读 default_model。"""
    src = open(MAIN, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_hive_run_model_now":
            body = ast.get_source_segment(src, node) or ""
            assert "_hive_pick_model(cfg)" in body, "群聊没有接上云端选择器"
            assert 'model = cfg.get("default_model")' not in body, \
                "又退回直读 default_model 了（那会跑本地模型）"
            assert "local:no-key" in body, "退回本地时应当在群里明说"
            assert '"model_source": _model_src' in body, \
                "start 帧应当带模型来源，否则没法核对走没走云端"
            return
    raise AssertionError("找不到 _hive_run_model_now")


def test_config_has_hive_model_key():
    """`hive_model` 必须进 DEFAULT_CONFIG，否则用户保存设置时会被静默清掉。"""
    from backend import config
    assert "hive_model" in config.DEFAULT_CONFIG, "DEFAULT_CONFIG 里没有 hive_model"


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
