# -*- coding: utf-8 -*-
"""导出**真实生效**的系统提示词全文（第三版，补齐前两版漏掉的段）。

主通道 /api/chat 里 sys_prompt 的真实拼装顺序（backend/main.py L8437 起）：
  1) _SystemPrompt.build(last_user, session, docs, no_tools, out)   L8437
  2) sys_prompt += _ask_rules(cfg)                                  L8604
  3) sys_prompt += "\n\n" + _reply_template_ctx()                   L8610
  4) sys_prompt += ( 跨轮串扰 / 做完的事不要再做一遍 )                 L8616
  5) if _needs_web_ctx(...): sys_prompt += ( 动笔前先核实 )            L8661

第 4、5 段是**内联字符串常量**，前两版脚本都没导出。本脚本用 AST 从
main.py 里**原样提取**这些常量（不手抄，避免抄错），按行号顺序拼上。
"""
import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend.main import _SystemPrompt, _ask_rules, _reply_template_ctx  # noqa: E402
from backend import config  # noqa: E402

cfg = config.load_config()

parts = []


def _as_text(v):
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x) for x in v)
    return str(v)


# 1) 主体
parts.append(("① _SystemPrompt.build() 主体", _as_text(_SystemPrompt.build("你好"))))

# 2) 询问模式规则
try:
    parts.append(("② _ask_rules(cfg) 询问模式规则", _as_text(_ask_rules(cfg))))
except Exception as e:
    parts.append(("② _ask_rules(cfg) 询问模式规则", "[构建失败] %s: %s" % (type(e).__name__, e)))

# 3) 回复格式模板
try:
    parts.append(("③ _reply_template_ctx() 回复格式模板", _as_text(_reply_template_ctx())))
except Exception as e:
    parts.append(("③ _reply_template_ctx() 回复格式模板", "[构建失败] %s: %s" % (type(e).__name__, e)))

# 4)+5) 从 main.py 里 AST 提取所有 `sys_prompt += "常量"` 的段
src_path = os.path.join(ROOT, "backend", "main.py")
with io.open(src_path, "r", encoding="utf-8") as f:
    src = f.read()
tree = ast.parse(src)
found = []
for node in ast.walk(tree):
    if (isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "sys_prompt"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)):
        found.append((node.lineno, node.value.value))
found.sort(key=lambda x: x[0])
for idx, (lineno, text) in enumerate(found):
    parts.append(("④ main.py L%d 内联段" % lineno, text))

full = "\n".join(p[1] for p in parts)

out_path = os.path.join(ROOT, "_sysprompt_full3.txt")
with io.open(out_path, "w", encoding="utf-8") as f:
    f.write(full)

print("OK path=%s" % out_path)
print("total_chars=%d" % len(full))
for name, t in parts:
    print("  [%s] %d chars" % (name, len(t)))
