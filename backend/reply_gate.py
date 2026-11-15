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
# 回复超过这个字数就强制「结论前置 + 过程折叠」形态（对应「回复被截断」）
FORCE_CONCLUSION_THRESHOLD = 800
# 长度预判阈值：预估输出超过这个字数时，强制先输出「结论 + 已改文件 + 待办」
# 三行摘要，再展开细节 —— 避免一次性超长输出被截断后用户拿到零信息。
# 比 FORCE_CONCLUSION_THRESHOLD 略低，留出摘要本身占用的空间。
PREDICT_TRUNCATE_THRESHOLD = 700
# 结尾固定输出的「已改文件 / 待办」分隔线
_TAIL_SEP = "\n\n---\n"


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


def _fold_process(process: str, force: bool = False) -> str:
    """过程超阈值就折叠成「N字，点开看」；短过程原样保留。

    force=True 时（回复整体过长、需要强制结论前置形态）即使没到
    FOLD_THRESHOLD 也折叠 —— 保证长回复里过程永远不占正文。
    """
    if not process:
        return ""
    n = len(process)
    if n <= FOLD_THRESHOLD and not force:
        return process
    preview = process[:FOLD_PREVIEW].replace("\n", " ").strip()
    if n <= FOLD_PREVIEW:
        return "<details><summary>过程 %d 字，点开看</summary>\n\n%s\n\n</details>" % (
            n, preview)
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


# ── 结论前置闸门（首段结论句判定）────────────────────────────────
# 首段长度区间：太短不像结论，太长不像「一行结论」
_CONCLUSION_MIN_LEN = 10
_CONCLUSION_MAX_LEN = 120
# 首段以这些标点结尾，视为「陈述句结尾」（结论句的典型形态）
_STATEMENT_ENDINGS = ("。", "！", "!", ".", "；", ";", "）", ")", "」", "”", "\"")
# 首段命中这些开头，说明已经是结论/小结形态，无需重排
_CONCLUSION_PREFIXES = ("结论", "小结", "总结", "结果", "已完成", "已修复", "已改")
# 首段出现这些「过程/诊断」口吻，视为可疑（需要 LLM 复核）
_PROCESS_TAIL_HINTS = ("我看一下", "我看下", "我看看", "我先看看", "我先看一下",
                       "让我", "接下来", "先分析", "先看", "正在", "稍等")


def _looks_like_conclusion(first_para: str) -> bool:
    """轻量启发式：首段是否已经是一句合格的「结论句」。

    判定条件（全部满足才算合格）：
      · 不含问号（结论不该是提问）；
      · 长度在 10~120 字之间；
      · 以陈述句标点结尾（。！；等），或以结论类前缀开头。

    返回 True 表示「看起来像结论」，False 表示「可疑，需要复核」。
    """
    s = (first_para or "").strip()
    if not s:
        return False
    if "?" in s or "？" in s:
        return False
    n = len(s)
    if n < _CONCLUSION_MIN_LEN or n > _CONCLUSION_MAX_LEN:
        return False
    if s.startswith(_CONCLUSION_PREFIXES):
        return True
    if s.endswith(_STATEMENT_ENDINGS):
        return True
    return False


def _is_suspicious_first_para(first_para: str) -> bool:
    """首段是否「可疑」——命中过程/诊断口吻，或明显不像结论。"""
    s = (first_para or "").strip()
    if not s:
        return False
    if any(h in s for h in _PROCESS_TAIL_HINTS):
        return True
    return not _looks_like_conclusion(s)


def _llm_judge_conclusion(first_para: str) -> bool:
    """可疑首段走一次 LLM 判定：它是不是一句合格的结论句。

    设计为「可插拔 + 安全降级」：
      · 若环境里配置了 reply_gate 的 LLM 判定回调（set_conclusion_judge），
        就调用它；回调返回 True 表示「是结论」，False 表示「不是」；
      · 未配置回调 / 回调抛异常 → 降级为启发式结果（_looks_like_conclusion），
        绝不因为 LLM 不可用而卡住回复。
    """
    judge = _CONCLUSION_JUDGE
    if judge is None:
        return _looks_like_conclusion(first_para)
    try:
        return bool(judge(first_para))
    except Exception:                                   # noqa: BLE001
        return _looks_like_conclusion(first_para)


