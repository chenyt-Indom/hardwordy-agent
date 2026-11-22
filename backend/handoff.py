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
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid

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


_MD_ESC = {"`": "｀", "*": "＊", "_": "＿", "[": "［", "]": "］",
           "#": "＃", "|": "｜", "~": "～", "<": "＜", ">": "＞"}
_MD_ESC_RE = re.compile(r"\\([`*_\[\]#|~<>])")


def _unescape_md(text: str) -> str:
    """把 markdown 转义（`\\``、`\\*`）换成**等长全角**。

    为什么必须做：转义符本身不参与 markdown 解析，但 `str.count("`")`
    会把 `\\`` 里的反引号也数进去，于是**奇偶判错、配平失效**。
    实测（用户贴的那张卡第 116 行）：
        原文 `…角度：**\\`last.jso…`
        有效反引号 5 个（奇数，真正落单），但 count 出来 8 个（偶数）
        → 配平放过 → 后面整段被当成行内代码。
    换成等长全角后：内容一个字不丢（｀ 就是反引号的样子），计数正确。
    """
    if "\\" not in text:
        return text
    return _MD_ESC_RE.sub(lambda m: _MD_ESC[m.group(1)], text)


_LITERAL_NL_RE = re.compile(r"\\r\\n|\\n")

# `\n` 被引号包住时是在**引用这个符号本身**，不是换行意图。实测真实会话：
#     原文在解释「字面 \n」本身      ← 还原成真换行会把「字面」和「」拆成两行
_QUOTE_OPEN = "「『（(【["
_QUOTE_CLOSE = "」』）)】]\"'"


def _nl_repl(m):
    """字面 `\\n` → 真换行；但被引号包住时不还原（那是在说这个符号）。"""
    s, a, b = m.string, m.start(), m.end()
    if (a > 0 and s[a - 1] in _QUOTE_OPEN) or (b < len(s) and s[b] in _QUOTE_CLOSE):
        return m.group(0)
    return "\n"


def _unescape_newline(text: str) -> str:
    """把**正文里**的字面 `\\n`（反斜杠 + n 两个字符）还原成真换行。

    为什么：模型输出偶尔把换行写成字面 `\\n`。实测真实会话 msg[17]：
        日志：24\\n\\n这张卡是…应用自己生成的「自我迭代交接卡」
    卡片上就挤成一行，读者看到的是 `\\n\\n` 四个字符，读不通。

    只动**正文**：围栏内、行内代码内的 `\\n` 是正当内容（讨论正则 / JSON /
    转义序列时到处是），一个字都不能改。实测 msg[3] 在围栏里、
    msg[5] 在行内代码里，都必须原样保留。
    """
    if "\\n" not in text:
        return text
    out, in_fence = [], False
    for ln in text.split("\n"):
        st = ln.lstrip()
        if st.startswith("```"):               # 只有**真围栏**才切换状态
            in_fence = not in_fence
            out.append(ln)
            continue
        # 围栏内是代码；含全角围栏（已降级）的行是代码示例 —— 两种都不动。
        # 注意：全角围栏**不切换 in_fence**。原来让它切换，于是行内出现的
        # `｀｀｀`（被跳过、不切换）和行首出现的 `｀｀｀`（切换）状态不一致，
        # 末尾正文被误判成围栏内容、字面 \n 不还原（实测 seed=1 第 4670 组）。
        if in_fence or "```" in ln or "｀｀｀" in ln or "～～～" in ln:
            out.append(ln)
            continue
        segs = ln.split("`")                   # 行内代码之间才是正文
        for j in range(0, len(segs), 2):
            segs[j] = _LITERAL_NL_RE.sub(_nl_repl, segs[j])
        out.append("`".join(segs))
    return "\n".join(out)


