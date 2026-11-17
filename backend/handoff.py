# -*- coding: utf-8 -*-
"""自我迭代交接卡（handoff）：重启之后，还记得刚才改了什么、聊到哪。

为什么需要
----------
改完自己的源码、重启之后是**新进程**：会话历史虽然落在 sessions/，
但长会话会被上下文预算裁掉早期部分；而"这次改了哪几个文件、为什么改"
只留在备份文件名和聊天记录里 —— 重启后的新一轮很可能答不上
"我们刚才在做什么"。

做法（四步，互不依赖）
--------------------
1) 每次自开发工具**写盘成功** → `record_change()` 追加一条
   「文件 + 一句话说明 + 时间」（同一文件连续改动会合并成一条）；
2) **每一轮对话结束** → `touch()` 把最近几轮原话**实时**落盘。
   这一步是 2026-11-09 加的：以前只在「重启前」快照，一旦进程崩溃、
   用户直接关窗口、或者前端没走 apply_restart，那次内容就一条不留，
   重启后自然"忘了刚刚做过什么"。
3) **重启前**（模型请求重启 / 用户点「立即重启」）→ `snapshot()`
   再落一次，并记下重启时间与原因；
4) 之后**每一轮**对话 → `render()` 生成一小段交接卡，注入到
   **最后一条用户消息**上（不能放进系统提示：系统提示一变，后面的
   工具定义全部作废，首字会慢十几秒 —— 见 main.py 里 _attach_live_ctx 的说明）。

存储：data/handoff/last.json（超过 KEEP_DAYS 天不再注入，文件留着不删）

markdown 安全层（2026-11-18 重写）
---------------------------------
交接卡是**拼给人看的 markdown**：任何一条用户/助手原话里出现落单的
反引号、未闭合的代码围栏、半个 `**`，都会把后面整张卡吞掉或变粗体。
2026-11-18 之前这里有 8 个函数各自做配平（_balance_fence / _inline /
_finalize / _clip / _tail / _fmt_change / _mask_code / _bold_pos），
互相调用、互相修正，结果是**修一处坏一处**：文件从 366 行涨到 846 行，
每轮修 3~5 个又引入 1~3 个。

现在收敛成**一个** `sanitize(text, cap, multiline, ellipsis)`：

    规范化空白 → 按预算截断 → 配平 → 补省略号 → 兜底

配平的三个步骤**全部是等长替换**（把落单的 ASCII 标记换成等长全角字符），
所以「配平之后长度不变」—— 这从结构上消灭了原来的死循环：
截断破坏配平 → 再配平 → 又超长 → 再截断 → 又破坏。
只有「没截断且空间足够」时才补一个闭合围栏（那时变长是安全的）。
"""
import functools
import json
import math
import os
import re
import shutil
import threading
import time

from . import config
import logging
logger = logging.getLogger(__name__)

DIR = os.path.join(config.data("data"), "handoff")
LAST = os.path.join(DIR, "last.json")

KEEP_DAYS = 14       # 超过这么久就不再注入
MAX_CHANGES = 30     # 落盘保留的改动条数
SHOW_CHANGES = 12    # 注入时最多列几条
MAX_DIALOG = 12      # 注入时最多带几轮对话（一问一答算两条）
TEXT_CAP = 200       # 单条对话注入时的字数上限
LAST_CAP = 500       # 最新一条（用户这句/你那句）放宽一点，前文才接得上
TAIL_CAP = 240       # 跨会话时补的「上次停在这里」最多多少字
LIVE_DEBOUNCE = 3    # 同一会话两次实时落盘至少隔几秒（避免每轮都写盘）

# 上一次实时落盘的时间戳（进程内，用来节流）
_LAST_TOUCH = {"session": "", "ts": 0.0}

# ★ 2026-11-18 修（数据丢失）：record_change / touch / snapshot 三个入口都是
#   「_read() → 改 d → _write(d)」这个非原子序列。_write 本身是原子替换，但
#   **整个序列不是**：两个线程同时读到旧内容，各自改一处，后写的那个会把先写的
#   整段覆盖掉。
#   实测：record_change 与 touch 并发 20 次 → **20 次全丢**。
#   修法：一把可重入锁包住整个序列（RLock 是因为 snapshot 内部会调 _dialog_digest，
#   将来若有嵌套调用也不会自锁）。
_LOCK = threading.RLock()


def _locked(fn):
    """把「读-改-写」整段串行化，避免并发写 last.json 互相覆盖。"""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _LOCK:
            return fn(*a, **kw)
    return wrapper


# =====================================================================
#  重启原因白名单（2026-11-16）
# =====================================================================
# 为什么需要：自我迭代改完代码 → 重启 → 重启后自检 → 自检又触发迭代 →
# 又重启…… 用户看到的就是「程序被反复修改、反复重启」。
# 切断点：**只有用户手动重启才自检**，自动迭代 / 冷启动一律跳过。
REASON_SELFDEV = "selfdev_iterate"   # 自我迭代改完代码后的重启 → 不自检
REASON_USER = "user_restart"         # 用户手动点「立即重启」   → 自检
REASON_COLD = "cold_start"           # 冷启动 / 关闭后重新打开   → 不自检

# 按 reason 文本猜类别时用的关键词（宁漏勿误：猜不出就当 user_restart，
# 因为「多跑一次自检」比「该查却没查」安全 —— 自检本身只查不改）。
_KIND_HINTS = (
    (REASON_SELFDEV, ("自我迭代", "自迭代", "selfdev", "self_iterate",
                      "自动迭代", "改完代码", "迭代改完", "apply_restart")),
    (REASON_COLD, ("冷启动", "cold", "关闭后", "重新打开", "首次启动",
                   "开机", "startup")),
    (REASON_USER, ("用户", "手动", "立即重启", "user_restart", "user restart")),
)


def _classify_reason(reason: str = "", kind: str = "") -> str:
    """把「重启原因」归到白名单里的三类之一。

    优先用调用方显式给的 `kind`（最准）；没给就按 reason 文本猜；
    都判不出来时返回 `user_restart` —— 宁可多跑一次自检，也不要漏查。
    """
    k = str(kind or "").strip().lower()
    if k in (REASON_SELFDEV, REASON_USER, REASON_COLD):
        return k
    t = str(reason or "").lower()
    if t:
        for name, words in _KIND_HINTS:
            if any(w.lower() in t for w in words):
                return name
    return REASON_USER


