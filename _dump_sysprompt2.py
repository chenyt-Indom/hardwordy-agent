# -*- coding: utf-8 -*-
"""导出**真实生效**的系统提示词全文。

上一版 _dump_sysprompt.py 只调了 _SystemPrompt.build()，
漏掉了主通道在 build 之后追加的两段：
  · _ask_rules(cfg)      → 「信息不足就先问 / 深度询问」整段规则
  · 短确认规则 / 历史摘要 → 视情况
本脚本按 backend/main.py 里 /api/chat 的真实顺序拼一遍，落盘成 _sysprompt_full.txt。
"""
import sys, os, io, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import _SystemPrompt, _ask_rules  # noqa: E402
from backend import config  # noqa: E402

cfg = config.load_config()

parts = []

# 1) 主体
base = _SystemPrompt.build("你好")
if isinstance(base, (list, tuple)):
    base = "\n".join(str(x) for x in base)
base = str(base)
parts.append(("_SystemPrompt.build()", base))

# 2) 询问规则（主通道在 build 之后 += ）
try:
    ar = _ask_rules(cfg)
except Exception as e:
    ar = "[_ask_rules 构建失败] %s: %s" % (type(e).__name__, e)
parts.append(("_ask_rules(cfg)", str(ar)))

full = "\n\n".join(p[1] for p in parts)

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sysprompt_full.txt")
with io.open(path, "w", encoding="utf-8") as f:
    f.write(full)

print("已写入：", path)
print("总长度：%d 字符" % len(full))
for name, t in parts:
    print("  · %-24s %6d 字符" % (name, len(t)))
print("---- 段落标题（【...】开头）----")
for line in full.splitlines():
    if line.strip().startswith("【"):
        print("   ", line.strip()[:60])
