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


# 内部调试占位符：不渲染给用户（对应「你好」那轮泄漏）
# 说明：这些是思考块/调试面板的内部标记，一旦出现在正文里就是泄漏。
_INTERNAL_MARKERS = (
    "🧠 本次没有思考内容",
    "本次没有思考内容",
    "本次无思考内容",
    "没有思考内容",
    "归位",
)
# 思考块整体标记：命中后整段（含 ▸… 折叠尾巴）一起剥离
_THINKING_BLOCK_MARKERS = ("🧠", "思考内容", "思考块", "▸")


def _strip_internal(text: str) -> str:
    """过滤内部调试占位符，避免泄漏给用户。

    处理两类泄漏：
      1. 单行内部标记（「本次没有思考内容」「归位」等）—— 整行丢弃；
      2. 思考块占位（「🧠 本次没有思考内容▸…归位」这类混排）—— 若一行里
         同时命中思考块标记，则把该行整段剥离；若剥离后该行只剩空白/标点，
         也一并丢弃，避免留下「▸…」这种残渣。
    """
    if not text:
        return ""
    out_lines = []
    for line in text.split("\n"):
        if any(m in line for m in _INTERNAL_MARKERS):
            continue
        # 思考块混排：命中思考块标记且该行明显是占位/调试信息时整行剥离
        if any(m in line for m in _THINKING_BLOCK_MARKERS):
            # 只剥离「看起来像思考占位」的行，避免误伤正常正文里的 ▸ 列表符号
            stripped = line.strip()
            if stripped.startswith("🧠") or "思考内容" in stripped or "思考块" in stripped:
                continue
        out_lines.append(line)
    # 清理剥离后可能残留的孤立折叠尾巴（如单独一行的「▸…」）
    cleaned = []
    for line in out_lines:
        s = line.strip()
        if s in ("▸", "▸…", "▸...", "…", "..."):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


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


def sanitize(text) -> str:
    """回复后处理：空输出兜底 + 超长保留结论段 + 剥离内部标记。

    对应两个摩擦点：
      · 空回复硬失败 —— 用户看到一片空白，以为系统挂了；
      · 调试信息外泄 —— 「🧠 本次没有思考内容」「归位」这类内部标记
        不该出现在给用户看的回复里。

    行为：
      1. 剥离内部标记（复用 _strip_internal）；
      2. 空输出 / 只有省略号 → 给一句占位，绝不留白；
      3. 超长 → 保留结论段（首个空行前的部分），其余截断并提示。
    """
    s = _strip_internal(_as_text(text).strip())
    if not s or s in ("…", "...", "。。。」"):
        return "（本轮没有产出内容，请重试或换个说法。）"
    if len(s) <= DEFAULT_MAX_CHARS:
        return s
    # 超长：保留结论段（首个空行之前），其余截断
    head = s.split("\n\n", 1)[0].strip()
    if not head:
        head = s[:DEFAULT_MAX_CHARS]
    tail = "\n\n…（回复过长，已保留结论段）"
    room = max(0, DEFAULT_MAX_CHARS - len(tail))
    if len(head) > room:
        head = head[:room]
    return head + tail


# 本轮动作里出现这些关键字，视为「有 selfdev_patch 动作」
_PATCH_ACTION_MARKERS = ("selfdev_patch", "selfdev_write", "selfdev_restart")
# 回复里出现这些字样，视为已经交代了「改了哪些文件」
_CHANGED_FILES_MARKERS = ("已改文件", "已修改文件", "改动文件", "已改：", "已改:")
# 回复以这些「只诊断不修复」的口头禅结尾时，补一句结论
_DIAGNOSE_TAIL_MARKERS = ("我看一下", "我看下", "我看看", "我先看看", "我先看一下")


def _has_patch_action(actions) -> bool:
    """判断本轮动作里是否包含 selfdev_patch 类动作。

    对应摩擦点「只诊断不修复」14 次：模型经常只读代码、只写分析，
    回复里看不出到底改没改文件。这里把「有没有 patch 动作」变成可判定的事实。
    """
    if not actions:
        return False
    if isinstance(actions, str):
        return any(m in actions for m in _PATCH_ACTION_MARKERS)
    if isinstance(actions, dict):
        actions = [actions]
    try:
        for a in actions:
            if isinstance(a, str):
                if any(m in a for m in _PATCH_ACTION_MARKERS):
                    return True
                continue
            if not isinstance(a, dict):
                continue
            # 动作类型字段（不同调用方命名不一，全兜住）
            for k in ("action", "type", "tool", "name", "kind"):
                v = str(a.get(k) or "")
                if any(m in v for m in _PATCH_ACTION_MARKERS):
                    return True
            # 显式标记
            if a.get("selfdev_patch") or a.get("is_patch") or a.get("patch"):
                return True
    except Exception:                                   # noqa: BLE001
        return False
    return False