def reason_kind() -> str:
    """读交接卡里记下的「这次重启的原因类别」（没记录时返回空串）。"""
    try:
        d = _read()
        k = str(d.get("reason_kind") or "").strip().lower()
        if k in (REASON_SELFDEV, REASON_USER, REASON_COLD):
            return k
        # 老版本交接卡没有 reason_kind 字段 → 按 reason 文本补判一次
        return _classify_reason(str(d.get("reason") or ""))
    except Exception:
        return ""


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean(s, cap: int = 200) -> str:
    """只用于**会话 id** 这类不需要保留格式的短串（压成一行、硬截断）。"""
    return " ".join(str(s or "").split())[:cap]


# =====================================================================
#  markdown 安全层 —— 唯一入口 sanitize()
# =====================================================================
# 落单的 ASCII 标记一律换成**等长全角**字符：
#   `  → ｀     ``` → ｀｀｀     ** → ＊＊
# 为什么是"换"而不是"删"：2026-11-18 实测过，删掉会让内容凭空消失
# （「落单的 ` 会吞掉整张卡」→「落单的  会吞掉整张卡」；
#   note 里的 `**` 被删 → 「再配一次 （rel 里的星号…）」）。
# 为什么是"等长"：配平不改变长度，就不会再触发截断，也不会让上一轮算好的
# 下标失效 —— 这是把「配平/截断互相破坏」这个死循环从结构上掐掉的关键。
_FW_TICK = "｀"          # 行内代码
_FW_FENCE = "｀｀｀"       # 代码围栏
_FW_BOLD = "＊＊"         # 粗体


def _normalize(text: str, multiline: bool) -> str:
    """规范化空白。multiline=True 保留换行与行首缩进；否则压成一行。"""
    if not multiline:
        return " ".join(text.split())
    lines = []
    for ln in text.splitlines():
        if not ln.strip():
            if lines and lines[-1] != "":
                lines.append("")               # 连续空行合并成一个
            continue
        indent = ln[:len(ln) - len(ln.lstrip())]
        lines.append(indent + " ".join(ln.split()))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _plain_spans(s: str) -> list:
    """返回**正文**区间 [(start, end)]：既不在围栏里，也不在行内代码里。

    要求 s 的围栏已经配平（否则末尾那段其实还在围栏内）。
    """
    spans = []
    pos = 0
    for idx, part in enumerate(s.split("```")):
        if idx % 2 == 0:                       # 围栏之外
            o = pos
            for j, seg in enumerate(part.split("`")):
                if j % 2 == 0 and seg:         # 行内代码之外
                    spans.append((o, o + len(seg)))
                o += len(seg) + 1
        pos += len(part) + 3
    return spans


def _degrade_stars(s: str) -> str:
    """把**正文里**连续 ≥3 个星号换成等长全角。

    `***` / `****` 是 markdown 的粗斜体 / 分隔线，落单时会把后面整段变粗体。
    而单个 `*` 不能像 `**` 那样按奇偶配平 —— 它还是列表符号（`* item`）和
    乘号（`2*3`），乱配会把正常内容改坏。所以只处理"连写 3 个以上"这种
    确定是标记的形态，换成全角，长度不变。
    """
    if "*" not in s:
        return s
    out, last = [], 0
    for a, b in _plain_spans(s):
        out.append(s[last:a])
        out.append(re.sub(r"\*{3,}", lambda m: "＊" * len(m.group()), s[a:b]))
        last = b
    out.append(s[last:])
    return "".join(out)


def _balance_bold_block(block: str, drop_lone: bool = False) -> str:
    """在**一个围栏块内**把落单的 `**` 配平（行内代码里的不算）。

    单独拿出来是因为：粗体配对必须以围栏为界 —— 围栏前一个 `**` 和围栏后
    一个 `**` 并不构成一对（markdown 在围栏处就打断解析了）。
    """
    if "**" not in block:
        return block
    marks = []
    for a, b in _plain_spans(block):        # block 内没有围栏，只按 ` 切
        k = a
        while True:
            p = block.find("**", k, b)
            if p < 0:
                break
            marks.append(p)
            k = p + 2
    if len(marks) % 2 == 1:
        p = marks[-1]
        block = block[:p] + (block[p + 2:] if drop_lone
                             else _FW_BOLD + block[p + 2:])
    return block


def _demote_inline_fences(s: str) -> str:
    """把**不在行首**的 ``` 换成全角（等长替换）。

    markdown 只把**行首**（允许前导空格）的 ``` 当围栏，行内的 ``` 只是
    普通字符。但下游判围栏全用 count("```") / split("```") / rfind("```")，
    分不出两者 —— 于是「正文里提到 ``` 」会被当成**未闭合围栏**，
    grow=True 时补一个闭合，**凭空造出一个真围栏**。

    实测（2026-11-18 交接卡）：助手原文里有 `_balance` 仍会补 `"\\n```"`
    这么一句，_tail 从末尾截出来后判出 1 个围栏 → 补 "\\n```" →
    整卡围栏行变成 1（奇数），后面的改动清单全被吞进代码块。

    换成全角后：视觉一致、长度不变、不再是标记，下游的 count/split
    也就只对应真围栏了。
    """
    if "```" not in s and "~~~" not in s:
        return s
    out = []
    for ln in s.split("\n"):
        st = ln.lstrip()
        if st.startswith("```"):
            # 行首那 3 个是围栏，保留；但**这一行后面**再出现的 ``` 仍然不是
            # 围栏（围栏行的信息串里不会再起一个围栏），必须一并降级 ——
            # 否则 count("```") 会把它们算进去，奇偶判断跟着错。
            # 实测：'```约```python' 这种行 count=2（偶数→不补），
            # 可它其实是一个未闭合围栏，整卡围栏就变成奇数。
            i = ln.find("```")
            out.append(ln[:i + 3] + ln[i + 3:].replace("```", _FW_FENCE))
        elif st.startswith("~~~"):
            # markdown 里 ~~~ 与 ``` 等价，但本模块只配平 ```。
            # 未闭合的 ~~~ 同样会把后面整张卡吞进代码块，所以这里一律
            # 等长降级成全角（不丢内容、不变长、绝不再构成标记）。
            out.append(ln.replace("~~~", "～～～"))
        else:
            out.append(ln.replace("```", _FW_FENCE))
    return "\n".join(out)


