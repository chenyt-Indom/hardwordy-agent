"""从 LLM 输出里可靠地抠出 JSON —— 公共实现。

背景（2026-11-16 用户报「上一轮反思失败：模型返回的不是合法 JSON」）：

  根因不是模型不听话，而是三条链路上的**结构性缺陷**同时发作：
    ① `_llm_text` 给 max_tokens=4200，而反思计划 JSON 实测 12~16 KB（中文），
       装不下 → 输出被截断在半个对象上；
    ② `chat_stream` 截断时会**往正文里追加**一句
       「（输出达到 max_tokens 上限被截断）」—— 这行中文插在 JSON 中间，
       结构彻底坏掉，光靠"抠大括号"救不回来；
    ③ `chat_stream` 这条通道**没有续写逻辑**（续写只写在 chat_stream_with_tools），
       所以截断了也没人接着写。

  同一个模式存在于 self_iterate / papervec / reverify 三个模块，各自有一份
  弱得多的 `_parse_json`。与其各修各的，不如共用这一份。

本模块只做纯函数，不依赖项目其它模块（避免循环 import）。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# LLM 客户端或模型自己会往正文尾部附加这类提示 —— 对 JSON 解析是毒药
_NOTICE_RE = re.compile(
    r"[（(]\s*输出(?:达到|被)[^）)]{0,40}(?:截断|上限)[^）)]{0,20}[）)]"
    r"|（已达工具调用轮次上限[^）]*）"
    r"|（系统提示）[^\n]*"
    r"|（输出被长度上限截断[^）]*）")


def strip_notice(txt: str | None) -> str:
    """剔除非正文的提示文字。截断提示插在 JSON 中间会让结构彻底坏掉。"""
    if not txt:
        return txt or ""
    return _NOTICE_RE.sub("", txt).strip()


def balanced(txt: str | None) -> bool:
    """括号是否配平（忽略字符串里的括号）。False = 输出被截断了。"""
    if not txt:
        return False
    depth = 0
    in_str = False
    esc = False
    for ch in txt:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth <= 0 and not in_str


def _close_truncated(body: str):
    """把被截断的 JSON 补齐成可解析的：丢掉最后一个不完整元素，闭合括号。

    救回来的内容可能缺最后一条，但**前面的全在** —— 比整轮作废好得多。
    返回 dict 或 None。
    """
    if not body or body[0] != "{":
        return None
    out = []
    stack = []
    in_str = False
    esc = False
    for ch in body:
        out.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    tail = "".join(out)
    if in_str:                      # 字符串没闭合 → 补引号
        tail += '"'
    tail = re.sub(r"[,:]\s*$", "", tail)   # 停在逗号/冒号上
    closer = {"{": "}", "[": "]"}
    for opener in reversed(stack):         # 按栈补闭合括号
        tail += closer.get(opener, "}")
    tail = re.sub(r",\s*([}\]])", r"\1", tail)
    try:
        v = json.loads(tail)
    except Exception:                       # noqa: BLE001
        # 最后一个元素是残缺的键值（"title": "xxx 没有值）→ 砍掉它再补
        cut = tail.rfind(",")
        if cut < 0:
            return None
        tail2 = tail[:cut]
        stack2 = []
        in_s = False
        es = False
        for ch in tail2:
            if in_s:
                if es:
                    es = False
                elif ch == "\\":
                    es = True
                elif ch == '"':
                    in_s = False
                continue
            if ch == '"':
                in_s = True
            elif ch in "{[":
                stack2.append(ch)
            elif ch in "}]":
                if stack2:
                    stack2.pop()
        if in_s:
            tail2 += '"'
        tail2 = re.sub(r"[,:]\s*$", "", tail2)
        for opener in reversed(stack2):
            tail2 += closer.get(opener, "}")
        tail2 = re.sub(r",\s*([}\]])", r"\1", tail2)
        try:
            v2 = json.loads(tail2)
        except Exception:                   # noqa: BLE001
            return None
        if not isinstance(v2, dict) or not v2:
            return None
        return v2
    if not isinstance(v, dict) or not v:
        return None
    return v


def parse(txt: str | None):
    """从 LLM 输出里抠出 JSON dict；失败返回 None。

    依次尝试：剔提示 → 代码围栏 → 直接 loads → 抠大括号 → 脏格式清洗 → 截断修复。
    """
    if not txt:
        return None
    s = strip_notice(txt.strip())
    # ★ 2026-11-22 修：**先原样解析，再考虑剥代码围栏**。
    #   原顺序是先跑 `re.search(r"```(?:json)?\s*(.+?)```")` 抠围栏内容，
    #   但 JSON 正文里本身就可能带 ```（反思动作的 detail 里写测试代码、举例说明），
    #   于是两个围栏之间的几百字被当成"整个 JSON"——实测 8224 字的合法输出
    #   被砍成 654 字碎片 → parse 返回 None → 报「模型返回的不是合法 JSON」
    #   → 每日自我反思整轮作废（data/self_iterate/last_bad_reflect.txt 就是这份）。
    #   证据：那份输出 json.loads 直接成功，被围栏正则砍坏后才失败。
    try:
        v = json.loads(s)
        if isinstance(v, dict) and v:
            return v
    except Exception:                       # noqa: BLE001
        logger.debug("json 直接解析失败", exc_info=True)
    # 只有**整段被围栏包住**（开头 ``` 且结尾 ```）时才剥，绝不中途截取
    if s.startswith("```"):
        m = re.match(r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*$", s, re.S)
        if m:
            s2 = m.group(1).strip()
            try:
                v = json.loads(s2)
                if isinstance(v, dict) and v:
                    return v
            except Exception:                   # noqa: BLE001
                logger.debug("json 剥围栏后解析失败", exc_info=True)
            s = s2
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            v = json.loads(s[i:j + 1])
            if isinstance(v, dict) and v:
                return v
        except Exception:                   # noqa: BLE001
            logger.debug("json 抠大括号失败", exc_info=True)
    if i < 0:
        return None
    body = s[i:]
    # 脏格式清洗（先做，截断修复在它之后才准）
    body = re.sub(r"//[^\n]*", "", body)                # 行注释
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # 块注释
    body = body.replace("“", '"').replace("”", '"')
    body = body.replace("‘", "'").replace("’", "'")
    body = re.sub(r"([{\[,])\s*'([^']{0,80})'\s*:", r'\1"\2":', body)
    body = re.sub(r",\s*([}\]])", r"\1", body)          # 尾逗号
    try:
        v = json.loads(body)
        if isinstance(v, dict) and v:
            return v
    except Exception:                       # noqa: BLE001
        logger.debug("json 清洗后仍失败", exc_info=True)
    fixed = _close_truncated(body)
    if isinstance(fixed, dict):
        return fixed
    return None


def dump_raw(txt: str | None, out_dir, where: str = "reflect") -> str:
    """解析失败时把原始输出落盘 —— 没有证据就只能靠猜。返回路径。"""
    try:
        import time as _t
        from pathlib import Path
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("last_bad_%s.txt" % where)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# %s  raw_len=%d\n"
                    % (_t.strftime("%Y-%m-%d %H:%M:%S"), len(txt or "")))
            f.write(txt or "")
        return str(p)
    except Exception:                       # noqa: BLE001
        return ""