# LLM 判定回调（由调用方注入；None 表示只用启发式）
_CONCLUSION_JUDGE = None


def set_conclusion_judge(fn) -> None:
    """注入「首段是否结论句」的 LLM 判定回调（可选）。

    回调签名：fn(first_para: str) -> bool
      True  = 是结论句；False = 不是（需要重排）。
    传 None 可恢复为纯启发式。
    """
    global _CONCLUSION_JUDGE
    _CONCLUSION_JUDGE = fn


def _ensure_conclusion_first(text: str) -> str:
    """结论前置闸门：首段不像结论时，在首段前插入「结论：」前缀重排。

    流程：
      1. 取首段（首个空行之前）；
      2. 启发式判定 —— 像结论就直接返回；
      3. 可疑 → 走一次 LLM 判定（未配置则降级启发式）；
      4. 仍不通过 → 在首段前插入「结论：」前缀，让结论先行。

    注意：只加前缀、不删原文，保证信息不丢；已带「结论」前缀的不重复加。
    """
    if not text:
        return text
    s = text.strip()
    if not s:
        return text
    # 已经以结论类前缀开头 → 无需处理
    if s.startswith(_CONCLUSION_PREFIXES):
        return text
    # 取首段（首个空行之前）
    parts = s.split("\n\n", 1)
    first_para = parts[0].strip()
    if not first_para:
        return text
    # 启发式：像结论 → 直接返回
    if _looks_like_conclusion(first_para):
        return text
    # 可疑 → LLM 复核
    if not _is_suspicious_first_para(first_para):
        return text
    if _llm_judge_conclusion(first_para):
        return text
    # 不通过 → 首段前插入「结论：」前缀重排
    rest = parts[1] if len(parts) > 1 else ""
    new_first = "结论：" + first_para
    if rest:
        return new_first + "\n\n" + rest
    return new_first


# 空输出兜底模板：已知 + 下一步
_EMPTY_FALLBACK_TEMPLATE = "已知：%s；下一步：%s"
# 最近一轮用户输入摘要的最大长度（避免兜底模板本身过长）
_EMPTY_FALLBACK_SUMMARY_MAX = 80
# 最近一条工具结果摘要的最大长度（用于「当前已知」部分）
_TOOL_RESULT_SUMMARY_MAX = 120
# 待办摘要的最大长度（用于「下一步要查」部分）
_TODO_SUMMARY_MAX = 120


def _summarize_user_input(user_input) -> str:
    """把最近一轮用户输入压成一句短摘要（用于空输出兜底模板）。"""
    s = _as_text(user_input).strip()
    if not s:
        return "（本轮无用户输入）"
    # 压成单行
    s = " ".join(s.split())
    if len(s) > _EMPTY_FALLBACK_SUMMARY_MAX:
        s = s[:_EMPTY_FALLBACK_SUMMARY_MAX].rstrip() + "…"
    return s


def _summarize_tool_result(tool_result) -> str:
    """把最近一条工具结果压成一句短摘要（用于「当前已知」部分）。

    工具结果可能是 str / list / dict / None，统一转文本后压成单行并截断。
    空结果返回空串，由调用方决定是否回退到用户输入摘要。
    """
    s = _as_text(tool_result).strip()
    if not s:
        return ""
    s = " ".join(s.split())
    if len(s) > _TOOL_RESULT_SUMMARY_MAX:
        s = s[:_TOOL_RESULT_SUMMARY_MAX].rstrip() + "…"
    return s


def _summarize_todos(todos) -> str:
    """把待办压成一句短摘要（用于「下一步要查」部分）。

    支持 str / list / dict；空待办返回空串，由调用方决定回退文案。
    """
    s = _as_text(todos).strip()
    if not s:
        return ""
    s = " ".join(s.split())
    if len(s) > _TODO_SUMMARY_MAX:
        s = s[:_TODO_SUMMARY_MAX].rstrip() + "…"
    return s