def _demote_all_fences(s: str) -> str:
    """把**所有**围栏标记换成全角（等长）。

    用在「已经确定围栏语义不成立」的地方 —— 交接卡里每条正文前面都有
    「用户：/ 你：」前缀：第一行的 ``` 根本不在行首，可**续行缩进两格后
    的 ``` 仍然在行首**，配对就被打断，整卡围栏变成奇数，后面的改动清单
    全被吞进代码块。

    实测：body='```python\\ncode\\n```' 单独看围栏 2（偶数，配平过的），
    拼进卡片变成 1（奇数）。所以进卡片之前必须全降级。
    """
    t = str(s or "").replace("```", _FW_FENCE).replace("~~~", "～～～")
    # 4 个反引号 ` ```` ` 降级后只剩 1 个半角 → 行内反引号变奇数，这里再配一次
    # （grow=False：绝不再补围栏，本函数的前提就是"围栏语义不成立"）
    return _balance(t, grow=False, drop_lone=True)


def _balance_once(s: str, grow: bool = True, drop_lone: bool = False) -> str:
    """配平**一轮**（由 _balance 循环调用到稳定）。

    为什么需要循环：drop_lone 会**删掉**一个 `**`，这一删可能让两侧的反引号
    连起来、变成一个未闭合的围栏（实测 `'``**``xx'` → `'````xx'`，4 个反引号
    又构成一个 ```）。单次处理时"围栏配平"那一步早就过去了，于是漏网。
    每轮要么只做等长替换、要么只删 2 个字符，所以一定收敛。

    grow=True  ：围栏没闭合就**补一个闭合**（更保真，但会变长，只在没截断
                 且长度富余时用）。
    grow=False ：一律**等长替换**（截断后必须用这个，否则补完又超长）。
    drop_lone  ：粗体落单时**直接去掉**那个标记，而不是换成 ＊＊。
                 只在"这段文字确实被截断过"时用 —— 那是切在粗体中间留下的
                 开标记，后面只剩半个词，换成 ＊＊ 会输出「：＊＊代…」这种
                 像错字的东西。

    三步的顺序**必须是**：围栏 → 行内反引号 → 粗体。
    因为配平行内反引号会改变「哪些 `**` 在代码里」，粗体判定必须在它之后；
    而三步都只在"配平之后"读文本，所以**一次扫描就够，不需要循环**。
    """
    if not s:
        return ""
    # 0) 行内的 ``` 不是围栏（markdown 只认行首），先降级成全角。
    #    否则下面 count("```") 会把「正文里提到 ``` 」当成未闭合围栏，
    #    grow=True 时补一个闭合 —— 反而凭空造出一个真围栏。
    s = _demote_inline_fences(s)
    # 0.5) 围栏行本身的 `**` 也不算正文粗体（见 _demote_fence_line_bold）。
    #      必须在下面 split("```") **之前**做：拆完之后围栏行的剩余内容会
    #      落进「围栏外」那一块，和围栏外真正落单的 `**` 凑成一对。
    s = _demote_fence_line_bold(s)
    # 1) 代码围栏
    if s.count("```") % 2 == 1:
        if grow:
            s = s.rstrip() + "\n```"          # 补的是**半角**，才是真正的闭合围栏
        else:
            i = s.rfind("```")
            s = s[:i] + _FW_FENCE + s[i + 3:]
    # 2) 围栏**之外**的落单行内反引号（逐段配平，等长替换）
    parts = s.split("```")
    for i in range(0, len(parts), 2):
        # **逐行**配平：行内代码不能跨行，所以「上一行 1 个、下一行 1 个」
        # 并不是一对，两行各留一个未闭合的 `。按整块判奇偶（原来）会算成
        # 偶数放过，于是那两行各自把后半句变成等宽。
        _ls = parts[i].split("\n")
        for _k, _ln in enumerate(_ls):
            if _ln.count("`") % 2 == 1:
                _j = _ln.rfind("`")
                if _j >= 0:
                    _ls[_k] = _ln[:_j] + _FW_TICK + _ln[_j + 1:]
        parts[i] = "\n".join(_ls)
    s = "```".join(parts)
    # 3) 正文里连续 ≥3 个星号 → 等长全角
    #    `***` / `****` 在 markdown 里是粗斜体/分隔线，落单时会把后面整段变粗体，
    #    而 `*` 又**不能**像 `**` 那样按奇偶配平 —— 它还是列表符号（`* item`）
    #    和乘号（`2*3`），乱配会把正常内容改坏。所以只处理"连写 3 个以上"这一种
    #    确定是标记的形态：换成全角，长度不变，`**` 粗体完全不受影响。
    #    实测：20000 组随机输入里 264 组「粗体奇数」全部含 `***` 及以上。
    s = _degrade_stars(s)
    # 4) 正文里的落单粗体 —— **按围栏分块**配对
    #    markdown 的粗体不能跨代码围栏（围栏会打断解析），所以「围栏前一个 **、
    #    围栏后一个 **」不是一对，必须各块单独配平。
    #    实测：`_[**\n```\\**```(🙂- |**#_` 全局数得 2 个（偶数）所以不处理，
    #    但每块各 1 个，输出后围栏前后两段都变粗体。
    parts = s.split("```")
    for i in range(0, len(parts), 2):
        parts[i] = _balance_bold_block(parts[i], drop_lone)
    s = "```".join(parts)
    # 5) 落单标记换走之后，那行可能只剩空格（markdown 里无害，但不干净）
    if "\n" in s:
        s = "\n".join(("" if not ln.strip() else ln.rstrip()) for ln in s.split("\n"))
    # 6) 结尾被切成一半的 *（`**` 只切到一个）
    #    必须放在**最后**并先去掉尾部空行：原来它在第 5 步之前，遇到
    #    「…＊＊ *\n\n」这种末尾是换行的，endswith("*") 判不到，那个 `*`
    #    就留在正文里；下一轮处理时末尾没换行了又能删掉 → 不幂等
    #    （实测 24000 组里漏了 1 组）。
    while True:
        s = s.rstrip("\n")
        if s.endswith("*") and not s.endswith("**"):
            s = s[:-1]                      # 删完可能又露出一个（`…*\n\n*`），继续
            continue
        break
    return s


