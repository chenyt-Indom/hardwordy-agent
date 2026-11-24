# -*- coding: utf-8 -*-
"""导出**真实生效**的系统提示词全文（第四版 · 最终版）。

主通道 /api/chat 里 sys_prompt 的拼装（backend/main.py）：
  1) _SystemPrompt.build(...)                        L8437   已单独调用
  2) sys_prompt += _ask_rules(cfg)                   L8604   已单独调用
  3) sys_prompt += "\\n\\n" + _reply_template_ctx()    L8610   已单独调用
  4) 其余 `sys_prompt += (...)` 内联段                  L8461/8481/8500/8616/8661
     —— 其中 L8500 是 `"..." + _ask_n + "..."`（BinOp），
        第三版只取 Constant 节点，把这一段整段漏掉了，本版修掉。

内联段一律用 AST 从 main.py **原样折叠**，不手抄。
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


def _as_text(v):
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x) for x in v)
    return str(v)


# ---- _ask_n 的真实取值（与 main.py L8496 完全一致）----
if str(cfg.get("ask_mode") or "quick").lower() == "deep":
    ASK_N = ("**5~7 个**（深度询问模式：把用途/受众/篇幅/文体/重点/约束/参考样例都问到；"
             "拿到答复后如果还有影响写法的空白，**再问一轮**，别问一轮就动笔）")
else:
    ASK_N = "**2~4 个**（快速模式：只问不问就会写错方向的）"


def fold(node):
    """把 AST 节点折叠成字符串；折叠不了（含函数调用等）返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = fold(node.left)
        right = fold(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Name) and node.id == "_ask_n":
        return ASK_N
    return None


parts = [
    ("① _SystemPrompt.build() 主体", _as_text(_SystemPrompt.build("你好"))),
    ("② _ask_rules(cfg) 询问模式规则", _as_text(_ask_rules(cfg))),
    ("③ _reply_template_ctx() 回复格式模板", _as_text(_reply_template_ctx())),
]

src_path = os.path.join(ROOT, "backend", "main.py")
with io.open(src_path, "r", encoding="utf-8") as f:
    src = f.read()
tree = ast.parse(src)

found = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "sys_prompt"):
        continue
    # 只取主通道那一段区间（L8400~L8700），其它通道的 += 不算本轮系统提示
    if not (8400 <= node.lineno <= 8700):
        continue
    text = fold(node.value)
    if text is None:
        print("[skip] L%d 含运行时变量/函数调用，本轮不生效或已单独导出" % node.lineno)
        continue
    found.append((node.lineno, text))

found.sort(key=lambda x: x[0])
for lineno, text in found:
    parts.append(("④ main.py L%d 内联段" % lineno, text))

full = "\n".join(p[1] for p in parts)

out_path = os.path.join(ROOT, "_sysprompt_full4.txt")
with io.open(out_path, "w", encoding="utf-8") as f:
    f.write(full)

print("OK path=%s" % out_path)
print("total_chars=%d  lines=%d" % (len(full), full.count("\n") + 1))
for name, t in parts:
    print("  [%s] %d chars" % (name, len(t)))