def _empty_output_fallback(user_input=None, suggested_action=None,
                           tool_result=None, todos=None) -> str:
    """空输出兜底：输出为空或纯空白时，强制返回「已知 + 下一步」模板。

    对应「空回复硬失败」—— 宁可给一句有信息量的占位，也不让用户看到空白。

    「当前已知」优先取最近一条工具结果摘要（信息量最大），没有工具结果时
    回退到用户输入摘要；「下一步要查」优先取待办摘要，其次取建议动作，
    都没有时给一句通用提示。
    """
    known = _summarize_tool_result(tool_result) or _summarize_user_input(user_input)
    nxt = _summarize_todos(todos) or _as_text(suggested_action).strip() or "请重试或换个说法"
    return _EMPTY_FALLBACK_TEMPLATE % (known, nxt)


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


def _ensure_conclusion_label(conclusion: str) -> str:
    """结论段统一加「结论：」前缀（已带结论类前缀的不重复加）。

    对应「结论前置」硬约束：长回复的首段必须一眼看出是结论，
    而不是「我看一下…」这种过程口吻。
    """
    s = (conclusion or "").strip()
    if not s:
        return s
    if s.startswith(_CONCLUSION_PREFIXES):
        return s
    return "结论：" + s


def _build_tail(actions=None, todos=None) -> str:
    """结尾固定输出「已改文件 / 待办」两行（无内容时给明确占位）。

    对应摩擦点「只诊断不修复」：每轮结尾都必须让用户一眼看到
    「到底改了哪些文件」「下一步做什么」，不能只留一段分析就结束。
    """
    files = _collect_changed_files(actions)
    file_line = "、".join(files) if files else "（本轮未改动文件）"
    todo_line = _as_text(todos).strip() if todos else ""
    if not todo_line:
        todo_line = "验证改动是否生效；如未生效，下一轮继续修"
    return _TAIL_SEP + "**已改文件**：%s\n**待办**：%s" % (file_line, todo_line)


def _build_head_summary(conclusion, actions=None, todos=None) -> str:
    """长度预判命中时，生成「结论 + 已改文件 + 待办」三行摘要。

    对应「回复被截断后用户拿到零信息」：在展开细节之前，先把最关键的三件事
    压成三行放在最前面，即使后面的细节被截断，用户也至少知道：
      · 本轮结论是什么；
      · 改了哪些文件；
      · 下一步要做什么。

    结论取首行（首个空行前），过长时截断；已带结论类前缀的不重复加。
    """
    head = (conclusion or "").strip()
    if not head:
        head = "本轮已处理"
    # 取首段（首个空行前）作为结论行
    head = head.split("\n\n", 1)[0].strip()
    if not head:
        head = "本轮已处理"
    # 结论行太长时截一下，摘要本身要短
    if len(head) > 200:
        head = head[:200].rstrip() + "…"
    head = _ensure_conclusion_label(head)

    files = _collect_changed_files(actions)
    file_line = "、".join(files) if files else "（本轮未改动文件）"
    todo_line = _as_text(todos).strip() if todos else ""
    if not todo_line:
        todo_line = "验证改动是否生效；如未生效，下一轮继续修"

    return "%s\n**已改文件**：%s\n**待办**：%s" % (head, file_line, todo_line)


def _predict_will_truncate(conclusion, process, artifacts, limit) -> bool:
    """长度预判：按当前形态组装后，预估输出是否会超过上限被截断。

    判据用「结论 + 过程 + 产物」的原始总长（折叠前），因为折叠后虽然变短，
    但折叠块本身也有开销；宁可早一点触发摘要，也不要等真被截断。

    返回 True 表示「预估会超长，需要先输出三行摘要」。
    """
    try:
        limit = int(limit)
    except Exception:
        limit = DEFAULT_MAX_CHARS
    if limit <= 0:
        limit = DEFAULT_MAX_CHARS
    raw_len = len(conclusion or "") + len(process or "") + len(artifacts or "")
    # 超过预判阈值，或原始总长已经逼近上限，都视为会截断
    return raw_len > PREDICT_TRUNCATE_THRESHOLD or raw_len > limit