def _balance(s: str, grow: bool = True, drop_lone: bool = False) -> str:
    """配平到**稳定**：反复跑 _balance_once，直到结果不再变化。

    实测需要多轮的情形：drop_lone 删掉一个 `**` 之后，两侧反引号连成新的
    未闭合围栏；那一轮里"围栏配平"已经走过，必须再来一轮。
    """
    for _ in range(8):
        before = s
        s = _balance_once(s, grow, drop_lone)
        if s == before:
            break
    return s


# 头尾截断的切点字符：句末 > 句中 > 空格（英文词边界）
_SENT_END = "。！？；!?;\n"
_SENT_MID = "，、：,:"
_WORD_END = " \t"


def _dot(s: str) -> str:
    """给 s 补省略号；已经以「…」结尾就不再叠一个（否则出现「……」。"""
    s = s.rstrip()
    while s.endswith("…"):
        s = s[:-1].rstrip()
    return s + "…"


def _smart_slice(ln: str, cap: int) -> str:
    """把一行截成「头…尾」，切点回退到句子 / 单词边界。

    **为什么需要它**：早先是按字符位置硬切，于是
    `晚于 handoff.py 的 03:05:45` 被切成 `晚于 h` + `andoff.py 的 03:05:45`，
    `（.bak 恢复失败会不会崩）` 被切成 `（｀.` + `崩”` —— 两截拼起来读不通。
    这里让每个片段都以完整句子 / 单词收尾。
    """
    if cap <= 0 or len(ln) <= cap:
        return ln
    if cap <= 4:
        return ln[:cap]
    body_cap = cap - 1                      # 留给 …
    head_n = max(1, int(body_cap * 0.6))
    tail_n = max(1, body_cap - head_n)

    def _left(s, pos, floor, chars):
        """pos 往左退到「chars 里的字符」之后；退不到就返回 pos。"""
        if pos > 0 and s[pos - 1] in chars:
            return pos
        i = pos
        while i > floor:
            if s[i - 1] in chars:
                return i
            i -= 1
        return pos

    def _right(s, pos, ceil, chars):
        """pos 往右进到「chars 里的字符」之后；进不到就返回 pos。"""
        if pos > 0 and s[pos - 1] in chars:
            return pos
        i = pos
        while i < ceil:
            if s[i - 1] in chars:
                return i
            i += 1
        return pos

    p, q = head_n, len(ln) - tail_n
    for chars in (_SENT_END, _SENT_MID, _WORD_END):
        got = _left(ln, p, max(1, int(body_cap * 0.35)), chars)
        if got != p:
            p = got
            break
    for chars in (_SENT_END, _SENT_MID, _WORD_END):
        got = _right(ln, q, len(ln) - max(1, int(body_cap * 0.25)), chars)
        if got != q:
            q = got
            break
    if q <= p or p + (len(ln) - q) + 1 > cap:   # 相接 / 超长 → 老实硬切
        p, q = head_n, len(ln) - tail_n
    if q <= p:
        return ln[:body_cap].rstrip("…").rstrip() + "…"
    head = ln[:p].rstrip()
    tail = ln[q:].lstrip()
    while head.endswith("…"):          # 原文自带省略号 → 不再叠一个
        head = head[:-1].rstrip()
    while tail.startswith("…"):
        tail = tail[1:].lstrip()
    return head + "…" + tail


def _cut(body: str, budget: int, per_line_cap: int = 0):
    """按**行**截断到 budget 字内，尽量不切断行（列表/表格/围栏才不会被撕开）。

    per_line_cap：单行最多能占多少字（0 = 不限制）。
    **为什么需要它**：实测一条回复的第一段就有 2293 字，而 budget 只有 1200，
    于是第一行把预算全吃掉、后面 45 行（标题、表格、结论）**全部丢弃** ——
    24 条对话合计只保留 38.7%，8 条只剩 25~31%，其中两条 40 多行只剩 1 行。
    交接卡本来就是要交代"刚才聊了什么"，结果只剩开头一句客套话。
    给单行设上限后，同样 1200 字能看到 3~5 行结构，信息量完全不同。

    返回 (text, cut)。
    """
    if budget <= 0:
        return "", True
    if len(body) <= budget:
        return body, False
    lines = body.split("\n")
    if per_line_cap and per_line_cap > 1:
        fixed = []
        for ln in lines:
            if len(ln) <= per_line_cap:
                fixed.append(ln)
                continue
            s = ln.strip()
            # 表格行 / 围栏行 / 缩进列表：只截前缀。截尾会把表格最后一列、
            # 列表层级截掉，比丢掉后半句更糟。
            if s.startswith("|") or s.startswith("```") or s.startswith("~~~"):
                fixed.append(_dot(ln[:per_line_cap - 1]))
                continue
            # 普通长段落：**头尾都留**。实测 12 条回复的前 200 字全是
            # 「我先核对…」这种开场白，真正的结论（查出了什么、改了什么）
            # 都在尾部 —— 只留前缀等于把有效信息全丢了。
            fixed.append(_smart_slice(ln, per_line_cap))
        lines = fixed
    out, total = [], 0
    for ln in lines:
        add = len(ln) + (1 if out else 0)
        if total + add <= budget:
            out.append(ln)
            total += add
            continue
        room = budget - total - (1 if out else 0)
        if room > 1:
            seg = ln[:room]
            if len(ln) > room:
                seg = _dot(ln[:room - 1])
            out.append(seg)
        break
    return "\n".join(out).rstrip(), True


