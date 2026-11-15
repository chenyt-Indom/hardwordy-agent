# -*- coding: utf-8 -*-
"""回复形态闸门：结论前置 + 过程折叠 + 长度上限。

对应痛点：回复被截断 13 次，用户以为系统挂了。
这不是模型能力问题，是输出形态问题 —— 把「结论一行 → 折叠过程 → 产物」
变成硬约束，从根上消掉截断和「看起来卡住」。

用法（main.py 回复组装处）：
    from .reply_gate import format_reply
    text = format_reply(conclusion, process, artifacts, max_chars)
"""

from __future__ import annotations

# 过程超过这个字数就折叠成「N字，点开看」
FOLD_THRESHOLD = 600
# 折叠提示里展示的过程预览长度
FOLD_PREVIEW = 120
# 默认整体上限
DEFAULT_MAX_CHARS = 4000


def _as_text(x) -> str:
    """把任意输入（None / str / list / dict）转成文本。"""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, (list, tuple)):
        return "\n".join(_as_text(i) for i in x if i is not None)
    if isinstance(x, dict):
        return "\n".join("%s: %s" % (k, _as_text(v)) for k, v in x.items())
    return str(x)


def _fold_process(process: str) -> str:
    """过程超阈值就折叠成「N字，点开看」；短过程原样保留。"""
    if not process:
        return ""
    n = len(process)
    if n <= FOLD_THRESHOLD:
        return process
    preview = process[:FOLD_PREVIEW].replace("\n", " ").strip()
    return "<details><summary>过程 %d 字，点开看</summary>\n\n%s\n\n…（其余 %d 字已折叠）\n\n</details>" % (
        n, preview, n - FOLD_PREVIEW)


def format_reply(conclusion, process=None, artifacts=None, max_chars=DEFAULT_MAX_CHARS) -> str:
    """按「结论一行 → 折叠过程 → 产物」组装回复，并保证总长不超 max_chars。

    硬约束：
      · 结论永远在最前面，且**不会被截断**（结论本身超长时整体放宽到结论长度）；
      · 过程超阈值折叠成「N字，点开看」；
      · 整体截到 max_chars 以内，截断只发生在过程/产物尾部。
    """
    conclusion = _as_text(conclusion).strip()
    process = _as_text(process).strip()
    artifacts = _as_text(artifacts).strip()

    try:
        limit = int(max_chars)
    except Exception:
        limit = DEFAULT_MAX_CHARS
    if limit <= 0:
        limit = DEFAULT_MAX_CHARS

    # 结论必须完整保留：上限至少给结论留出空间
    if len(conclusion) > limit:
        limit = len(conclusion)

    parts = []
    if conclusion:
        parts.append(conclusion)

    folded = _fold_process(process)
    if folded:
        parts.append(folded)

    if artifacts:
        parts.append(artifacts)

    text = "\n\n".join(parts)
    if len(text) <= limit:
        return text

    # 超长：优先砍产物，再砍过程，结论不动
    head = conclusion
    tail_parts = []
    if folded:
        tail_parts.append(folded)
    if artifacts:
        tail_parts.append(artifacts)

    budget = limit - len(head) - (2 if head else 0)
    if budget <= 0:
        return head[:limit]

    kept = []
    for p in tail_parts:
        if budget <= 0:
            break
        if len(p) <= budget:
            kept.append(p)
            budget -= len(p) + 2
            continue
        # ★ 2026-11-15 修：折叠块是 <details>…</details>，**绝不能截半截**。
        #   截断会留下没有闭合的 <details>，浏览器会把后面的内容全部当折叠区吞掉，
        #   用户看到的就是「回复莫名其妙没了」。放不下就整块换成一行纯文本提示。
        if p.startswith("<details"):
            kept.append("（过程过长，已省略）")
            budget = 0
            break
        kept.append(p[:max(0, budget)])
        budget = 0

    out = "\n\n".join([head] + kept) if head else "\n\n".join(kept)
    return out[:limit]