def enforce_shape(text, user_input=None, suggested_action=None, actions=None,
                  todos=None, max_chars=DEFAULT_MAX_CHARS) -> str:
    """回复形态闸门（形态硬约束版）：结论前置 + 过程折叠 + 长度上限 + 空回复兜底。

    与 enforce() 的区别：本函数把「回复形态」当成**硬约束**来执行，而不是
    尽量美化。对应三个高频摩擦点：

      ① 只诊断不修复 —— 回复必须以「结论：」或明确的 yes/no 开头，
         否则在首行前强制插入「结论：」；
      ② 空回复 —— 空文本 / 纯空白 / 只有省略号视为**硬失败**，
         直接返回「当前已知 + 下一步要查什么」兜底模板；
      ③ 回复被截断 —— 超过 max_chars 时截断并显式提示「已截断」，
         且过程一律折叠到 <details>，保证结论永远在最前面。

    参数
    ----
    text : 模型产出的原始回复（str / list / dict 均可）
    user_input : 最近一轮用户输入（用于空回复兜底模板的「已知」部分）
    suggested_action : 建议的下一步动作（用于空回复兜底模板的「下一步」部分）
    actions : 本轮动作列表（用于判定「已改 X 文件」与结构小结）
    todos : 待办（用于结构小结）
    max_chars : 整体长度上限

    返回处理后的文本；任何异常都退回原文，绝不因为格式化而丢内容。
    """
    raw = _as_text(text)
    try:
        s = _strip_internal(raw.strip())

        # ③ 空回复硬失败 → 「当前已知 + 下一步要查什么」兜底
        if not s or s in ("…", "...", "。。。"):
            known = _summarize_user_input(user_input)
            nxt = _as_text(suggested_action).strip() or "请重试或换个说法"
            return "当前已知：%s；下一步要查什么：%s" % (known, nxt)

        # ① 结论前置：首行必须是「结论：」或明确的 yes/no
        s = _force_conclusion_head(s)

        # ② 过程折叠：把过程口吻的段落收进 <details>
        s = _fold_process_paragraphs(s)

        # 结构小结：有 patch 动作但没交代改了哪些文件 → 补「已改文件 / 待办」
        s = _structure_check(s, actions, todos)

        # ③ 长度上限：超长截断并显式提示
        try:
            limit = int(max_chars)
        except Exception:
            limit = DEFAULT_MAX_CHARS
        if limit <= 0:
            limit = DEFAULT_MAX_CHARS
        if len(s) > limit:
            notice = "\n\n…（回复超过 %d 字，已截断；完整内容请点开过程或重试）" % limit
            room = max(0, limit - len(notice))
            s = s[:room].rstrip() + notice
        return s
    except Exception:                                   # noqa: BLE001
        return raw


# 明确的 yes/no 开头（结论前置的另一种合法形态）
_YES_NO_PREFIXES = ("是", "否", "对", "不对", "可以", "不可以", "能", "不能",
                    "Yes", "No", "YES", "NO", "yes", "no")


def _starts_with_yes_no(text: str) -> bool:
    """首行是否以明确的 yes/no 开头（结论前置的合法形态之一）。"""
    s = (text or "").strip()
    if not s:
        return False
    first = s.split("\n", 1)[0].strip()
    if not first:
        return False
    # 允许「是的」「不是」「可以，…」「不能，因为…」这类
    for p in _YES_NO_PREFIXES:
        if first.startswith(p):
            # 避免误伤「是否…」这种提问
            if first.startswith("是否"):
                continue
            return True
    return False


def _force_conclusion_head(text: str) -> str:
    """结论前置硬约束：首行必须是「结论：」或明确的 yes/no，否则强制补前缀。

    与 _ensure_conclusion_first 的区别：本函数不做启发式/LLM 判定，
    只做**硬性形态检查** —— 首行既不是结论类前缀、也不是 yes/no 开头，
    就直接在首行前插入「结论：」。保证用户第一眼永远看到结论。
    """
    if not text:
        return text
    s = text.strip()
    if not s:
        return text
    first_line = s.split("\n", 1)[0].strip()
    if not first_line:
        return text
    # 已经是结论类前缀 → 合法
    if first_line.startswith(_CONCLUSION_PREFIXES):
        return text
    # 已经是明确 yes/no 开头 → 合法
    if _starts_with_yes_no(first_line):
        return text
    # 否则强制补「结论：」前缀
    return "结论：" + s