def sanitize(s, cap: int = None, multiline: bool = True,
             ellipsis: bool = True, drop_lone: bool = False,
             per_line_cap: int = 0) -> str:
    """**唯一**的 markdown 安全入口：规范化 → 截断 → 配平 → 补省略号 → 兜底。

    参数
    ----
    cap       : 字数上限（None = 不限制）
    multiline : True 保留换行与缩进（对话正文）；False 压成一行（原因、路径、说明）
    ellipsis  : 截断时是否补「…」。False 用在"后面还会再截一次"的地方
                （_dialog_digest），否则注入时会出现「……」两个省略号。
    drop_lone : 粗体落单时直接去掉标记（见 _balance）
    """
    text = str(s or "")
    if not text.strip():
        return ""
    body = _normalize(text, multiline).strip("\n")
    if not body:
        return ""

    cut = False
    if cap and len(body) > cap:
        # 只有**真的超了**才截。原来无条件走 _cut(cap - 预留省略号)，
        # 于是长度正好等于 cap 的文本第二次进来时会被多切一刀、再加个「…」——
        # 幂等性实测 17/3000 组不稳定就是这个原因。
        reserve = 1 if (ellipsis and cap > 1) else 0
        body, _ = _cut(body, cap - reserve, per_line_cap=per_line_cap)
        cut = True

    # 补闭合围栏会变长，只有「多行 且 长度富余」时才敢用。
    # 单行上下文（原因、rel、note）**永远不补** —— 补出来的 "\n```" 会把一行
    # 撕成两行，整卡围栏数还变成奇数；那里只能等长替换成全角。
    # 实测：这正是 _inline 当初单独存在的原因，重写时收进这里，由参数决定。
    spare = (1 if (cut and ellipsis) else 0)
    grow = multiline and ((not cap) or (len(body) + 4 + spare <= cap))
    body = _balance(body, grow=grow, drop_lone=drop_lone or cut)

    if cut and ellipsis:
        body = _dot(body)

    # 兜底：等长替换理论上不会变长，但 grow 补围栏 + 省略号可能顶出去 1~2 字
    if cap and len(body) > cap:
        body = body[:cap]
        body = _balance(body, grow=False, drop_lone=True)
        if len(body) > cap:
            body = body[:cap]
    # 最后再规范化一次，让输出回到**规范形式**，这样「同一段文本处理两次」
    # 结果一定相同（幂等）。实测要在这里兜的有两种：
    #   · drop_lone 删掉一个 ** 之后留下两个连续空格；
    #   · 补围栏/截断之后出现连续空行。
    # 两种都会在第二次进来时被 _normalize 合并，于是 a != b。
    body = _normalize(body, multiline).strip("\n")
    # 规范化的副作用：压掉尾部空白后会**露出新的孤立 `*`**（`(* 　*` 删掉最后一个
    # 之后，`(*` 里的那个就跑到末尾了）。_balance 里那一步在它之前，所以这里再走一遍。
    while body.endswith("*") and not body.endswith("**"):
        body = body[:-1].rstrip()
    return body


# ---- 兼容旧调用点的薄包装（全部指向 sanitize，不再有第二份实现）----
def _clip(s, cap: int = 200, ellipsis: bool = True,
          per_line_cap: int = 0) -> str:
    """多行文本注入用（保留换行、缩进、段落空行）。"""
    return sanitize(s, cap, multiline=True, ellipsis=ellipsis,
                    per_line_cap=per_line_cap)


def _inline(s, cap: int = 160) -> str:
    """单行上下文（原因、改动清单的 rel/note）用：压成一行 + 配平。

    单行里没法承载代码围栏，所以这里的"落单围栏"也换成全角（等长替换），
    而不是补一个 "\n```" 把这一行撕成两行。
    """
    return sanitize(s, cap, multiline=False)


def _balance_fence(s: str, drop_lone: bool = False) -> str:
    """配平一段可能含未闭合围栏/反引号/粗体的文本（不截断）。"""
    return sanitize(s, None, multiline=True, drop_lone=drop_lone)


def _demote_fence_line_bold(s: str) -> str:
    """把**围栏行**（``` 开头那行）里的 `**` 换成全角（等长替换）。

    markdown 在围栏行只认 info string，那里的 `**` 不会渲染成粗体。
    不剔掉的话，`split("```")` 会把它归进「围栏外」的块，和围栏外真正
    落单的 `**` 凑成一对 —— 那个落单的就永远配不平。
    实测：`'```a\\n```** x\\n正文**'` → 正文那个 `**` 一直留成半角，
    输出后它把后面整段变粗体。
    等长替换，所以调用方拿到的是等长字符串，下标不会错位。
    """
    if "**" not in s:
        return s
    ls = s.split("\n")
    for i, ln in enumerate(ls):
        if ln.lstrip().startswith("```") and "**" in ln:
            ls[i] = ln.replace("**", _FW_BOLD)
    return "\n".join(ls)


def _bold_pos(s: str) -> list:
    """返回**正文里**（行内代码 / 围栏之外）所有 `**` 的起始下标。

    先做等长配平再统计：这样"哪些 `**` 在代码里"才是确定的，
    不会因为行内代码没配平而把代码里的 `**` 当成正文粗体。
    """
    t = str(s or "")
    if not t:
        return []
    t = _demote_inline_fences(t)
    t = _demote_fence_line_bold(t)
    if t.count("```") % 2 == 1:
        i = t.rfind("```")
        t = t[:i] + _FW_FENCE + t[i + 3:]
    parts = t.split("```")
    for i in range(0, len(parts), 2):
        if parts[i].count("`") % 2 == 1:
            j = parts[i].rfind("`")
            if j >= 0:
                parts[i] = parts[i][:j] + _FW_TICK + parts[i][j + 1:]
    t = "```".join(parts)
    t = _degrade_stars(t)
    pos = []
    for a, b in _plain_spans(t):
        k = a
        while True:
            p = t.find("**", k, b)
            if p < 0:
                break
            pos.append(p)
            k = p + 2
    return pos