def _collect_changed_files(actions) -> list:
    """从本轮动作里抽出被改动的文件路径（去重、保序）。"""
    out = []
    seen = set()
    if not actions:
        return out
    if isinstance(actions, dict):
        actions = [actions]
    if isinstance(actions, str):
        return out
    try:
        for a in actions:
            if not isinstance(a, dict):
                continue
            for k in ("file", "path", "target", "filename"):
                v = a.get(k)
                if isinstance(v, str) and v.strip():
                    v = v.strip()
                    if v not in seen:
                        seen.add(v)
                        out.append(v)
            files = a.get("files")
            if isinstance(files, (list, tuple)):
                for v in files:
                    if isinstance(v, str) and v.strip():
                        v = v.strip()
                        if v not in seen:
                            seen.add(v)
                            out.append(v)
    except Exception:                                   # noqa: BLE001
        return out
    return out


def _ends_with_diagnose(text: str) -> bool:
    """回复是否以「我看一下…」这类只诊断不修复的口头禅结尾。"""
    if not text:
        return False
    tail = text.strip().rstrip("。.！!？?…~～ \t\n")
    if not tail:
        return False
    # 只看最后一行，避免正文里出现「我看一下」被误判
    last_line = tail.split("\n")[-1].strip().rstrip("。.！!？?…~～ \t")
    return any(last_line.endswith(m) for m in _DIAGNOSE_TAIL_MARKERS)


def _structure_check(text: str, actions=None, todos=None) -> str:
    """每轮结尾强制输出「结论 + 已改文件 + 待办」的结构化小结。

    对应摩擦点「只诊断不修复」14 次：模型经常只读代码、只写分析，
    回复里看不出到底改没改文件、下一步要做什么。这里做两道结构检查：

      1. 本轮有 selfdev_patch 动作，但回复里没有「已改文件」字样
         → 追加一段结构化小结（结论 / 已改文件 / 待办）；
      2. 回复以「我看一下…」这类口头禅结尾
         → 补一句明确结论，别让用户以为又只诊断不修复。

    返回处理后的文本；不需要补时原样返回。
    """
    if not text:
        return text
    out = text
    has_patch = _has_patch_action(actions)
    has_changed_marker = any(m in out for m in _CHANGED_FILES_MARKERS)

    # 检查 1：有 patch 动作但没交代改了哪些文件 → 追加结构化小结
    if has_patch and not has_changed_marker:
        files = _collect_changed_files(actions)
        file_line = "、".join(files) if files else "（本轮动作未带文件路径）"
        todo_line = _as_text(todos).strip() if todos else ""
        if not todo_line:
            todo_line = "验证改动是否生效；如未生效，下一轮继续修"
        # 结论：取回复首行（首个空行前）作为结论，没有就用一句兜底
        head = out.split("\n\n", 1)[0].strip()
        if not head:
            head = "本轮已产出代码改动"
        # 结论太长时截一下，小结本身要短
        if len(head) > 200:
            head = head[:200].rstrip() + "…"
        summary = (
            "\n\n---\n"
            "**结论**：%s\n"
            "**已改文件**：%s\n"
            "**待办**：%s"
        ) % (head, file_line, todo_line)
        out = out.rstrip() + summary

    # 检查 2：以「我看一下…」结尾 → 补一句结论
    if _ends_with_diagnose(out):
        out = out.rstrip() + "\n\n结论：本轮先定位问题，下一步直接给出修复 patch（不只诊断）。"

    return out


def format_reply(conclusion, process=None, artifacts=None, max_chars=DEFAULT_MAX_CHARS,
                 actions=None, todos=None) -> str:
    """按「结论一行 → 折叠过程 → 产物」组装回复，并保证总长不超 max_chars。

    硬约束：
      · 结论永远在最前面，且**不会被截断**（结论本身超长时整体放宽到结论长度）；
      · 过程超阈值折叠成「N字，点开看」；
      · 整体截到 max_chars 以内，截断只发生在过程/产物尾部；
      · 每轮结尾强制输出「结论 + 已改文件 + 待办」结构（见 _structure_check）。

    参数 actions / todos 可选：传了本轮动作列表和待办，就能做结构检查；
    不传则退化为纯形态组装（向后兼容）。
    """
    conclusion = _strip_internal(_as_text(conclusion).strip())
    process = _strip_internal(_as_text(process).strip())
    artifacts = _strip_internal(_as_text(artifacts).strip())

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

    # ★ 空回复硬失败兜底：最终回复为空或只有「…」时，强制给出「已知 + 下一步」。
    #   对应 3 次空回复硬失败 —— 宁可给一句占位，也不让用户看到空白。
    _stripped = text.strip()
    if not _stripped or _stripped in ("…", "...", "。。。"):
        known = conclusion or "（本轮未产出结论）"
        nxt = process or artifacts or "（待补充）"
        text = "当前已知：%s；下一步要查：%s" % (known, nxt)

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