def _fold_process_paragraphs(text: str) -> str:
    """把过程口吻的段落折叠到 <details>，结论段永远留在正文。

    判据：段落命中过程/诊断口吻（_is_suspicious_first_para），且不是结论类前缀。
    折叠后的过程统一收进一个 <details> 块，避免正文被过程淹没。
    """
    if not text:
        return text
    paras = text.split("\n\n")
    head_paras = []
    body_paras = []
    for p in paras:
        ps = p.strip()
        if not ps:
            continue
        if _is_suspicious_first_para(ps) and not ps.startswith(_CONCLUSION_PREFIXES):
            body_paras.append(ps)
        else:
            head_paras.append(ps)
    head = "\n\n".join(head_paras).strip()
    body = "\n\n".join(body_paras).strip()
    if not body:
        return head or text
    folded = _fold_process(body, force=True)
    if not head:
        return folded
    return head + "\n\n" + folded


def enforce(text, user_input=None, suggested_action=None, actions=None, todos=None,
            max_chars=DEFAULT_MAX_CHARS) -> str:
    """回复形态闸门（单一入口）：结论前置 + 过程折叠 + 空输出兜底。

    这是给 main.py 回复出口调用的**统一闸门**，把三件事一次做完：

      ① 首行强制是结论（通过 / 未通过 / 已改 X 文件）—— 没有就补一行；
      ② 超过 800 字的探查过程折叠到 <details>；
      ③ 空输出或只有「…」时返回「当前已知 + 下一步要查什么」兜底。

    对应三个高频摩擦点：只诊断不修复 / 空回复 / 回复被截断。

    参数
    ----
    text : 模型产出的原始回复（str / list / dict 均可）
    user_input : 最近一轮用户输入（用于空输出兜底模板）
    suggested_action : 建议的下一步动作（用于空输出兜底模板）
    actions : 本轮动作列表（用于判定「已改 X 文件」与结构小结）
    todos : 待办（用于结构小结）
    max_chars : 整体长度上限

    返回处理后的文本；任何异常都退回原文，绝不因为格式化而丢内容。
    """
    raw = _as_text(text)
    try:
        s = _strip_internal(raw.strip())
        # ③ 空输出 / 只有省略号 → 「已知 + 下一步」兜底
        if not s or s in ("…", "...", "。。。"):
            return _empty_output_fallback(user_input, suggested_action)

        # ② 过程折叠：把「过程口吻」的段落收进 <details>。
        #    判据：整体超 FORCE_CONCLUSION_THRESHOLD，或存在明显的过程段。
        paras = s.split("\n\n")
        head_paras = []
        body_paras = []
        for p in paras:
            ps = p.strip()
            if not ps:
                continue
            if _is_suspicious_first_para(ps) and not ps.startswith(_CONCLUSION_PREFIXES):
                body_paras.append(ps)
            else:
                head_paras.append(ps)
        head = "\n\n".join(head_paras).strip()
        body = "\n\n".join(body_paras).strip()

        # ① 首行强制结论：没有结论就补一行
        if not head:
            # 全是过程 → 用过程首句当结论占位，并把过程折叠
            first = body.split("\n", 1)[0].strip() if body else ""
            head = _ensure_conclusion_label(first or "本轮已处理")
        elif not head.startswith(_CONCLUSION_PREFIXES):
            head = _ensure_conclusion_label(head)

        # ② 过程折叠：body 超阈值就折叠
        folded = _fold_process(body, force=len(body) > FORCE_CONCLUSION_THRESHOLD)

        parts = [head]
        if folded:
            parts.append(folded)
        out = "\n\n".join(parts)

        # 结构小结：有 patch 动作但没交代改了哪些文件 → 补「已改文件 / 待办」
        out = _structure_check(out, actions, todos)

        # 长度上限：结论不动，只砍过程尾部
        try:
            limit = int(max_chars)
        except Exception:
            limit = DEFAULT_MAX_CHARS
        if limit <= 0:
            limit = DEFAULT_MAX_CHARS
        if len(head) > limit:
            limit = len(head)
        if len(out) > limit:
            budget = limit - len(head) - (2 if head else 0)
            if budget <= 0:
                return head[:limit]
            if folded:
                if folded.startswith("<details"):
                    out = head + "\n\n（过程过长，已省略）"
                else:
                    out = head + "\n\n" + folded[:budget]
            else:
                out = head[:limit]
        return out
    except Exception:                                   # noqa: BLE001
        return raw