def _tail(text: str, cap: int = TAIL_CAP) -> str:
    """取一段文字的最后 cap 字（跨会话时补「上次停在这里」用）。

    与 sanitize 的区别：它从**末尾**截（要的是"停在哪"），并且续行缩进
    两格，好嵌在「· 那个会话最后停在这里：」这一行下面。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    if len(t) > cap:
        t = t[-cap:]
        nl = t.find("\n")
        if 0 <= nl < len(t) - 1:            # 别从半行开始
            t = t[nl + 1:]
        t = "…" + t.lstrip("\n")
    # 从末尾截会把开头的 `**` 切掉、只剩结尾那个闭合标记 → 后面整段变粗体。
    # 那是纯语法标记，直接去掉（drop_lone）。
    # 这里**不能** grow：_tail 的结果要拼在「· 那个会话最后停在这里：」后面，
    # 第一行有前缀、续行缩进两格 —— 补出来的闭合围栏会单独成行、配对不成立。
    t = _balance(t, grow=False, drop_lone=True)
    if "\n" in t:
        t = "\n".join(("  " + ln) if ln else "" for ln in t.split("\n"))
    return t


def _similar(a: str, b: str) -> bool:
    """两条消息是不是「同一件事被记了两遍」。

    中断后重试会产生两条内容高度重复的 assistant 消息（实测：前 40 字
    完全相同，只是长度差几十字）。原样列两遍会让模型以为做了两轮。
    """
    if a == b:
        # 短消息重复往往是「用户催了两次 / 确认了两次」，是真实轮次，
        # 合并会让轮数少算（实测连发两次「继续」被并成一条 → 2 轮显示成 1 轮）。
        # 但 12 字以上完全相同，基本只可能是「中断后重试」，应该合并。
        return len(a) >= 12
    if not a or not b:
        return False
    # 一条正好是另一条的开头 → 中断后重试/续写，几乎肯定同一件事。
    short, lng = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 12 and lng.startswith(short):
        return True
    # 否则要求「前 80 字全同」；短消息只开头像不算（「好的，我来看看」这种
    # 开场白极易撞车，合并会把真正不同的两轮吞掉一条）
    n = min(len(a), len(b), 80)
    if n < 40:
        return False
    return a[:n] == b[:n]


# =====================================================================
#  读写
# =====================================================================
def _read() -> dict:
    try:
        with open(LAST, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
        raise ValueError("last.json 顶层不是 dict")
    except FileNotFoundError:
        return {}
    except Exception:
        # 2026-11-18 修（数据丢失级）：原来这里直接 return {}，而调用方都是
        # 「读 → 改 → 写」—— 文件一旦损坏（手工编辑出错、断电写一半、磁盘异常），
        # 下一次 record_change/touch 就会拿空字典写回，把整份历史无声覆盖掉。
        # 现在：先把坏文件改名隔离（留着人工抢救），再尝试上一次的 .bak。
        try:
            bad = LAST + ".corrupt-" + time.strftime("%Y%m%d_%H%M%S")
            os.replace(LAST, bad)
            logger.warning("交接卡 last.json 无法解析，已隔离为 %s", os.path.basename(bad))
        except Exception:
            logger.debug("silent-except", exc_info=True)
        bak = LAST + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    raise ValueError("last.json.bak 顶层不是 dict")
                logger.warning("交接卡已从 last.json.bak 恢复")
                # 2026-11-18 修（数据丢失级，上一版只修了一半）：原来这里
                # 只把 d 返回给调用方，**没写回磁盘**。而 LAST 已被上面改名
                # 隔离、此刻并不存在 —— 于是下一次 _read 走 FileNotFoundError
                # 直接 return {}，恢复等于白做，接着 record_change 就拿空字典
                # 覆盖。实测：24 条对话 / 30 条改动在第 2 次读取后全部消失。
                _write(d)
                return d
            except Exception:
                # .bak 自己也坏了：隔离它，否则每次损坏都拿同一份坏备份去试，
                # 永远恢复不出来，还会把 corrupt 文件堆在原地。
                try:
                    bad2 = bak + ".corrupt-" + time.strftime("%Y%m%d_%H%M%S")
                    os.replace(bak, bad2)
                    logger.warning("交接卡 last.json.bak 也无法解析，已隔离为 %s",
                                   os.path.basename(bad2))
                except Exception:
                    logger.debug("silent-except", exc_info=True)
        return {}


def _write(d: dict) -> None:
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = LAST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST)          # 原子替换，避免写一半断电把交接卡写坏
        # 写成功后同步留一份 .bak：放在**写成功之后**，这样首次写入也有备份
        # （实测原来「先备份再写」的写法，第一次写时 LAST 还不存在，压根没有 .bak）。
        # .bak 永远等于最后一次成功写入的状态。
        try:
            shutil.copyfile(LAST, LAST + ".bak")
        except Exception:
            logger.debug("silent-except", exc_info=True)
    except Exception:
        # 写失败不能影响正常对话/改代码，但要在日志里留痕（磁盘满/只读时
        # 用 debug 级别会完全看不到，用户只会觉得「交接卡怎么不更新了」）
        logger.warning("交接卡写入失败", exc_info=True)


# =====================================================================
#  1) 记录一次改动（自开发工具写盘成功后调用）
# =====================================================================
_PATH_JUNK = re.compile(r"[`｀*＊]")


def _safe_rel(rel, cap: int = 120) -> str:
    """rel 是**文件路径**：反引号/星号本来就非法，而且外层还要用 ` 包一层。

    这里全部剔除（含全角），避免 rel 的标记和 note 的 **粗体** 混在一起
    （实测 note="**重点** 说明" 因为整行配平把闭合 ** 删成了 "**重点 说明"）。
    """
    return _PATH_JUNK.sub("", _inline(rel, cap)).strip()


@_locked
def record_change(rel: str, note: str = "", added: int = 0, removed: int = 0,
                  act: str = "write") -> None:
    rel = _safe_rel(rel, 120)
    if not rel:
        return
    d = _read()
    ch = d.get("changes")
    if not isinstance(ch, list):
        ch = []
    note = _inline(note, 160)
    try:
        added = int(added or 0)
        removed = int(removed or 0)
    except Exception:
        added = removed = 0

    # 同一个文件连续改了好几次 → 合并成一条（否则清单里全是同一个文件名）
    if ch and isinstance(ch[-1], dict) and ch[-1].get("rel") == rel:
        it = ch[-1]
        try:
            it["added"] = int(it.get("added") or 0) + added
            it["removed"] = int(it.get("removed") or 0) + removed
        except Exception:
            logger.debug("silent-except", exc_info=True)
        if note:
            it["note"] = note
        it["act"] = act
        it["time"] = _now_str()
    else:
        ch.append({"rel": rel, "note": note, "added": added,
                   "removed": removed, "act": act, "time": _now_str()})

    d["changes"] = ch[-MAX_CHANGES:]
    d["updated"] = time.time()
    _write(d)


def record_from_tool(r: dict, act: str = "write", note: str = "") -> None:
    """给 tools.py 的 `_selfdev_ui` 用的**一行入口**：自己吞掉所有异常。

    交接卡是锦上添花，绝不能因为它让"改代码"这个主流程报错。
    """
    try:
        d = (r or {}).get("diff") or {}
        record_change(str((r or {}).get("rel") or ""), note=note, act=act,
                      added=d.get("added") or 0, removed=d.get("removed") or 0)
    except Exception:
        logger.debug("silent-except", exc_info=True)


# =====================================================================
#  2) 对话摘要
# =====================================================================
def _dialog_digest(session: str) -> list:
    """从会话历史里取最近几轮，压成「角色 + 一段话」。"""
    if not session:
        return []
    try:
        from . import sessions as _ss
        msgs = _ss.get_messages(session) or []
    except Exception:
        return []
    out = []
    # 多取一些：下面要去重，去掉重复后还要凑够 MAX_DIALOG*2 条
    for m in msgs[-(MAX_DIALOG * 4):]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        # 这里**不补省略号**（否则注入时再截一次会出现「……」）
        # per_line_cap：一条回复的第一段常有 2000+ 字，不限的话它把 1200 字
        # 预算全吃掉，后面 40 多行（标题/表格/结论）全丢 —— 实测只保留 38.7%。
        text = _clip(m.get("content"), 1200, ellipsis=False, per_line_cap=400)
        if role not in ("user", "assistant") or not text:
            continue
        # 同角色 + 内容高度重复 → 只留一条（长的），避免同一件事报两遍
        if out and out[-1]["role"] == role and _similar(out[-1]["text"], text):
            if len(text) > len(out[-1]["text"]):
                out[-1] = {"role": role, "text": text}
            continue
        out.append({"role": role, "text": text})
    return out[-(MAX_DIALOG * 2):]


@_locked
def touch(session: str = "", reason: str = "") -> None:
    """**每一轮对话结束后**调用：把最近几轮原话实时写进交接卡。

    为什么要实时写：重启不一定发生在「点立即重启」那一刻 —— 崩溃、
    直接关窗口、断电都会让进程凭空消失，那一刻的对话就再也写不下来了。
    这里每次落盘只多几毫秒（带 3 秒节流），换来的是「无论怎么断，前文都在」。
    """
    try:
        if not session:
            return
        now = time.time()
        if (_LAST_TOUCH.get("session") == session
                and now - float(_LAST_TOUCH.get("ts") or 0) < LIVE_DEBOUNCE):
            return
        dlg = _dialog_digest(session)
        if not dlg:
            return
        d = _read()
        d["dialog"] = dlg
        d["dialog_session"] = _clean(session, 64)
        d["session"] = _clean(session, 64)
        d["ts"] = now
        d["updated"] = now
        d["live_at"] = _now_str()
        if reason:
            d["reason"] = _inline(reason, 120)
        _write(d)
        _LAST_TOUCH["session"] = session
        _LAST_TOUCH["ts"] = now
    except Exception:
        logger.debug("silent-except", exc_info=True)


# =====================================================================
#  3) 重启前快照
# =====================================================================
@_locked
def snapshot(session: str = "", reason: str = "", kind: str = "") -> dict:
    """重启前调用：把「改动清单 + 最近几轮对话」落盘。

    返回落盘后的内容（方便调用方打日志）。任何异常都不往外抛 ——
    交接卡是"锦上添花"，绝不能因为它导致重启失败。

    ★ 2026-11-16：`reason` 现在会被 bootaudit 当作「这次要不要自检」的判据，
      所以除了自由文本，还会额外落一个**结构化**的 `reason_kind`：
        · selfdev_iterate —— 自我迭代改完代码后的重启（**不自检**）
        · user_restart    —— 用户手动点「立即重启」（**自检**）
        · cold_start      —— 冷启动 / 关闭后重新打开（**不自检**）
      判据优先看调用方显式传入的 `kind`，其次按 reason 文本猜。
    """
    try:
        d = _read()
        if session:
            d["session"] = _clean(session, 64)
            d["dialog_session"] = _clean(session, 64)
        if reason:
            d["reason"] = _inline(reason, 120)
        d["reason_kind"] = _classify_reason(reason, kind)
        d["ts"] = time.time()
        d["restarted_at"] = _now_str()
        dlg = _dialog_digest(str(d.get("session") or ""))
        if dlg:
            d["dialog"] = dlg
        _write(d)
        return d
    except Exception:
        return {}


# =====================================================================
#  4) 每轮注入
# =====================================================================
def _fmt_change(it: dict) -> str:
    """一条改动 → 一行 markdown。

    三个字段各自 sanitize 之后拼起来就是干净的：
      · rel 外层由这里加 ` ` 包住，且 rel 自己已剔掉所有反引号/星号；
      · note 里的 ` 和 ** 已配平成偶数；
    所以**不需要**再对整行做一次兜底配平（旧实现有那一步，反而会误伤
    note 里正常的 **粗体**）。
    """
    rel = _safe_rel(it.get("rel"), 120)
    note = _inline(it.get("note"), 160)
    try:
        added = int(it.get("added") or 0)
        removed = int(it.get("removed") or 0)
    except Exception:
        added = removed = 0
    line = "- `%s`" % rel
    if added or removed:
        line += "（+%d -%d 行）" % (added, removed)
    if note:
        line += "：%s" % note
    # 只留「月-日 时:分」。原来直接 t[5:16] 硬切，实测 time 是 "11-18 05:00"
    # （没有年份）时会切出 "[ 05:00]" —— 前面多一个空格，看着像时间丢了。
    # 改成按正则取，取不到就不显示时间（比显示一个错时间好）。
    m = re.search(r"(\d{1,2}-\d{1,2})[T\s]+(\d{1,2}:\d{2})", str(it.get("time") or ""))
    if m:
        line += "　[%s %s]" % (m.group(1), m.group(2))
    return line


def _last_assistant(dialog: list) -> str:
    for m in reversed(dialog):
        if m.get("role") == "assistant" and str(m.get("text") or "").strip():
            return str(m.get("text") or "").strip()
    return ""


def _dedup(dialog: list) -> list:
    """同角色 + 内容高度重复 → 只留一条（长的）。

    落盘时已经去过一次，但**旧数据**是旧代码写的、可能还留着「中断 + 重试」
    的两条重复，注入前再兜一次。
    """
    out = []
    for m in dialog:
        t = str(m.get("text") or "")
        if (out and out[-1].get("role") == m.get("role")
                and _similar(str(out[-1].get("text") or ""), t)):
            if len(t) > len(str(out[-1].get("text") or "")):
                out[-1] = m
            continue
        out.append(m)
    return out


def _fold(body: str, orig_text: str, limit: int = 24) -> str:
    """长代码块/长列表折叠：只显示前 limit 行，并说明原文一共多少行。"""
    lines = body.split("\n")
    if len(lines) <= limit:
        return body
    head = _demote_inline_fences("\n".join(lines[:limit]))
    orig = len(str(orig_text or "").split("\n"))
    note = "…（原文共 %d 行，已折叠显示前 %d 行）" % (max(orig, len(lines)), limit)
    # 折叠点可能正落在 ``` 代码块里 → 先补闭合围栏，提示放到围栏**外面**
    if head.count("```") % 2 == 1:
        head += "\n```"
    return head + "\n" + note


def _ts_ok(ts) -> bool:
    """这张卡还该不该注入（时间戳必须可信）。"""
    try:
        ts = float(ts)
    except Exception:
        return False
    now = time.time()
    # 原来只判 `not ts` 和「太久以前」。实测 ts=NaN / Infinity / 10**20 时，
    # `now - ts > KEEP_DAYS*86400` 全部为 False（NaN 比较恒 False，inf 相减得
    # -inf），于是**永远不过期**，白占上下文。这里要求：有限数、为正、
    # 且不超过当前时间 1 天（时钟被改到未来同样会永不过期）。
    if not math.isfinite(ts) or ts <= 0 or ts > now + 86400:
        return False
    return (now - ts) <= KEEP_DAYS * 86400


def _split_changes(changes: list, restarted: str):
    """按「重启时间」把改动分成 重启前 / 重启之后 两组。

    原来一律写「那次重启前改过的文件」，但清单取的是「最近 SHOW_CHANGES 条」，
    里面混着重启之后才做的改动（实测真实卡片：上次重启 02:34:17，清单里却有
    02:36/02:37/02:41 共 6 条）。读卡的人会以为这些都已经生效过。

    另外：两组都有时**固定给「重启前」留 4 个位置** —— 否则重启后又改了 12 条
    以上时，「重启前」那组被整个挤掉，交接卡最该交代的信息反而没了。
    """
    pre, post = [], []
    for c in changes:
        t = str(c.get("time") or "")
        if restarted and t and t > restarted:
            post.append(c)                 # 格式固定 "YYYY-MM-DD HH:MM:SS"，可直接比
        else:
            pre.append(c)
    if pre and post:
        return pre[-4:], post[-(SHOW_CHANGES - min(4, len(pre))):]
    return pre[-SHOW_CHANGES:], post[-SHOW_CHANGES:]


def render(session: str = "") -> str:
    """生成要注入的交接卡文本；没有可注入的内容就返回空串。"""
    d = _read()
    if not d:
        return ""
    if not _ts_ok(d.get("ts")):
        return ""                          # 太久以前的重启，别再占上下文

    changes = [c for c in (d.get("changes") or []) if isinstance(c, dict)]
    dialog = [m for m in (d.get("dialog") or []) if isinstance(m, dict)]
    if not changes and not dialog:
        return ""

    restarted = str(d.get("restarted_at") or "")
    # ★ 2026-11-18 修（注入）：这是**唯一**没走 sanitize 的入口。原来直接
    #   "%s" % restarted 拼进标题，实测 restarted_at 里带一个换行 + ``` 时，
    #   标题被撕成两行、整卡围栏行变 1（奇数），**后面整张卡全被当代码块吞掉**。
    #   live_at 同理。标题是单行上下文，用 _inline。
    if restarted:
        lines = ["【自我迭代交接卡 · 上次重启 %s】" % _inline(restarted, 40)]
    else:
        lines = ["【自我迭代交接卡 · 上次对话 %s（没有重启记录，可能是异常退出）】"
                 % _inline(d.get("live_at") or "时间未知", 40)]
    if d.get("reason"):
        lines.append("原因：%s" % _inline(d.get("reason"), 120))

    # 这段前文是不是**当前这段对话**自己的 —— 是的话，它就是"刚才"，权重更高
    same = bool(session) and str(d.get("dialog_session") or "") == str(session)
    if dialog:
        dialog = _dedup(dialog)
        # 轮数按**真实用户消息条数**算。以前用 len(dialog)//2，一旦有条目
        # 被去重或多出一条 assistant，就会显示成 12 轮而实际只有 11 轮。
        rounds = sum(1 for m in dialog if m.get("role") == "user") or 1
        lines.append("· %s（最近 %d 轮）："
                     % ("本会话刚才在聊" if same else "另一个会话当时在聊", rounds))
        n = len(dialog)
        for i, m in enumerate(dialog):
            who = "用户" if m.get("role") == "user" else "你"
            cap = LAST_CAP if i >= n - 2 else TEXT_CAP
            raw = str(m.get("text") or "")
            # per_line_cap：不设的话第一条长段落就能占满 200 字预算，
            # 卡片里每条回复只剩开头一句，结论/验证结果全看不到。
            body = _fold(_clip(raw, cap, per_line_cap=max(60, cap // 2)), raw)
            # 进卡片前把围栏全降级：这一行前面要加「用户：/ 你：」前缀，
            # 第一行的 ``` 就不在行首了，可续行缩进后仍在行首 —— 配对被打断，
            # 整卡围栏变奇数、后面全被吞。实测 body 单独 2 个围栏、进卡变 1 个。
            body = _demote_all_fences(body)
            if "\n" in body:
                # 续行缩进对齐；空行留空，别写成 "  "（markdown 里两个空格＝硬换行）
                body = "\n".join(("  " + ln) if ln else "" for ln in body.split("\n"))
            lines.append("  %s：%s" % (who, body))

    # 同一段会话里，最后那句助手回复上面已经列过了，再补一遍纯属浪费上下文；
    # 只有「另一个会话」时才补一句结尾，帮模型接上别人那条线。
    last = _last_assistant(dialog)
    if last and not same:
        lines.append("· 那个会话最后停在这里：%s"
                     % _demote_all_fences(_tail(last)))

    if changes:
        pre, post = _split_changes(changes, restarted)
        if pre:
            head = ("· 那次重启前改过的文件（最近 %d 个）：" % len(pre)) if restarted \
                else ("· 最近改过的文件（最近 %d 个）：" % len(pre))
            lines.append(head)
            lines.extend(_fmt_change(c) for c in pre)
        if post:
            lines.append("· 重启之后又改过的文件（最近 %d 个）：" % len(post))
            lines.extend(_fmt_change(c) for c in post)

    lines.append(
        "⚠️ 这张卡是**上一次会话的背景**，用来接上前文（用户可能正在接着问刚才的事）；"
        "和用户当前这句话冲突时**以当前为准**。不要主动复述这张卡，"
        "除非用户问「刚才聊到哪 / 改了些什么」。")
    return "\n".join(lines)
