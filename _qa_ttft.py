# -*- coding: utf-8 -*-
"""量真实首 token 延迟（TTFT）：全量工具 vs 精简工具 vs 无工具。

目的：确认群聊「响应太慢」到底是不是工具表 3.5 万 token 造成的。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backend.main as M  # noqa
import backend.config as C  # noqa
import backend.deepseek_client as DS  # noqa


def tok(chars):
    return int((chars or 0) * 0.75)


cfg = M.load_cfg()
key = M._ds_key()
model = str(cfg.get("model") or "deepseek-flash")
print("模型: %s  key: %s" % (model, "有" if key else "无"))
if not key:
    print("没有 key，跳过")
    sys.exit(0)

sysp = M._SystemPrompt.build("hive")
full_tools = M._hive_tool_schemas(cfg)
# 精简集：和单人 lean 一样砍掉「地图/工作区/天气/github」
_drop = {"map_plan", "nearby_places", "connect_amap", "get_weather", "github_push"}
lean_tools = [s for s in full_tools
              if (s.get("function") or {}).get("name") not in _drop
              and not str((s.get("function") or {}).get("name") or "").startswith("workspace_")]
print("全量 %d 个 / %d token   精简 %d 个 / %d token"
      % (len(full_tools), tok(len(json.dumps(full_tools, ensure_ascii=False))),
         len(lean_tools), tok(len(json.dumps(lean_tools, ensure_ascii=False)))))
print("系统提示 %d token\n" % tok(len(sysp)))

Q = [{"role": "user", "content": "用一句话说明什么是冒泡排序。"}]


def run(label, tools):
    t0 = time.time()
    ttft = None
    first_kind = ""
    text = ""
    try:
        for kind, val in DS.chat_stream_with_tools(
                Q, model, key, tools, lambda n, a: "ok",
                max_rounds=1, temperature=0.7, max_tokens=256,
                thinking=False, system_prompt=sysp):
            if ttft is None and kind in ("content", "thinking") and val:
                ttft = time.time() - t0
                first_kind = kind
            if kind == "content":
                text += val
            if ttft is not None and len(text) > 20:
                break
    except Exception as e:
        print("%-10s 失败: %s" % (label, e))
        return
    print("%-10s TTFT %5.2fs  (%s)  首段: %s"
          % (label, ttft or -1, first_kind, text[:40].replace("\n", " ")))


run("无工具", [])
run("精简集", lean_tools)
run("全量52", full_tools)
