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
import time

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


# =====================================================================
#  safe_get：兜底取字段，避免 KeyError / AttributeError
#  ---------------------------------------------------------------------
#  2026-11-22 用户要求：「safe_get 兜底取字段，避免 KeyError」。
#
#  为什么要它：详情面板（iterpanel）与体检接口（main.py 的
#  /api/iter/health、/api/selfiter/health、/api/iter/panel）都是**只读聚合层**，
#  数据来自七八个模块的 stats() / check() / health_snapshot()。上游少给一个键、
#  返回 None、或者列表里混进一条格式不对的行时：
#        obj["k"]        → KeyError，
#        obj.get("k")    → 在 None 上 AttributeError，
#  整个接口 500，前端显示「体系挂了」—— 实际只是某个字段没给。用户看到的
#  「体检报错」十次有九次是这个原因，不是体系真的坏了。
#
#  统一口径：
#    · 取得到 → 返回**真实值本体**（0 / False / "" 也算取到，绝不当缺失）；
#    · 取不到 → 返回 default，**绝不抛异常**（路径非法、类型对不上都一样）；
#    · 取不到时把「实际可用的 key 列表」写进 actlog 的 field_miss 事件 ——
#      否则下一个人只能靠猜「上游到底给了什么字段」。
# =====================================================================

_MISS_SESSION = "field_miss"     # 落盘到 data/actlog/field_miss.jsonl
_MAX_KEYS = 24                   # 事件里最多列多少个可用 key（别把账本撑大）
_MISS_KEEP = 200                 # 单进程最多记多少条，防止异常路径刷爆账本
_MISS_GAP = 5.0                  # 同一 (调用点, 路径) 5 秒内只记一次
_MISS_SEEN: dict = {}
_MISS_N = 0


def _split_path(path) -> list:
    """'a.b.c' → ['a','b','c']；也接受已经拆好的序列。空段直接丢掉。

    ★ 2026-11-22 补：支持**方括号下标 / 带点的键名**写法，可与点分混用 ——
        'items[0].name'  → ['items', '0', 'name']
        'items[-1].name' → ['items', '-1', 'name']
        'a["k.w"].b'     → ['a', 'k.w', 'b']   （引号里的点不再被拆开）

    以前只认纯点分：'items[0].name' 会去找字面上的 'items[0]' 键 ——
    数据明明在，却被判成缺字段，field_miss 账本里堆一堆**假缺失**，
    排查时反而被误导。
    """
    if isinstance(path, (list, tuple)):
        return [str(p).strip() for p in path if str(p).strip()]
    s = str(path or "").strip()
    if not s:
        return []
    parts, buf, i, n = [], [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch == ".":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 1
            continue
        if ch == "[":
            # 先把方括号前面那一段推进去（'items' 之类）
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            j, quote = i + 1, ""
            while j < n:
                c = s[j]
                if quote:
                    if c == quote:
                        quote = ""
                elif c in "'\"":
                    quote = c
                elif c == "]":
                    break
                j += 1
            if j >= n:                  # 括号没闭合 → 还原成普通键名，保持老的宽松行为
                if seg and parts and parts[-1] == seg:
                    parts.pop()         # 刚才推过头了，退回去
                buf = list(seg) + [ch]
                i += 1
                continue
            inner = s[i + 1:j].strip()
            if len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]:
                inner = inner[1:-1].strip()     # 去引号：'k.w' 是一个完整的键
            if inner:
                parts.append(inner)
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        parts.append(seg)
    return parts


def _avail_keys(obj) -> list:
    """取 obj 上「实际可用的 key 列表」，给 field_miss 事件用。"""
    try:
        if isinstance(obj, dict):
            ks = [str(k) for k in obj.keys()]
        elif isinstance(obj, (list, tuple)):
            ks = ["[%d]" % i for i in range(min(len(obj), _MAX_KEYS))]
        else:
            ks = []
    except Exception:                       # noqa: BLE001
        ks = []
    return ks[:_MAX_KEYS]


def _soft_key(obj, seg):
    """大小写 / 首尾空白不敏感地找一个键（LLM 给的 JSON 常见 'Title' / ' Title'）。

    找到返回**原始键**（拿来索引），找不到返回 None。
    """
    want = str(seg).strip().lower()
    for k in obj.keys():
        try:
            if str(k).strip().lower() == want:
                return k
        except Exception:                   # noqa: BLE001
            continue
    return None


def _as_index(seg):
    """'0' / '[0]' → 0；不是下标写法返回 None。"""
    s = str(seg).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    try:
        return int(s)
    except Exception:                       # noqa: BLE001
        return None


def _actlog_module():
    """拿到 actlog 模块；拿不到返回 None（本模块要保持「可独立导入」）。"""
    try:
        from . import actlog as _al
        return _al
    except Exception:                       # noqa: BLE001
        logger.debug("jsonfix 取 actlog 失败（字段缺失事件不落账本）", exc_info=True)
    for name in ("actlog", "backend.actlog"):
        try:
            return __import__(name, fromlist=["*"])
        except Exception:                   # noqa: BLE001
            continue
    return None


