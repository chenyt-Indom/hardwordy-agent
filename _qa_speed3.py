# -*- coding: utf-8 -*-
"""对照实验：工具数量 → 工具轮次 / 耗时。

同一个问题，分别给「旧群聊的 12 个工具」和「现在的 59 个工具」，
用**真实的 tools.dispatch** 执行，量轮次和总耗时。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backend.main as M  # noqa
import backend.config as C  # noqa
import backend.deepseek_client as DS  # noqa
import backend.tools as T  # noqa

cfg = C.load_config()
key = M._ds_key()
model = str(cfg.get("model") or "deepseek-flash")
sysp = M._SystemPrompt.build("hive")
full = M._hive_tool_schemas(cfg)


def is_selfdev(s):
    n = str((s.get("function") or {}).get("name") or "")
    return n.startswith("selfdev_") or n.startswith("workspace_")


old12 = [s for s in full if is_selfdev(s)]
print("旧群聊工具数 %d / 新工具数 %d" % (len(old12), len(full)))

Q = [{"role": "user", "content": "看看工作区里现在有哪些项目，用一句话告诉我。"}]


def run(label, tools):
    calls = []

    def ex(name, args):
        calls.append(name)
        ctx = {"images": [], "last_image": None, "mask": None, "shown_images": [],
               "ask_mode": "quick", "session": "qa:speed", "user_text": "qa",
               "image_key": "", "image_model": "", "ds_key": ""}
        try:
            out = T.dispatch(name, args if isinstance(args, dict) else {}, [], ctx)
        except Exception as e:
            return "err:%s" % e
        return str(out)[:600]

    t0 = time.time()
    text = ""
    for kind, val in DS.chat_stream_with_tools(
            Q, model, key, tools, ex, max_rounds=12,
            temperature=0.7, max_tokens=800, thinking=None, system_prompt=sysp):
        if kind == "content":
            text += val or ""
    tot = time.time() - t0
    print("%-8s 工具 %2d 个 → 调用 %d 次 %s, 总 %5.2fs"
          % (label, len(tools), len(calls), calls, tot))
    print("         回答: %s" % text.strip().replace("\n", " ")[:70])


run("旧12个", old12)
run("新59个", full)
