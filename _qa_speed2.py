# -*- coding: utf-8 -*-
"""量流式输出速度（token/s）和一次带工具的完整任务耗时。

TTFT 已经排除（全量/精简/无工具都在 0.8 秒左右），
剩下的可能是：① 输出速度慢；② 工具轮次多、每轮都要重新等。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backend.main as M  # noqa
import backend.config as C  # noqa
import backend.deepseek_client as DS  # noqa

cfg = C.load_config()
key = M._ds_key()
model = str(cfg.get("model") or "deepseek-flash")
sysp = M._SystemPrompt.build("hive")
tools = M._hive_tool_schemas(cfg)

# ---- ① 纯输出速度：让它写一段较长的文字，量字符/秒 ----
Q1 = [{"role": "user", "content": "用大约 400 字介绍冒泡排序的原理、复杂度和适用场景。"}]
t0 = time.time()
ttft = None
n = 0
think = 0
for kind, val in DS.chat_stream_with_tools(
        Q1, model, key, tools, lambda x, a: "ok", max_rounds=1,
        temperature=0.7, max_tokens=1200, thinking=None, system_prompt=sysp):
    if ttft is None and kind in ("content", "thinking") and val:
        ttft = time.time() - t0
    if kind == "content":
        n += len(val or "")
    elif kind == "thinking":
        think += len(val or "")
tot = time.time() - t0
print("① 纯输出: 正文 %d 字, 思考 %d 字, TTFT %.2fs, 总 %.2fs"
      % (n, think, ttft or -1, tot))
if tot > 0 and n:
    print("   正文速度 %.1f 字/秒（≈%.1f token/s）"
          % (n / max(0.01, tot - (ttft or 0)), n * 0.75 / max(0.01, tot - (ttft or 0))))

# ---- ② 带工具的一轮：数轮次、量每轮间隔 ----
Q2 = [{"role": "user", "content": "看看工作区里现在有哪些项目，用一句话告诉我。"}]
rounds = []


def ex(name, args):
    rounds.append((name, time.time()))
    try:
        import backend.workspace as W
        if name == "workspace_projects":
            return str(W.list_projects())
    except Exception as e:
        return "err:%s" % e
    return "ok"


t0 = time.time()
first = None
for kind, val in DS.chat_stream_with_tools(
        Q2, model, key, tools, ex, max_rounds=6,
        temperature=0.7, max_tokens=800, thinking=None, system_prompt=sysp):
    if first is None and kind in ("content", "thinking"):
        first = time.time() - t0
tot2 = time.time() - t0
print("② 带工具: 调用 %d 次 %s, 首字 %.2fs, 总 %.2fs"
      % (len(rounds), [r[0] for r in rounds], first or -1, tot2))