def _note_miss(path, where, avail, session="") -> None:
    """把一次取字段失败写进 actlog 的 field_miss 事件。任何异常都吞掉。

    事件里必须带「实际可用 key 列表」—— 只记「缺了 a.c」没用，真正要回答的是
    「那上游到底给了什么」。落两行（工具动作行 + watch 事件行）是故意的：
    前者进 fail_kind 统计（count_fail_kind_recent 能数出「这个字段缺了几次」），
    后者进 list_events(kind="watch") / recent_watch_events，前端盯梢卡片能看到。
    """
    global _MISS_N
    avail = list(avail or [])
    try:
        key = (str(where or ""), str(path or ""))
        now = time.time()
        last = _MISS_SEEN.get(key)
        if last is not None and now - last < _MISS_GAP:
            return                          # 同一调用点同一路径，短时间内只记一次
        if _MISS_N >= _MISS_KEEP:
            return                          # 账本轮不上限就别写，宁可少记也别刷爆
        _MISS_SEEN[key] = now
        _MISS_N += 1
    except Exception:                       # noqa: BLE001
        logger.debug("jsonfix field_miss 去重失败（照常记录）", exc_info=True)
    al = _actlog_module()
    if al is None:
        return
    try:
        path_s = str(path or "")[:80]
        where_s = str(where or "")[:60]
        note = ("缺字段 %s（%s）；可用 key: %s"
                % (path_s, where_s or "未标注调用点",
                   ",".join(avail) if avail else "无（不是 dict/list 或为空）"))
        al.record(session or _MISS_SESSION, "field_miss",
                  {"path": path_s, "rel": where_s}, ok=False, result=note,
                  evidence={"fail_kind": "field_miss"})
        al.append_watch_event(session or _MISS_SESSION,
                              {"kind": "field_miss", "key": path_s,
                               "state": "miss", "note": note,
                               "count": len(avail)})
    except Exception:                       # noqa: BLE001
        logger.debug("jsonfix 写 field_miss 事件失败（账本坏了不能影响取值）",
                     exc_info=True)


def safe_get(obj, path, default=None, *, session="", where=""):
    """按点分路径从嵌套 dict / list 里取值，取不到返回 default，**绝不抛异常**。

    用法与语义::

        safe_get({'a': {'b': 1}}, 'a.b')            -> 1
        safe_get({'a': {'b': 1}}, 'a.c')            -> None   （并记 field_miss）
        safe_get({'a': {'b': 1}}, 'a.c', 0)         -> 0
        safe_get({'a': [{'b': 1}]}, 'a.0.b')        -> 1      （支持下标）
        safe_get({'a': [{'b': 1}]}, 'a[0].b')       -> 1      （下标也可贴写）
        safe_get({'a': {'k.w': 1}}, 'a["k.w"]')     -> 1      （带点的键用引号）
        safe_get(None, 'a.b', '默认')                -> '默认'

    参数：
      obj      任意对象（None / 非容器都安全）
      path     点分路径字符串，或已拆好的序列；段的比较对大小写/空白不敏感
      default  取不到时的返回值（只表示「缺失」，不表示「值是空」）
      session  field_miss 事件落到哪个账本（默认 data/actlog/field_miss.jsonl）
      where    调用点标注（如 "iterpanel.score"），排查时一眼知道是谁缺的字段

    注意：键存在但值是 None 时**返回 None**（存在 ≠ 缺失）。要让 None 也走默认值，
    调用方自己写 `safe_get(...) or default` —— 本函数不替调用方猜意图。
    """
    try:
        cur = obj
        parts = _split_path(path)
        miss_at = -1
        avail: list = []
        for i, seg in enumerate(parts):
            if isinstance(cur, dict):
                if seg in cur:
                    cur = cur[seg]
                    continue
                real = _soft_key(cur, seg)
                if real is not None:
                    cur = cur[real]
                    continue
                miss_at, avail = i, _avail_keys(cur)
                break
            if isinstance(cur, (list, tuple)):
                idx = _as_index(seg)
                if idx is not None and -len(cur) <= idx < len(cur):
                    cur = cur[idx]
                    continue
                miss_at, avail = i, _avail_keys(cur)
                break
            # 走到非容器（None / int / str …）上还想再下钻 → 这一段就算缺
            miss_at, avail = i, _avail_keys(cur)
            break
        if miss_at < 0:
            return cur                      # 全程取到（哪怕是 0 / False / ""）
        _note_miss(path, where, avail, session=session)
        return default
    except Exception:                       # noqa: BLE001
        # 连路径解析都炸了（传了奇怪对象）也不能让调用方崩 —— 兜底就兜到底
        logger.debug("safe_get 兜底失败 path=%r", path, exc_info=True)
        try:
            _note_miss(path, where, [], session=session)
        except Exception:                   # noqa: BLE001
            logger.debug("jsonfix field_miss 补记失败", exc_info=True)
        return default


def safe_list(obj, path, *, session="", where=""):
    """safe_get 的列表专用版：取到的不是 list 就当缺失（返回 []）。

    面板里全是 `for it in (d.get("items") or [])` 这种写法 —— 上游万一给了
    dict 或字符串，`or []` 挡不住，for 会遍历键/字符甚至直接抛错。这里一次说清。
    """
    v = safe_get(obj, path, None, session=session, where=where)
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if v is None:
        return []
    return []


def safe_dict(obj, path, *, session="", where=""):
    """safe_get 的 dict 专用版：取到的不是 dict 就当缺失（返回 {}）。"""
    v = safe_get(obj, path, None, session=session, where=where)
    return v if isinstance(v, dict) else {}


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
