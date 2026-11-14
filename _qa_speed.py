# -*- coding: utf-8 -*-
"""量化群聊每一轮的输入规模：系统提示 / 工具表 / 历史，找出"慢"的大头。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backend.tools as tools  # noqa
from backend import main as M  # noqa


def tok(s):
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，取折中。"""
    n = len(s or "")
    return int(n * 0.75)


cfg = M.load_cfg() if hasattr(M, "load_cfg") else {}
if not isinstance(cfg, dict):
    cfg = {}

# ① 系统提示
sysp = ""
try:
    sysp = M._SystemPrompt.build("hive") if hasattr(M, "_SystemPrompt") else ""
except Exception as e:
    sysp = ""
    print("系统提示构建失败:", e)
print("① 系统提示: %d 字 ≈ %d token" % (len(sysp), tok(sysp)))

# ② 工具表
sch = M._hive_tool_schemas(cfg)
sj = json.dumps(sch, ensure_ascii=False)
print("② 工具表: %d 个工具, JSON %d 字符 ≈ %d token" % (len(sch), len(sj), tok(sj)))
names = [str((s.get("function") or {}).get("name") or "") for s in sch]
print("   前 10 个:", names[:10])

# ③ 历史：扫所有群，取最大的一个
import backend.hive as hive  # noqa

groups = []
try:
    groups = hive.list_groups() or []
except Exception as e:
    print("列群失败:", e)
worst = None
for g in groups:
    gid = g.get("gid") or ""
    try:
        hist = hive.load_messages(gid) or []
    except Exception:
        hist = []
    turns = M._hive_history_turns(hist, 40)
    total = sum(len(t[1]) for t in turns)
    if worst is None or total > worst[1]:
        worst = (g.get("name") or gid, total, len(hist), len(turns))
if worst:
    print("③ 最大的群「%s」: 磁盘 %d 条, 注入 %d 轮, 共 %d 字 ≈ %d token"
          % (worst[0], worst[2], worst[3], worst[1], tok("x" * worst[1])))
else:
    print("③ 没有群")

# ④ 合计
hist_chars = worst[1] if worst else 0
tot_chars = len(sysp) + len(sj) + hist_chars
print("④ 单轮输入合计: %d 字 ≈ %d token" % (tot_chars, tok("x" * tot_chars)))
print("   其中 历史占 %.0f%%, 工具占 %.0f%%, 系统提示占 %.0f%%"
      % (100.0 * hist_chars / max(1, tot_chars),
         100.0 * len(sj) / max(1, tot_chars),
         100.0 * len(sysp) / max(1, tot_chars)))