def format_reply(conclusion, process=None, artifacts=None, max_chars=DEFAULT_MAX_CHARS,
                 actions=None, todos=None, user_input=None, suggested_action=None) -> str:
    """按「结论一行 → 折叠过程 → 产物 → 已改文件/待办」组装回复。

    硬约束：
      · 结论永远在最前面，且**不会被截断**（结论本身超长时整体放宽到结论长度）；
      · 回复超过 FORCE_CONCLUSION_THRESHOLD 时强制首段为「结论：…」，
        过程/代码块一律折叠（即使没到 FOLD_THRESHOLD）；
      · 整体截到 max_chars 以内，截断只发生在过程/产物尾部；
      · 结尾固定输出「已改文件 / 待办」（见 _build_tail）。

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

    # ★ 长回复强制形态：结论前置 + 过程折叠。
    #   判据用「结论+过程+产物」的原始总长，避免折叠后反而漏判。
    raw_len = len(conclusion) + len(process) + len(artifacts)
    force_form = raw_len > FORCE_CONCLUSION_THRESHOLD

    # ★ 长度预判兜底：预估输出会超长被截断时，强制先输出「结论 + 已改文件 + 待办」
    #   三行摘要，再展开细节。这样即使后面的细节被截断，用户也至少拿到三行关键信息，
    #   而不是一片空白或半截过程。
    predict_truncate = _predict_will_truncate(conclusion, process, artifacts, limit)

    if force_form:
        conclusion = _ensure_conclusion_label(conclusion)

    parts = []
    if predict_truncate:
        # 三行摘要永远在最前面，且不参与后续折叠/截断
        parts.append(_build_head_summary(conclusion, actions, todos))
        # 摘要已含结论行，正文里不再重复结论（避免「结论：…」出现两次）
        conclusion = ""
    elif conclusion:
        parts.append(conclusion)

    folded = _fold_process(process, force=force_form)
    if folded:
        parts.append(folded)

    if artifacts:
        parts.append(artifacts)

    text = "\n\n".join(parts)

    # ★ 空回复硬失败兜底：最终回复为空或只有「…」时，强制给出「已知 + 下一步」。
    #   对应 3 次空回复硬失败 —— 宁可给一句占位，也不让用户看到空白。
    #   出口检查：空串 / 纯空白 / 只有省略号一律视为硬失败，绝不让空串直接返回。
    _stripped = text.strip()
    if not _stripped or _stripped in ("…", "...", "。。。", "。。", "···", "⋯"):
        # 用「当前轮已收集到的工具结果 + 待办/建议动作」拼最小结论：
        #   「目前查到 X，还没确认 Y，下一步查 Z」
        # 工具结果优先（信息量最大），没有则回退到用户输入摘要。
        text = _empty_output_fallback(
            user_input=user_input,
            suggested_action=suggested_action,
            tool_result=artifacts or process,
            todos=todos,
        )

    # ★ 结论前置闸门：首段不像结论时，插入「结论：」前缀重排。
    #   放在长度截断之前，保证重排后的结论段不会被截掉。
    text = _ensure_conclusion_first(text)

    # ★ 结尾固定输出「已改文件 / 待办」：长回复或本轮有 patch 动作时必带。
    #   放在截断之前，保证这两行永远在（截断只砍过程/产物尾部）。
    #   长度预判命中时，三行摘要里已经带了「已改文件 / 待办」，不再重复追加。
    if (force_form or _has_patch_action(actions)) and not predict_truncate:
        if "**已改文件**" not in text and "已改文件：" not in text:
            text = text.rstrip() + _build_tail(actions, todos)

    if len(text) <= limit:
        return text

    # 超长：优先砍产物，再砍过程，结论不动。
    # 长度预判命中时，三行摘要已经占了 parts[0]，结论已并入摘要，head 为空，
    # 截断只砍后面的过程/产物，摘要三行永远保留。
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