def _normalize(text: str, multiline: bool) -> str:
    """规范化空白。multiline=True 保留换行与行首缩进；否则压成一行。"""
    text = _unescape_md(text)
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
    # 按**段落**分段配平：CommonMark 里空行会打断段落，强调（`**`）不能跨段落
    # 配对。原来按整块数 marks，`**a\n\nb**` 数出 2 个（偶数）就放过，
    # 实际两段各落单一个，渲染时各留半截标记。
    # 实测（2026-11-18 模糊测试）：'**~~~\n\n`|　**\*' 整块 2 个（偶数→放过），
    # 但两段各 1 个。7/30000 组失败全部是这一种。
    # 同段落内的跨行粗体（`**a\nb**`）不受影响（没有空行 → 不分段）。
    paras = block.split("\n\n")
    for i, para in enumerate(paras):
        if "**" not in para:
            continue
        marks = []
        for a, b in _plain_spans(para):     # para 内没有围栏，只按 ` 切
            k = a
            while True:
                p = para.find("**", k, b)
                if p < 0:
                    break
                marks.append(p)
                k = p + 2
        if len(marks) % 2 == 1:
            p = marks[-1]
            paras[i] = para[:p] + (para[p + 2:] if drop_lone
                                   else _FW_BOLD + para[p + 2:])
    return "\n\n".join(paras)


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
# ★ 只留**全角**标点。原来还含半角 `,` `:`，于是时间 `03:05:45` 里的冒号
#   被当成句子边界，尾段从 `05:45，因此…` 开始（半截时间，读不通）。
#   半角标点在中英混排/代码里到处是，不能当句子边界用。
_SENT_MID = "，、："
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

    p = head_n
    for chars in (_SENT_END, _SENT_MID, _WORD_END):
        got = _left(ln, p, max(1, int(body_cap * 0.35)), chars)
        if got != p:
            p = got
            break

    # ★ 尾段必须从**完整句子**开始。原来 _right 只允许往右挪 body_cap*0.25，
    #   挪不到就退回硬切 —— 实测 `backend/_feature_tests/test_handoff_card.py`
    #   被切成 `s/test_handoff_card.py`（从路径中间开始），
    #   `（.bak 恢复失败会不会崩）` 被切成 `（｀.` + `崩”`。
    #   现在：允许牺牲最多一半尾段长度去对齐，只认句子符（含换行）；
    #   仍然对不齐 → **放弃尾段**，预算全给头段（宁可只有开头，也不给半截）。
    q = len(ln) - tail_n
    q2 = None
    for chars in (_SENT_END, "\n"):
        got = _right(ln, q, min(len(ln), q + tail_n), chars)
        if got != q:
            q2 = got
            break
    if q2 is None:
        got = _right(ln, q, min(len(ln), q + tail_n), _SENT_MID)
        if got != q:
            q2 = got
    if q2 is None or q2 <= p or q2 >= len(ln) - 2:
        # 尾部没有干净的起点 → 只留头段（按句子对齐到 cap）
        got = _left(ln, body_cap, max(1, int(body_cap * 0.5)), _SENT_END)
        if got == body_cap:
            got = _left(ln, body_cap, max(1, int(body_cap * 0.5)), _SENT_MID)
        head = ln[:got] if got != body_cap else ln[:body_cap]
        while head.endswith("…"):
            head = head[:-1].rstrip()
        return head.rstrip() + "…"
    q = q2
    if p + (len(ln) - q) + 1 > cap:
        # 对齐后超长 → **缩短头段**去容纳干净的尾段。
        # 原来这里退回 `p, q = head_n, len(ln)-tail_n`（双硬切），
        # 于是刚对齐好的尾段又被切回半句（实测 `…05:45，因此`）。
        room = cap - 1 - (len(ln) - q)
        if room >= 8:
            got = _left(ln, room, max(1, room // 2), _SENT_END)
            if got == room:
                got = _left(ln, room, max(1, room // 2), _SENT_MID)
            p = got if got != room else room
        else:
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


def _cut_table_row(ln: str, cap: int):
    """表格行超长时**按单元格**截，保住列数。

    实测：`| 1 | 很长的描述… | 证据… |` 被前缀硬切后成了 `| 1 | 很长的描述…`
    —— 只剩 2 个单元格、末尾没有 `|`，而表头是 3 列，markdown 渲染时这一行
    缺列错位，整张表看起来是坏的。

    返回 None 表示「这个预算放不下所有列」——这一行整行丢弃，
    比输出一个列数不对的行要好。
    """
    s = ln.strip()
    if not (s.startswith("|") and s.endswith("|")):
        # 不是完整表格行（原文自带的碎片，如 `| 前 20…`）：只能前缀截，保住内容
        return _dot(ln[:cap - 1]) if cap > 1 else None
    if cap <= 3:
        return None
    indent = ln[:len(ln) - len(ln.lstrip())]
    cells = s.split("|")[1:-1]
    n = len(cells)
    if n <= 0:
        return _dot(ln[:cap - 1])
    # "| " + " | ".join(parts) + " |" 的固定开销 = 3n + 1
    overhead = 3 * n + 1
    per = (cap - overhead) // n
    if per < 2:
        return None
    parts = []
    for c in cells:
        c = c.strip()
        parts.append(c if len(c) <= per else _dot(c[:per - 1]))
    out = indent + "| " + " | ".join(parts) + " |"
    return out if len(out) <= cap else None


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
            if s.startswith("|"):
                row = _cut_table_row(ln, per_line_cap)
                if row is not None:
                    fixed.append(row)
                continue
            if s.startswith("```") or s.startswith("~~~"):
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
            if ln.strip().startswith("|"):
                row = _cut_table_row(ln, room)
                # 只收「列数完整」的行：以 | 开头结尾且至少 2 列。
                # 否则会输出 `|…` 这种只有一根竖线的残行，比不输出更糟。
                if row and row.strip().startswith("|") and row.strip().endswith("|") \
                        and row.count("|") >= 3:
                    out.append(row)
            else:
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
    # 字面 `\n` 的还原必须**在配平之后**：配平前围栏还没确定、行内 ``` 也没
    # 降级，判不出哪里是代码。实测放前面会把 `入: '｀｀｀a\n｀｀｀** x'` 这种
    # 字符串字面内容里的 \n 也改掉。
    # 单行上下文（改动清单的 rel/note、标题）里**不能还原**：note 本来就是一行，
    # 把字面 `\n` 换成真换行后会被压平成空格 —— 原文「字面 \n 不还原」渲染成
    # 「字面 不还原」，字就丢了。实测真实卡片第 3 条改动就是这样少了一个 \n。
    _nl = _unescape_newline(body) if multiline else body
    if _nl != body:
        # 还原换行会把标记**送到行首**：实测 `` `code`\n\n~~~ `` 里的 `~~~`
        # 原本不在行首（前面是字面 \n\n），还原后跑到行首却没被降级 ——
        # 第二次进来才降级，于是非幂等（模糊测试 391/20000 全是这一类）。
        # 只降级不补围栏，所以 grow=False。
        body = _balance(_nl, grow=False, drop_lone=drop_lone or cut)

    if cut and ellipsis:
        # 先剥掉末尾的「纯省略号残行」：drop_lone 删掉 `**…` 里的 ** 之后
        # 只剩一个 `…`，它会挡住下面「最后一行是不是表格行」的判断，
        # 结果把表格行的收尾 `|` 顶成 `|…`（列结构又坏了）。
        _t = body.rstrip()
        while "\n" in _t and not _t.rsplit("\n", 1)[-1].strip().strip("…．."):
            _t = _t.rsplit("\n", 1)[0].rstrip()
        if _t:
            body = _t
        # 最后一行若是**完整的表格行**（以 | 收尾），末尾不能再加「…」——
        # 否则变成 `|…`，收尾的竖线没了，渲染时这一行缺列；而且 _cut_table_row
        # 已经在超长单元格里补过「…」了，这里再加是重复。
        _last = body.rsplit("\n", 1)[-1].strip()
        if not (_last.startswith("|") and _last.endswith("|")):
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


_SCRATCH_PREFIXES = ("_pending_", "_tmp_", "_probe_", "_guard_")


def _is_scratch(rel) -> bool:
    """_pending_ 是自迭代快照暂存、_tmp_ 是调试用临时脚本 —— 都不是真实代码改动。

    实测 2026-11-18 的真实卡片：「最近 12 个」被 _tmp_verify_map.py 这类
    **已经删掉**的临时脚本占满，map_tools.py / amap.py 的真实改动一条都没露出来。

    另有第三种：`_feature_tests/` 下**没有前缀**的中文名 .py（如「画布对话：回复变
    可拖拽卡片.py」），也是自迭代写的验收暂存脚本 —— 实测 14 个、一条都没进
    changes，但将来一旦被写入就会占名额。该目录里只有 `test_*.py` 是真测试。
    """
    s = str(rel or "").replace("\\", "/")
    base = os.path.basename(s)
    if base.startswith(_SCRATCH_PREFIXES):
        return True
    # 2026-11-18 修：_probe_ / _guard_ 之外还有各种单下划线变体的探针脚本（_w1.py 之类），
    # 实测 30 条改动里 8 条是这种、占 27% 名额，指向的文件早就删了。
    # backend 根目录下唯一的单下划线正式文件是 __init__.py（双下划线），所以用通用规则。
    if base.endswith(".py") and base.startswith("_") and not base.startswith("__"):
        return True
    # 前面补一个 "/"，否则相对路径写成 `_feature_tests/x.py`（不带 backend/）
    # 时匹配不到 "/_feature_tests/" —— 实测这条漏判。
    if "/_feature_tests/" in "/" + s.lstrip("/") and base.endswith(".py") \
            and not base.startswith("test_"):
        return True
    return False


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
    # 暂存/临时脚本不是真实代码改动，记进来会白占清单名额 —— 实测真实卡片
    # 先是被 _pending_ 占了名额，后来被 16 个 _tmp_ 占满整张清单。
    if _is_scratch(rel):
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
    window = msgs[-(MAX_DIALOG * 4):]
    # ★ 2026-11-18 修（去重留哪条）：原来留「第一条」，实测最坏情况下重复正好
    #   发生在最后一轮（[u, a, u, a] 且两个 a 完全相同），删掉后面那条之后
    #   卡尾就断在用户消息上 —— 卡片最该显示的「刚才说了什么」反而没了。
    #   改为留**最后一条**（离当前最近的那次），卡尾始终落在 assistant 上。
    last_pos = {}
    for _i, _m in enumerate(window):
        if isinstance(_m, dict) and str(_m.get("role") or "") == "assistant":
            _t = _clip(_m.get("content"), 8000, ellipsis=False, per_line_cap=0)
            if _t and len(_t) >= 12:
                last_pos[_t] = _i
    for _i, m in enumerate(window):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        # 这里**不补省略号**（否则注入时再截一次会出现「……」）
        # ★ 2026-11-18 修（碎片固化）：原来存 1200 字 + per_line_cap=400，
        #   于是这里就做了「头 60% + … + 尾 40%」的拼接，把长回复从中间
        #   挖掉一块。实测存进 last.json 的是
        #     `…角度：**`last.jso…109/109 通过。…`
        #     `…（`奇偶随之……`、｀磁盘异…测试。15,000 组…`
        #   这种碎片**一旦落盘就固化**，render 层再怎么按句子边界切也救不回来
        #   （它看到的就是一截断掉的 `last.jso`）。
        #   改为存**完整原文**（8000 只是兜底，实测最长 4292），把「怎么截」
        #   全部交给 render —— 只有那一层知道卡片只有 200 字预算、头尾各留多少。
        text = _clip(m.get("content"), 8000, ellipsis=False, per_line_cap=0)
        if role not in ("user", "assistant") or not text:
            continue
        # ★ 2026-11-18 修（间隔重复）：完全相同的长 assistant 回复只留第一条。
        #   下面那条判断只跟 out[-1] 比，实测真实数据里 [10] 与 [13] 是同一段
        #   2187 字回复，中间隔着一条被中断的 152 字回复 [12]，于是两条都留在
        #   卡上，白占 200 字预算（卡片预算被这两条吃掉一半）。
        #   user 的重复（连问两次「好了吗」）是**真实轮次**，这里只对 assistant
        #   动手，且要求完全相同、长度 ≥12，短状态句照旧保留。
        if role == "assistant" and len(text) >= 12:
            if last_pos.get(text, _i) != _i:      # 不是最后一次出现 → 让给后面那条
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
    try:
        n = int(it.get("n") or 1)
    except Exception:
        n = 1
    if n > 1:
        line += "（共 %d 次）" % n
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
    # 同 _dialog_digest：留**最后一条**，否则重复落在最后一轮时卡尾断在用户消息上
    last_pos = {}
    for _i, _m in enumerate(dialog):
        if _m.get("role") == "assistant":
            _t = str(_m.get("text") or "")
            if len(_t) >= 12:
                last_pos[_t] = _i
    for _i, m in enumerate(dialog):
        t = str(m.get("text") or "")
        # ★ 2026-11-18 修（间隔重复）：同 _dialog_digest，救 last.json 里
        #   已经存下来的间隔重复（真实数据 dialog[10] == dialog[13]）。
        if m.get("role") == "assistant" and len(t) >= 12:
            if last_pos.get(t, _i) != _i:         # 不是最后一次出现 → 让给后面那条
                continue
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


def _merge_by_rel(items: list) -> list:
    """同一文件在窗口里出现多次 → 合并成一条。

    **为什么不能只在 record_change 里合并相邻的**：实际改代码是
    「改 handoff.py → 补测试 → 改 handoff.py → 补测试」交替进行的，
    `ch[-1]` 永远不是同文件，那条相邻合并**从来没生效过**。
    实测 last.json 30 条改动 = handoff.py 15 条 + test_handoff_card.py 15 条，
    卡片上同一个测试文件出现 6 次，标题还写「最近 12 个」——
    读起来像 12 个不同文件，实际只有 2 个。
    """
    seen, order = {}, []
    for c in items:
        rel = str(c.get("rel") or "")
        it = seen.get(rel)
        if it is None:
            it = dict(c)
            it["n"] = 1
            seen[rel] = it
            order.append(it)
            continue
        try:
            it["added"] = int(it.get("added") or 0) + int(c.get("added") or 0)
            it["removed"] = int(it.get("removed") or 0) + int(c.get("removed") or 0)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        it["n"] = int(it.get("n") or 1) + 1
        if c.get("note"):
            it["note"] = c.get("note")          # 留最新的说明
        it["time"] = c.get("time") or it.get("time")
    return order


def _split_changes(changes: list, restarted: str):
    """按「重启时间」把改动分成 重启前 / 重启之后 两组，组内按文件合并。

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
    # ★ 先按文件合并，**再**切片 —— 反过来的话，4 个「重启前」位置可能被
    #   同一个文件的 4 次改动占满，等于又退回原来的问题。
    pre, post = _merge_by_rel(pre), _merge_by_rel(post)
    if pre and post:
        return pre[-4:], post[-(SHOW_CHANGES - min(4, len(pre))):]
    return pre[-SHOW_CHANGES:], post[-SHOW_CHANGES:]


def _task_lines() -> list:
    """把自迭代任务状态摘成几行，塞进交接卡。

    ★ 2026-11-20 用户：「如果我中断任务时在下一句开启任务时还需要回忆
      刚才的对话以及任务，防止任务内容因中断补充重启后丢失」。
      交接卡原来只记**对话**和**改过的文件**，完全不记任务状态 ——
      重启/中断之后用户说「继续」，模型根本不知道队列里还排着什么、
      哪个被打断了、哪个已经做完不该重做。

    只摘**还要继续的**（正在跑 / 排队中 / 待验证），做完的不写，省上下文。
    """
    try:
        from . import iter_runner as _ir
        snap = _ir.snapshot(since=0)
    except Exception:                                   # noqa: BLE001
        logger.debug("读自迭代任务状态失败（跳过）", exc_info=True)
        return []
    run = snap.get("running") or None
    q = snap.get("queue") or []
    v = snap.get("verify") or None
    if not run and not q and not v:
        return []

    def _one(x, tag):
        t = str(x.get("title") or "").strip() or "（未命名）"
        br = x.get("brief") or {}
        acts = [str(a) for a in (br.get("actions") or [])[:3] if a]
        tail = ("；要做：%s" % " / ".join(acts)) if acts else ""
        if not tail:
            s = str(br.get("summary") or "").strip()
            if s:
                tail = "；内容：%s" % _inline(s, 80)
        miss = "（⚠️ 计划文件已找不到）" if x.get("plan_missing") else ""
        return "  - %s：%s%s%s" % (tag, _inline(t, 60), tail, miss)

    out = ["· 自迭代任务（中断 / 重启后要接着做的）："]
    if run:
        out.append(_one(run, "正在跑"))
    for i, x in enumerate(q[:6]):
        out.append(_one(x, "排队第 %d" % (i + 1)))
    if len(q) > 6:
        out.append("  - 还有 %d 个排队的没列出来" % (len(q) - 6))
    if v:
        out.append("  - 待验证：%s（第 %s 轮）"
                   % (_inline(str(v.get("title") or "（未命名）"), 60),
                      v.get("tries") or 1))
    out.append("  ⚠️ 用户说「继续」时先看这里：别重复做已经做完的，"
               "也别把队列里的任务弄丢。")
    return out


def render(session: str = "") -> str:
    """生成要注入的交接卡文本；没有可注入的内容就返回空串。"""
    d = _read()
    if not d:
        return ""
    if not _ts_ok(d.get("ts")):
        return ""                          # 太久以前的重启，别再占上下文

    # 旧数据里已经存进了 _pending_ 快照 / _tmp_ 临时脚本，渲染时也过滤一遍
    # —— 光靠 record_change 拦不住历史数据（实测存量 16 条 _tmp_）。
    changes = [c for c in (d.get("changes") or [])
               if isinstance(c, dict) and not _is_scratch(c.get("rel"))]
    dialog = [m for m in (d.get("dialog") or []) if isinstance(m, dict)]
    # ★ 2026-11-20 用户：「中断任务后下一句要能回忆任务」。所以只有任务、
    #   没有对话和文件改动时，这张卡也得出来 —— 否则「中断后重启」正好
    #   是最该交代任务的那一刻，卡却是空的。
    _tasks = []
    try:
        _tasks = _task_lines()
    except Exception:                                   # noqa: BLE001
        logger.debug("交接卡取任务状态失败（跳过）", exc_info=True)
    if not changes and not dialog and not _tasks:
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

    if _tasks:
        lines.extend(_tasks)

    lines.append(
        "⚠️ 这张卡是**上一次会话的背景**，用来接上前文（用户可能正在接着问刚才的事）；"
        "和用户当前这句话冲突时**以当前为准**。不要主动复述这张卡，"
        "除非用户问「刚才聊到哪 / 改了些什么」。")
    return "\n".join(lines)


# =====================================================================
#  盯梢小助手（watch）—— 说一句「盯着 XX」就长期监视，变化时主动汇报
#  （2026-11-22）
# =====================================================================
# 为什么需要
# ----------
# 用户 89 次重复发同一句需求（「有没有免费的」「帮我看着这个文件夹」…）。
# 他真正要的不是「再问一次」，而是「事情变了你自己回来告诉我，而且顺手办好」。
# 所以这里把「盯梢」做成一条**长期任务**：
#     说一次 → 记下目标和触发条件 → 后台定时比对快照 →
#     有变化就落一条 actlog 事件 + 生成一张卡（带「已执行的动作」+「查看详情」）。
#
# 和 scheduler 的关系
# -------------------
# 存储、去重、快照比对在本模块（要记路径 / 条件 / 基线 / 历史，比定时任务复杂）；
# **触发**复用现有的定时任务表：create_watch 会在 schedules.json 里登记一条
# kind="watch" / watch_kind="path" 的任务（watch_ref 指回本模块的监视 id），
# 调度循环到点就调 check_watch —— 用户不需要学第二套概念，也不会多一个线程。
# ⚠️ 事件落盘走 actlog.append_watch_event，不是模块级的 actlog(...) 直接调用；
#    这里只借它那套「按会话隔离 + 滚动覆盖 + 重启后还在」的账本。
#
# 设计取舍
# --------
#   1. **建的时候立刻记基线**。否则用户刚说完「盯着」，下一次检查必然报「有变化」，
#      那是噪音（也是同类功能以前踩过的坑）。
#   2. **同一目标重复交代 → 复用同一条**（按路径 + 条件归一化查重）。这正是
#      「同一需求被重复发送 89 次」的直接解法：说十遍也只盯一条。
#   3. **快照用内容哈希，不只看 mtime**。同一秒内改两次 mtime 可能不变，
#      只看 mtime 会漏掉真变化。
#   4. 任何异常都吞掉并写日志 —— 盯梢坏了绝不能影响对话和调度本身。

WATCH_FILE = os.path.join(DIR, "watch.json")
WATCH_MAX = 40                 # 最多同时盯多少条（超了就滚动丢最老的）


def _watch_file() -> str:
    """监视清单的路径 —— **按当前 DIR 现算**，不要用模块常量。

    ★ 2026-11-22 修真 bug：原来 `_watch_read` / `_watch_write` 用的是导入时
      算好的常量 WATCH_FILE，而回归断言 test_handoff_card 会把 `handoff.DIR`
      指到临时目录做隔离、且**跑完不恢复**。于是后面所有盯梢操作变成：
        · `os.makedirs(DIR)` 建的是临时目录（成功）
        · `open(WATCH_FILE + ".tmp")` 写的是真实数据目录下的文件（目录没建 → 失败）
        · 而 `_watch_write` 把异常吞了只记日志 → **清单一条也存不下来**
      表现就是回归里「盯梢小助手」用例稳定失败（check_watch 找不到记录返回
      False），而单独跑它又通过 —— 典型的环境状态污染。
      改成现算之后，DIR 被指到哪儿，清单就写到哪儿，隔离才是真隔离。
    """
    return os.path.join(DIR, "watch.json")
WATCH_HISTORY = 24             # 每条监视最多留多少条变化历史
WATCH_DEFAULT_MINUTES = 30     # 用户没说间隔时的默认值
WATCH_MAX_FILES = 1500         # 一个目录最多扫多少个文件（防巨目录）
WATCH_HASH_BYTES = 512 * 1024  # 单文件超过这个大小只比大小 + mtime

# 触发条件：key → 一句人话（前端直接显示这句）
WATCH_CONDITIONS = {
    "content_change": "内容一变就汇报",
    "size_change": "大小变了就汇报",
    "exists": "出现 / 消失就汇报",
    "any": "有任何变化就汇报",
}

_WATCH_LOCK = threading.RLock()


def _cond_of(condition) -> str:
    """把用户/调用方给的触发条件归一化成白名单里的一个（认不出的按内容变化）。"""
    c = str(condition or "").strip().lower()
    alias = {
        "change": "content_change", "content": "content_change",
        "diff": "content_change", "内容": "content_change",
        "size": "size_change", "大小": "size_change",
        "exist": "exists", "appear": "exists", "出现": "exists",
    }
    c = alias.get(c, c)
    return c if c in WATCH_CONDITIONS else "content_change"


def _md5(b) -> str:
    if isinstance(b, str):
        b = b.encode("utf-8", "ignore")
    return hashlib.md5(b or b"").hexdigest()


def _watch_read() -> dict:
    """读监视清单（坏文件不覆盖，只当空处理并留日志）。"""
    try:
        with open(_watch_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("watches"), list):
            return d
    except FileNotFoundError:  # noqa - 首次运行还没有清单文件，属正常情况
        pass
    except Exception:
        logger.warning("盯梢 watch.json 读不出来，本次按空处理", exc_info=True)
    return {"watches": []}


def _watch_write(d: dict) -> None:
    """原子写监视清单（写一半断电也不会把整份清单写坏）。"""
    try:
        wf = _watch_file()
        os.makedirs(os.path.dirname(wf) or DIR, exist_ok=True)
        tmp = wf + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, wf)
    except Exception:
        logger.warning("盯梢 watch.json 写入失败", exc_info=True)


def _snapshot(target: str) -> dict:
    """抓一次目标的当前快照（不联网、不改目标）。

    返回 {ok, sig, kind, exists, files, bytes, preview, error}：
      · sig     —— 内容指纹（目录 = 每个文件「相对路径|大小|mtime|内容哈希」再取 md5）
      · kind    —— dir / file / missing
      · preview —— 一句人话摘要（卡片上直接显示）

    目标不存在算**合法状态**（ok=True, kind="missing"）—— 「等文件出现」是正经
    用法，不该报错。
    """
    out = {"ok": False, "sig": "", "kind": "missing", "exists": False,
           "files": 0, "bytes": 0, "preview": "", "error": "", "detail": {}}
    p = str(target or "").strip()
    if not p:
        out["error"] = "没有要盯的目标"
        return out
    out["ok"] = True
    try:
        if os.path.isdir(p):
            rows = []
            notes = []
            n = 0
            total = 0
            for root, dirs, files in os.walk(p):
                dirs.sort()
                files.sort()
                for fn in files:
                    if n >= WATCH_MAX_FILES:
                        break
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, p).replace("\\", "/")
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue      # 正在被写 / 刚好被删，这一轮跳过
                    n += 1
                    total += int(st.st_size)
                    sig = "%s|%d|%d" % (rel, int(st.st_size), int(st.st_mtime))
                    if st.st_size <= WATCH_HASH_BYTES:
                        try:
                            with open(fp, "rb") as f:
                                sig += "|" + _md5(f.read())
                        except OSError:
                            logger.debug("盯梢读文件失败（跳过内容哈希）", exc_info=True)
                    rows.append(sig)
                    if len(notes) < 3:
                        notes.append(rel)
                if n >= WATCH_MAX_FILES:
                    break
            out["kind"] = "dir"
            out["exists"] = True
            out["files"] = n
            out["bytes"] = total
            out["sig"] = _md5("\n".join(rows))
            out["preview"] = "共 %d 个文件 / %d 字节%s" % (
                n, total, ("；" + "、".join(notes)) if notes else "（空目录）")
            if n >= WATCH_MAX_FILES:
                out["preview"] += "（已扫到 %d 个文件上限）" % WATCH_MAX_FILES
            return out
        if os.path.isfile(p):
            try:
                st = os.stat(p)
            except OSError:
                st = None
            size = int(getattr(st, "st_size", 0) or 0)
            body = b""
            try:
                with open(p, "rb") as f:
                    body = f.read(WATCH_HASH_BYTES + 1)
            except OSError:
                logger.debug("盯梢读文件失败", exc_info=True)
            keep = body[:WATCH_HASH_BYTES]
            out["kind"] = "file"
            out["exists"] = True
            out["files"] = 1
            out["bytes"] = size
            out["sig"] = "%d|%d|%s" % (int(getattr(st, "st_mtime", 0) or 0),
                                       size, _md5(keep))
            txt = keep[:200].decode("utf-8", "replace")
            out["preview"] = "文件 %d 字节%s" % (
                size, ("；开头：" + _clean(txt, 60)) if txt.strip() else "")
            return out
        # 目标还不存在：这是「等它出现」的正常状态
        out["kind"] = "missing"
        out["exists"] = False
        out["sig"] = "missing"
        out["preview"] = "目标还不存在（出现时会汇报）"
        return out
    except Exception as e:                                # noqa: BLE001
        out["ok"] = False
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out


def _watch_changed(rec: dict, snap: dict) -> bool:
    """比一次快照，判断「算不算用户想看的变化」。

    · 抓取失败 —— 不算变化（不能在读失败时就喊「变了」）；
    · 第一次（没有旧指纹）—— 不算变化，只记基线；
    · exists      —— 只看「在 / 不在」有没有翻转；
    · size_change —— 只看字节数有没有变；
    · 其余（content_change / any）—— 看整体指纹。
    """
    if not snap.get("ok"):
        return False
    old = str(rec.get("sig") or "")
    if not old:
        return False
    cond = _cond_of(rec.get("condition"))
    if cond == "exists":
        return bool(rec.get("exists")) != bool(snap.get("exists"))
    if cond == "size_change":
        if rec.get("bytes") is None:
            return False
        return int(rec.get("bytes") or 0) != int(snap.get("bytes") or 0)
    return old != str(snap.get("sig") or "")


def _run_watch_action(rec: dict, snap: dict) -> list:
    """执行监视任务上预设的动作，返回「已经办了什么」的人话列表。

    目前支持（都不联网、都不改目标，用户说了才做）：
      · "snapshot"      —— 把这次看到的摘要另存一份，方便事后对比
      · "job:<任务id>"  —— 立刻跑一条已有的定时任务（顺手把下一步也办了）
      · 其它 / 空       —— 只汇报，不动手
    """
    done = []
    act = str(rec.get("action") or "").strip()
    if not act:
        return done
    try:
        if act == "snapshot":
            d = os.path.join(DIR, "watch_snap", str(rec.get("id") or "w"))
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, time.strftime("%Y%m%d_%H%M%S") + ".txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("%s\n%s\n\n%s\n" % (rec.get("target") or "",
                                            _now_str(), snap.get("preview") or ""))
            done.append("已另存快照 %s" % os.path.basename(fp))
        elif act.startswith("job:"):
            jid = act.split(":", 1)[1].strip()
            from . import scheduler as _sch
            r = _sch.run_now(jid, background=True)
            done.append("已触发定时任务 %s（%s）" % (
                jid, "已开始" if r.get("ok") else (r.get("error") or "没跑起来")))
        else:
            done.append("按预设动作「%s」记了一笔待办" % _clean(act, 40))
    except Exception as e:                                # noqa: BLE001
        logger.debug("盯梢预设动作执行失败", exc_info=True)
        done.append("预设动作「%s」没执行成功：%s" % (_clean(act, 40), e))
    return done


def _emit_watch_change(rec: dict, ev: dict) -> None:
    """把「有变化」这件事落一条 actlog 事件（前端据此弹卡）。

    ★ 只落**一条**：验收断言按「事件数 +1」判断有没有真汇报，
      多写一条就会把「汇报了几次」算错。
    """
    try:
        from . import actlog
        actlog.append_watch_event(str(rec.get("event_session") or ""), {
            "kind": "watch_change",
            "key": str(rec.get("target") or "")[:120],
            "state": "changed",
            "prev": str(ev.get("before") or "")[:120],
            "note": "；".join([str(ev.get("summary") or "")] +
                             list(ev.get("actions") or []))[:120],
            "ts": time.time(),
            "count": int(rec.get("changes") or 0),
        })
    except Exception:
        logger.warning("盯梢事件落盘失败（不影响监视本身）", exc_info=True)


def _watch_path_of(target: str) -> str:
    """把目标规整成绝对路径（认不出就当普通字符串原样留着）。"""
    t = str(target or "").strip().strip("\"'「」“”")
    if not t:
        return ""
    try:
        return os.path.abspath(os.path.expanduser(t))
    except Exception:                                     # noqa: BLE001
        return t


def _sync_watch_job(rec: dict) -> str:
    """把监视任务登记进**现有的**定时任务表（kind="watch" / watch_kind="path"）。

    失败不影响创建 —— 用户至少能在「盯梢」分组里手动点「立即检查」。
    """
    try:
        from . import scheduler as _sch
        r = _sch.register_file_watch(
            rec.get("path") or rec.get("target") or "",
            str(rec.get("id") or ""),
            interval_minutes=int(rec.get("interval_minutes") or 0),
            name=str(rec.get("name") or ""),
            session=str(rec.get("session") or ""))
        if r.get("ok"):
            return str((r.get("job") or {}).get("id") or "")
        logger.warning("盯梢登记定时任务失败：%s", r.get("error"))
    except Exception:                                     # noqa: BLE001
        logger.warning("盯梢登记定时任务异常（监视本身仍在）", exc_info=True)
    return ""


def _prune_watch_jobs(alive_ids) -> int:
    """清掉 schedules.json 里「监视记录已经不存在」的残留任务（抖动防护）。"""
    try:
        from . import scheduler as _sch
        alive = set(str(x) for x in (alive_ids or []))
        jobs = _sch.load_jobs()
        keep = []
        dropped = 0
        for j in jobs:
            if (str(j.get("kind") or "") == "watch"
                    and str(j.get("watch_kind") or "") == "path"
                    and str(j.get("watch_ref") or "") not in alive):
                dropped += 1
                continue
            keep.append(j)
        if dropped:
            _sch.save_jobs(keep)
        return dropped
    except Exception:                                     # noqa: BLE001
        logger.debug("清理残留盯梢任务失败（跳过）", exc_info=True)
        return 0


def create_watch(target: str, condition: str = "content_change", *,
                 name: str = "", interval_minutes: int = 0, action: str = "",
                 session: str = "") -> str:
    """登记一条「盯着 XX」的长期监视任务，返回它的 id（失败返回空串）。

    参数：
      target            —— 要盯的东西：目录、文件、网址 / 关键词（原样保存）
      condition         —— 触发条件，见 WATCH_CONDITIONS（默认「内容一变就汇报」）
      interval_minutes  —— 多久查一次（0 = 默认 30 分钟）
      action            —— 有变化时**顺手办好**的预设动作（见 _run_watch_action）
      session           —— 哪段会话说的（事件落进那段账本，前端好归位）

    ★ 建的时候立刻记一次基线：用户刚说完「盯着」，下一次检查不该马上收到
      「有变化」—— 那是噪音。所以这里先抓一次快照当基线。
    ★ 同一目标 + 同一条件再交代一次 → **复用**原来那条（只更新间隔 / 开关），
      返回同一个 id。这是「同一需求被重复发送 89 次」的直接解法。
    """
    t = str(target or "").strip()
    if not t:
        return ""
    path = _watch_path_of(t)
    cond = _cond_of(condition)
    try:
        mins = int(interval_minutes or 0)
    except Exception:                                     # noqa: BLE001
        mins = 0
    if mins <= 0:
        mins = WATCH_DEFAULT_MINUTES
    mins = max(1, min(mins, 7 * 24 * 60))
    act = str(action or "").strip()
    job_id = ""
    with _WATCH_LOCK:
        d = _watch_read()
        watches = [w for w in (d.get("watches") or []) if isinstance(w, dict)]
        for old in watches:
            if _cond_of(old.get("condition")) != cond:
                continue
            if str(old.get("path") or "") not in (path, ""):
                continue
            old["enabled"] = True
            old["interval_minutes"] = mins
            if name:
                old["name"] = _clean(name, 40)
            if act:
                old["action"] = act
            if session and not old.get("session"):
                old["session"] = _clean(session, 60)
            d["watches"] = watches
            _watch_write(d)
            _sync_watch_job(old)
            return str(old.get("id") or "")

        snap = _snapshot(path or t)
        wid = "w" + uuid.uuid4().hex[:10]
        rec = {
            "id": wid,
            "target": t,
            "path": path,
            "condition": cond,
            "name": _clean(name, 40) or ("盯着：" + _clean(t, 24)),
            "action": act,
            "session": _clean(session, 60),
            # 事件落进这条监视**自己的**账本：每条 watch 一个文件，避免和其它
            # 会话的滚动覆盖互相挤掉（一次变化就该留下一条）。
            "event_session": "watch_" + wid[-8:],
            "enabled": True,
            "interval_minutes": mins,
            "created": _now_str(),
            "last_check": _now_str(),
            "checks": 0,
            "changes": 0,
            "sig": snap.get("sig") or "",
            "kind": snap.get("kind") or "",
            "exists": bool(snap.get("exists")),
            "files": int(snap.get("files") or 0),
            "bytes": int(snap.get("bytes") or 0),
            "preview": snap.get("preview") or "",
            "status": "已记基线" if snap.get("ok") else ("基线没抓成：%s"
                                                       % (snap.get("error") or "")),
            "pending": False,
            "job_id": "",
            "history": [],
        }
        watches.append(rec)
        d["watches"] = watches[-WATCH_MAX:]
        _watch_write(d)
        _prune_watch_jobs([w.get("id") for w in d["watches"]])
        job_id = _sync_watch_job(rec)
        if job_id:
            rec["job_id"] = job_id
            _watch_write(d)                # rec 就在 d["watches"] 里，一起落盘
    return wid


def check_watch(watch_id: str) -> bool:
    """检查一条监视任务：抓快照 → 和上次比 → 有变化就落事件 + 执行预设动作。

    返回 **True 表示这次真的发现变化**（调用方 / 验收断言就按这个判断）。
    任何异常都不抛：一条监视炸掉不该把调度循环带走。
    """
    wid = str(watch_id or "").strip()
    if not wid:
        return False
    changed = False
    ev = None
    rec = None
    with _WATCH_LOCK:
        d = _watch_read()
        for w in (d.get("watches") or []):
            if isinstance(w, dict) and str(w.get("id") or "") == wid:
                rec = w
                break
        if rec is None:
            return False
        snap = _snapshot(rec.get("path") or rec.get("target") or "")
        changed = _watch_changed(rec, snap)
        rec["last_check"] = _now_str()
        rec["checks"] = int(rec.get("checks") or 0) + 1
        if changed:
            before = str(rec.get("preview") or "")
            preview = str(snap.get("preview") or "")
            rec["changes"] = int(rec.get("changes") or 0) + 1
            actions = _run_watch_action(rec, snap)
            if not actions:
                actions = ["仅汇报（这条监视没有预设动作）"]
            summary = "「%s」有变化" % _clean(rec.get("target"), 40)
            if before and preview and before != preview:
                summary += "：%s → %s" % (_clean(before, 50), _clean(preview, 50))
            ev = {"t": rec["last_check"], "before": before, "after": preview,
                  "summary": summary, "actions": actions}
            hist = [h for h in (rec.get("history") or []) if isinstance(h, dict)]
            rec["history"] = (hist + [ev])[-WATCH_HISTORY:]
            rec["status"] = "有变化（第 %d 次）" % rec["changes"]
            # pending=True：前端据此「主动弹一次卡」，用户看过就清掉
            rec["pending"] = True
            rec["pending_at"] = rec["last_check"]
        elif not snap.get("ok"):
            rec["status"] = "检查失败：%s" % (snap.get("error") or "未知错误")
        else:
            rec["status"] = "无变化"
            if not rec.get("sig"):
                rec["status"] = "已记基线"
        # 快照无论变没变都更新 —— 下一次比对永远对着「刚刚看到的」这一版
        if snap.get("ok"):
            rec["sig"] = snap.get("sig") or ""
            rec["kind"] = snap.get("kind") or ""
            rec["exists"] = bool(snap.get("exists"))
            rec["files"] = int(snap.get("files") or 0)
            rec["bytes"] = int(snap.get("bytes") or 0)
            rec["preview"] = snap.get("preview") or ""
        _watch_write(d)
    if changed and ev is not None:
        _emit_watch_change(rec, ev)        # 落账本放在锁外：IO 不占监视锁
    return bool(changed)


def _watch_view(rec: dict) -> dict:
    """把一条监视记录整理成给前端 / 接口用的**精简视图**（字段固定、可序列化）。"""
    hist = [h for h in (rec.get("history") or []) if isinstance(h, dict)]
    last = hist[-1] if hist else None
    return {
        "id": str(rec.get("id") or ""),
        "name": str(rec.get("name") or ""),
        "target": str(rec.get("target") or ""),
        "path": str(rec.get("path") or ""),
        "kind": str(rec.get("kind") or ""),
        "condition": _cond_of(rec.get("condition")),
        "condition_text": WATCH_CONDITIONS.get(_cond_of(rec.get("condition")), ""),
        "action": str(rec.get("action") or ""),
        "enabled": bool(rec.get("enabled")),
        "interval_minutes": int(rec.get("interval_minutes") or 0),
        "period": "每 %d 分钟" % int(rec.get("interval_minutes") or 0),
        "created": str(rec.get("created") or ""),
        "last_check": str(rec.get("last_check") or ""),
        "status": str(rec.get("status") or ""),
        "checks": int(rec.get("checks") or 0),
        "changes": int(rec.get("changes") or 0),
        "files": int(rec.get("files") or 0),
        "bytes": int(rec.get("bytes") or 0),
        "preview": str(rec.get("preview") or ""),
        "pending": bool(rec.get("pending")),
        "job_id": str(rec.get("job_id") or ""),
        # 最近一次变化：卡片上的「已执行的动作」+「查看详情」都吃这两个
        "last_change": last,
        "history": hist[-8:],
    }


def list_watches(session: str = "") -> list:
    """列出所有监视任务（按创建时间倒序），每条都是 _watch_view 的精简视图。

    给「盯梢」分组用：用户关掉页面再回来，能一眼看到「我在盯哪些东西、
    最近一次检查是什么时候、上次变化时它顺手做了什么」。
    """
    out = []
    try:
        d = _watch_read()
    except Exception:                                     # noqa: BLE001
        logger.debug("读监视清单失败（当空处理）", exc_info=True)
        return out
    sid = str(session or "").strip()
    for rec in (d.get("watches") or []):
        if not isinstance(rec, dict):
            continue
        if sid and str(rec.get("session") or "") not in (sid, ""):
            continue
        out.append(_watch_view(rec))
    out.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    return out


def get_watch(watch_id: str) -> dict:
    """取一条监视的精简视图（没有就 {}）。

    给「变化了 → 主动弹卡」用：卡片上的名字 / 目标 / 上次变化 / 已执行动作
    都在这份视图里，调度线程不用自己去翻 watch.json。
    """
    wid = str(watch_id or "").strip()
    if not wid:
        return {}
    try:
        for rec in (_watch_read().get("watches") or []):
            if isinstance(rec, dict) and str(rec.get("id") or "") == wid:
                return _watch_view(rec)
    except Exception:                                     # noqa: BLE001
        logger.debug("读单条监视失败（按没有处理）", exc_info=True)
    return {}


def watch_group(title: str = "盯梢") -> dict:
    """jobplan 面板的「盯梢」分组数据源（前端直接渲染，不用自己拼口径）。"""
    items = list_watches()
    return {
        "group": "watch",
        "title": title,
        "count": len(items),
        "pending": len([x for x in items if x.get("pending")]),
        "changed": len([x for x in items if int(x.get("changes") or 0) > 0]),
        "items": items,
    }


def ack_watch(watch_id: str) -> dict:
    """用户已经看过这张卡 → 清掉 pending（下次有变化才会再弹）。"""
    wid = str(watch_id or "").strip()
    if not wid:
        return {"ok": False, "error": "缺少监视 id"}
    with _WATCH_LOCK:
        d = _watch_read()
        hit = None
        for rec in (d.get("watches") or []):
            if isinstance(rec, dict) and str(rec.get("id") or "") == wid:
                rec["pending"] = False
                hit = rec
                break
        if hit is None:
            return {"ok": False, "error": "没有这条监视"}
        _watch_write(d)
        return {"ok": True, "watch": _watch_view(hit)}


def remove_watch(watch_id: str) -> dict:
    """删掉一条监视任务，同时把它在定时任务表里的那条也删掉。"""
    wid = str(watch_id or "").strip()
    if not wid:
        return {"ok": False, "error": "缺少监视 id"}
    job_id = ""
    with _WATCH_LOCK:
        d = _watch_read()
        watches = [w for w in (d.get("watches") or []) if isinstance(w, dict)]
        keep = []
        for rec in watches:
            if str(rec.get("id") or "") == wid:
                job_id = str(rec.get("job_id") or "")
                continue
            keep.append(rec)
        if len(keep) == len(watches):
            return {"ok": False, "error": "没有这条监视"}
        d["watches"] = keep
        _watch_write(d)
    try:
        from . import scheduler as _sch
        jobs = _sch.load_jobs()
        keepj = [j for j in jobs
                 if not (str(j.get("kind") or "") == "watch"
                         and (str(j.get("watch_ref") or "") == wid
                              or (job_id and str(j.get("id") or "") == job_id)))]
        if len(keepj) != len(jobs):
            _sch.save_jobs(keepj)
    except Exception:                                     # noqa: BLE001
        logger.debug("删除监视对应的定时任务失败（不影响监视本身）", exc_info=True)
    return {"ok": True, "id": wid}


def check_due(limit: int = 0) -> dict:
    """把所有**该检查的**监视任务跑一遍（供调度/接口兜底调用）。

    判断「该不该查」：上次检查时间 + 间隔 <= now。返回
    {ok, ran, changed, errors} —— 和 scheduler.tick_watch 的口径一致。
    """
    now = time.time()
    ran = 0
    changed = 0
    errors = []
    try:
        d = _watch_read()
        items = [w for w in (d.get("watches") or []) if isinstance(w, dict)]
    except Exception:                                     # noqa: BLE001
        return {"ok": False, "ran": 0, "changed": 0, "errors": ["清单读不出来"]}
    for rec in items:
        if not rec.get("enabled"):
            continue
        try:
            mins = int(rec.get("interval_minutes") or WATCH_DEFAULT_MINUTES)
        except Exception:                                 # noqa: BLE001
            mins = WATCH_DEFAULT_MINUTES
        try:
            last = time.mktime(time.strptime(str(rec.get("last_check") or ""),
                                             "%Y-%m-%d %H:%M:%S"))
        except Exception:                                 # noqa: BLE001
            last = 0.0
        if last and (now - last) < mins * 60:
            continue
        ran += 1
        try:
            if check_watch(str(rec.get("id") or "")):
                changed += 1
        except Exception as e:                            # noqa: BLE001
            errors.append({"id": rec.get("id"), "error": "%s: %s" % (type(e).__name__, e)})
        if limit and ran >= int(limit):
            break
    return {"ok": True, "ran": ran, "changed": changed, "errors": errors}


# ---------------------------------------------------------------- 一句话解析
# 用户不会念参数，他只会说：「盯着 D:\work\报价单，有变化就告诉我」。
# 这里把这句话拆成 target / condition / action —— 纯规则、不调模型（要秒回、
# 要离线可测）。拆不出来就返回空 target，让调用方去问一句，别瞎猜。
_WATCH_TRIGGER_RE = re.compile(r"^(.*?)(盯着|盯住|盯一下|看着|看住|监视|监控|跟进|"
                               r"watch)\s*[:：]?\s*", re.I)
_WATCH_TAIL_RE = re.compile(r"[，,]?\s*(一有变化|有变化|变了|变化时|变动时|更新时|"
                            r"一旦变化|如果变了|只要有变化).*$")
_WATCH_ACT_RE = re.compile(r"(另存|存一份|留一份|备份)", )


def parse_watch_text(text: str) -> dict:
    """从用户原话里拆出「盯什么 / 什么算变化 / 要不要顺手办什么」。

    返回 {target, condition, action, name}，认不出的字段给默认值；
    实在没提到目标就返回 target=""。
    """
    raw = " ".join(str(text or "").split())
    out = {"target": "", "condition": "content_change", "action": "", "name": ""}
    if not raw:
        return out

    # 1) 触发条件：先按整句判断（「出现 / 消失」「大小」这些词可能在从句里）
    if re.search(r"(出现|消失|不见了|多出来|新建)", raw):
        out["condition"] = "exists"
    elif re.search(r"(大小|体积|多大|占多少)", raw):
        out["condition"] = "size_change"

    # 2) 预设动作
    if _WATCH_ACT_RE.search(raw):
        out["action"] = "snapshot"

    # 3) 目标：去掉开头的客套和「盯着」，再去掉「有变化就告诉我」这类尾巴
    s = re.sub(r"^(帮我|帮忙|请你?|麻烦你?|我要你|给我|替我)\s*", "", raw).strip()
    m = _WATCH_TRIGGER_RE.match(s)
    if m:
        s = s[m.end():]
    s = _WATCH_TAIL_RE.sub("", s).strip()
    # 从句兜底：路径里不会有中文标点，也不会「空格 + 一段中文」。
    # 不做这层的话「盯着 a.log 每 30 分钟看一次」会把整段尾巴当成路径，
    # 建出来的监视指向一个不存在的文件，永远只会报「文件不存在」。
    s = re.split(r"[，,。；;！!？?]", s)[0].strip()
    _m2 = re.match(r"^(\S+)\s+[\u4e00-\u9fff]", s)
    if _m2:
        s = _m2.group(1)
    s = s.strip(" \t，,。.！!；;：:、")
    s = s.strip("\"'「」“”《》")
    if s:
        out["target"] = s
        # 名称优先用文件名：长路径从头截断会变成「盯着：D:/local-multimodal-src/」，
        # 列表里好几条长得一模一样，根本分不清盯的是哪个文件。
        _base = re.split(r"[\\/]", s.rstrip("\\/ "))[-1] or s
        out["name"] = "盯着：" + _clean(_base, 24)
    return out


# ------------------------------------------------------ 一句话意图解析
# 2026-11-22（盯梢小助手一句话入口 + 主动弹卡）
#
# 痛点：用户连续 4 轮追问「盯到了吗」，因为他真正想说的是**一句话**：
#       「帮我盯着某商品降价」。而 parse_watch_text 只回答「盯什么」，
#       调用方还需要知道三件更关键的事：
#         · 这句到底是不是盯梢需求（不是就别乱建监视，别把要求当目标）；
#         · 目标是本机路径，还是网页 / 关键词 —— 这是**两条完全不同的执行路径**：
#             路径   → 本模块的快照比对（check_watch）
#             关键词 → scheduler 的联网抓取比对（run_watch / kind="web"）
#           建错边的话，「盯着某商品降价」会变成盯一个不存在的文件，
#           永远只会报「目标还不存在」——功能看着在跑，实际一句有用的话都没有。
#         · 用户有没有顺口说间隔（「每 30 分钟看一眼」）。
# 所以这里再包一层，产出一份**结构化意图**，tools / 前端 / HTTP 接口
# 都按同一口径分流，不用各自再写一遍正则（也不容易写歪）。
_WATCH_INTENT_TRIGGER_RE = re.compile(
    r"(盯着|盯住|盯一下|盯紧|盯着点|帮我盯|给我盯|看着点|看着|看住|"
    r"监视|监控|跟进|留意|watch)", re.I)
_WATCH_MIN_RE = re.compile(r"(?:每|每隔)?\s*(\d+)\s*(?:分钟|分|min)", re.I)
_WATCH_HOUR_RE = re.compile(r"(?:每|每隔)?\s*(\d+)\s*(?:个)?\s*(?:小时|钟头|h)", re.I)
_WATCH_INTERVAL_WORDS = (
    ("每半小时", 30), ("半个小时", 30), ("每小时", 60), ("每一小时", 60),
    ("每半天", 720), ("每天", 1440), ("每日", 1440), ("每周", 10080),
)
_WATCH_PATH_EXT_RE = re.compile(
    r"\.(?:txt|log|md|json|csv|xlsx?|docx?|pptx?|pdf|py|js|ts|html?|zip|"
    r"png|jpe?g|webp|mp4|srt)$", re.I)
_WATCH_URL_RE = re.compile(r"^(?:https?://|www\.)", re.I)


def _watch_interval_of(text: str) -> int:
    """从原话里捞间隔（分钟）；没说返回 0，交给调用方用默认值。

    认「每 30 分钟 / 每2小时 / 半小时 / 每天」这些最常见的说法。
    上限和 create_watch 保持一致（7 天），防止一句「每 999999 分钟」把调度压死。
    """
    s = str(text or "")
    m = _WATCH_MIN_RE.search(s)
    if m:
        try:
            return max(1, min(int(m.group(1)), 7 * 24 * 60))
        except Exception:                                 # noqa: BLE001
            pass
    m = _WATCH_HOUR_RE.search(s)
    if m:
        try:
            return max(1, min(int(m.group(1)) * 60, 7 * 24 * 60))
        except Exception:                                 # noqa: BLE001
            pass
    for word, mins in _WATCH_INTERVAL_WORDS:
        if word in s:
            return mins
    return 0


def _watch_kind_of(target: str) -> str:
    """判断目标是本机路径（"path"）还是联网关键词 / 网址（"web"）。

    ★ 网址先判：`https://a.com/b` 里也有 `/`，不先拦会被当成 Windows 路径，
      于是「盯着某个商品页」变成盯一个永远不存在的本地文件。
    """
    t = str(target or "").strip()
    if not t:
        return "web"
    if _WATCH_URL_RE.match(t):
        return "web"
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith(("\\\\", "./", "../", "~/")):
        return "path"
    if _WATCH_PATH_EXT_RE.search(t):
        return "path"
    if re.search(r"[\\/]", t):
        return "path"
    try:
        if os.path.exists(t):
            return "path"
    except Exception:                                     # noqa: BLE001
        logger.debug("盯梢目标存在性判断失败（按关键词处理）", exc_info=True)
    return "web"


def parse_watch_intent(text: str) -> dict:
    """把「帮我盯着某商品降价」这类原话解析成**结构化盯梢意图**。

    返回（字段固定，缺省也有值，调用方不用再判 None）::

        {"intent": "watch" | "",     # 认出来了就是 "watch"，否则空串
         "is_watch": bool,           # 是否应当按盯梢处理（有触发词 + 有目标）
         "matched": bool,            # 句子里出现了「盯着 / 监视 / 留意…」这类词
         "raw": str,                 # 规整后的原话
         "target": str,              # 盯什么（路径 / 网址 / 关键词）
         "kind": "path" | "web",     # 走哪条执行路径（见 _watch_kind_of）
         "condition": str,           # content_change / size_change / exists / any
         "condition_text": str,      # 上面那个的一句人话（前端直接显示）
         "interval_minutes": int,    # 用户顺口说的间隔（没说 = 0）
         "action": str,              # 有变化时顺手做的预设动作
         "name": str}                # 列表里显示的名字

    ★ 纯规则、不调模型：要秒回，也要能在回归里离线断言。
    ★ 认不出目标就返回 is_watch=False，让调用方去问一句 —— 猜错了比不问更糟
      （会建出一条永远不报变化的监视，用户以为它在盯）。
    """
    raw = " ".join(str(text or "").split())
    base = parse_watch_text(raw)
    target = str(base.get("target") or "").strip()
    matched = bool(_WATCH_INTENT_TRIGGER_RE.search(raw))
    is_watch = bool(matched and target)
    cond = _cond_of(base.get("condition"))
    out = {
        "intent": "watch" if is_watch else "",
        "is_watch": is_watch,
        "matched": matched,
        "raw": raw,
        "target": target,
        "kind": _watch_kind_of(target),
        "condition": cond,
        "condition_text": WATCH_CONDITIONS.get(cond, ""),
        "interval_minutes": _watch_interval_of(raw),
        "action": str(base.get("action") or ""),
        # 不是盯梢就别给名字 —— 调用方（比如前端）看到 name 会以为真解析出了监视
        "name": str(base.get("name") or "") if is_watch else "",
    }
    if is_watch and not out["name"]:
        _base = re.split(r"[\\/]", target.rstrip("\\/ "))[-1] or target
        out["name"] = "盯着：" + _clean(_base, 24)
    return out


def create_watch_from_text(text: str, *, session: str = "",
                           interval_minutes: int = 0) -> dict:
    """一句话建监视：「盯着 XX，有变化就告诉我」→ 真的登记一条长期监视。

    ★ 2026-11-22 分流：一句话里的目标可能是**本地路径**，也可能是
      **网页 / 关键词**（「盯着某商品降价」）。前者归本模块（快照比对），
      后者归 scheduler 的联网抓取比对 —— 两边都叫「盯梢」，用户不需要知道区别。
      以前不分，关键词也被当成路径建了一条监视，于是永远「目标还不存在」，
      用户反复问「盯到了吗」，就是这个断口。

    返回 {ok, id, watch, parsed} 或 {ok: False, error, parsed}。
    """
    parsed = parse_watch_intent(text)
    if not parsed.get("target"):
        return {"ok": False, "error": "没听出要盯什么 —— 说「盯着 <目录 / 网址 / 关键词>」就行",
                "parsed": parsed}
    try:
        mins = int(interval_minutes or 0)
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        mins = int(parsed.get("interval_minutes") or 0)
    if parsed.get("kind") == "web":
        # 关键词 / 网址：交给 scheduler 联网比对（每轮抓一次、和上次做 diff）
        try:
            from . import scheduler as _sch
            kind = "page" if _WATCH_URL_RE.match(str(parsed["target"])) else "web"
            r = _sch.register_watch(str(parsed["target"]), interval_minutes=mins,
                                    kind=kind, name=str(parsed.get("name") or ""),
                                    session=session, source="watch_intent")
        except Exception as e:                            # noqa: BLE001
            logger.warning("盯梢（联网类）登记失败：%s", e)
            return {"ok": False, "error": "联网监视没建起来：%s" % e,
                    "parsed": parsed}
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "联网监视没建起来",
                    "parsed": parsed}
        job = r.get("job") or {}
        every = int(((job.get("schedule") or {}).get("every_minutes")) or 0)
        return {
            "ok": True,
            "id": str(job.get("id") or ""),
            "reused": bool(r.get("reused")),
            "parsed": parsed,
            "watch": {
                "id": str(job.get("id") or ""),
                "name": str(job.get("name") or parsed.get("name") or ""),
                "target": str(job.get("watch_query") or parsed["target"]),
                "kind": str(job.get("watch_kind") or ""),
                "condition": parsed.get("condition"),
                "condition_text": parsed.get("condition_text"),
                "interval_minutes": every,
                "period": ("每 %d 分钟" % every) if every else "",
                "status": str(job.get("last_status") or "已登记，等第一次抓取"),
                "last_change": None,
            },
        }
    wid = create_watch(parsed["target"],
                       condition=parsed.get("condition") or "content_change",
                       name=parsed.get("name") or "",
                       interval_minutes=mins,
                       action=parsed.get("action") or "",
                       session=session)
    if not wid:
        return {"ok": False, "error": "监视没建起来（目标为空或写盘失败）", "parsed": parsed}
    one = None
    for w in list_watches():
        if w.get("id") == wid:
            one = w
            break
    return {"ok": True, "id": wid, "watch": one, "parsed": parsed}
