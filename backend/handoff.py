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
"""
import json
import os
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
    return " ".join(str(s or "").split())[:cap]


def _mask_code(s: str) -> str:
        """把 ``` 围栏与 ` 行内代码换成等长空格（只用于统计正文里的 markdown 标记）。

        2026-11-18 加（用户贴的真实卡片抓到）：原来一律用 `s.count("**") % 2`
        判奇偶，把**行内代码里**的 `**` 也算进去了。实测一条消息里有 3 处
        `` `**` `` 加若干正常粗体，总数变奇数，于是把最后一对**正常粗体**的闭合
        `**` 换成了全角 ＊＊ —— 输出「现在**必须重启＊＊——」，粗体被拆开、
        还凭空多出两个全角星号。行内代码里的 `**` 是**内容**，不该参与配平。
        """
        if not s:
            return ""
        out = list(s)
        off = 0
        for idx, part in enumerate(s.split("```")):
            if idx % 2 == 1:                       # 围栏内部：整段遮掉
                for k in range(off, min(off + len(part), len(out))):
                    out[k] = " "
            else:                                  # 围栏外：再按 ` 分出代码段
                o = off
                for j, seg in enumerate(part.split("`")):
                    if j % 2 == 1:                 # 行内代码：遮掉
                        for k in range(o, min(o + len(seg), len(out))):
                            out[k] = " "
                    o += len(seg) + 1
            off += len(part) + 3
        return "".join(out)


def _bold_pos(s: str) -> list:
        """返回**正文里**（行内代码/围栏之外）所有 `**` 的起始下标。"""
        m = _mask_code(s)
        pos = []
        k = 0
        while True:
            p = m.find("**", k)
            if p < 0:
                break
            pos.append(p)
            k = p + 2
        return pos


def _balance_fence(s: str, drop_lone: bool = False) -> str:
    """把未闭合的 ``` 围栏、` 行内代码补平。

    2026-11-18 修：原来只在「截断时」才配平，于是**短消息**（不截断）里
    一个落单的 ` 就原样注入 —— 实测「用 `rescue.py 处理」输出后整张卡后面
    的 markdown 全被当成行内代码吞掉。配平必须对所有输出生效。
    """
    if not s:
        return s
    # 2026-11-18 修（模糊测试抓到 29/10000 组）：** 的奇偶会在「配平反引号」**之后**
    # 变化 —— 落单的 ` 换成全角 ｀ 后，原本被行内代码遮住的 ** 就露出来了，奇偶
    # 又变奇，于是要到下一轮（_finalize）才被改成 ＊＊，输出「a＊＊b*」。
    # 所以这里改成**反复处理到稳定**：每轮「处理落单 ** → 补围栏 → 配平行内反引号」。
    for _pass in range(5):
        _before = s
        # 1) 正文里落单的 **（_bold_pos 会跳过 `…` 与 ``` 围栏）
        _bp = _bold_pos(s)
        if len(_bp) % 2 == 1:
            k = _bp[-1]
            if drop_lone:
                # 截断正好切在**粗体中间**时，这个 ** 是「开标记」，配对的闭合已被
                # 切掉、后面只剩半个词。换成 ＊＊ 会输出「2 个是真实 bug：＊＊代…」
                # 这种像错字的东西；它只是语法标记，直接去掉（不涉及内容）。
                s = s[:k] + s[k + 2:]
            else:
                # 换全角 ＊＊：视觉等价、不构成 markdown 粗体、原文不丢（不删内容）
                s = s[:k] + "＊＊" + s[k + 2:]
        # 2) 代码围栏没关 → 补一个
        if s.count("```") % 2 == 1:
            s += "\n```"
        # 3) 围栏**之外**的落单行内反引号换全角 ｀
        #    （不能删：实测「落单 ` 反引号」→「落单  反引号」，少一个字符还多出双空格）
        parts = s.split("```")
        for i in range(0, len(parts), 2):   # 偶数下标＝围栏之外的文本
            if parts[i].count("`") % 2 == 1:
                j = parts[i].rfind("`")
                if j >= 0:
                    parts[i] = parts[i][:j] + "｀" + parts[i][j + 1:]
        s = "```".join(parts)
        if s == _before:                    # 稳定了就停
            break
    out = s
    # 去掉落单 ` 之后那行可能只剩空格（实测输入「 `」→ 输出一行只有一个空格），
    # markdown 里一行纯空格虽然无害，但会让「无纯空格行」这条断言不成立，也不干净。
    return "\n".join(("" if not l.strip() else l.rstrip()) for l in out.split("\n"))


def _inline(s, cap: int = 160) -> str:
    """单行上下文（原因、改动清单的 rel/note）专用的截断。

    2026-11-18 加（整卡模糊测试 400 组发现）：这几处原来复用 `_clip`，而
    `_clip` 遇到未闭合围栏会补一个 "\\n```" —— 在单行里补出来就变成
    「这一行 3 个反引号 + 下一行孤零零一个 ```」，markdown 被撕开，整卡
    围栏数还变成奇数，后面照样被吞。单行里没法承载围栏，所以这里**去掉**
    多余的反引号，而不是补一个闭合的。
    """
    t = " ".join(str(s or "").split())          # 压成一行（这几处本来就不需要换行）
    if cap and len(t) > cap:
        t = t[:max(0, cap - 1)].rstrip() + "…"
    # 2026-11-18 加：截断可能把开头的 ** 切掉只剩结尾的 **，后面整段变粗体。
    # 和 _balance_fence 一样，必须在配平反引号之前处理（删 ** 会合并反引号段）。
    # 2026-11-18 修（真实卡片抓到）：同 _balance_fence —— 只统计正文里的 **，
    # 否则 note 里 `` `**` `` 这类行内代码会让正常粗体被拆成 ＊＊。
    _bp = _bold_pos(t)
    if len(_bp) % 2 == 1:
        k = _bp[-1]
        # 换全角 ＊＊：视觉等价、不是 markdown 标记、原文不丢（不删内容）
        t = (t[:k] + "＊＊" + t[k + 2:]).strip()
    # 单行里既要围栏成对、围栏外的行内反引号也要成对。
    # 2026-11-18 修：原来只判 `t.count("`") % 2`，实测 reason 里出现 3 个 ```
    # （9 个反引号，奇数）时删掉一个 → 剩 8 个，但围栏还是 3 个，整卡照样奇数。
    # 2026-11-18 修（2000 组整卡模糊抓到）：原来写死 range(4)，而每轮只修**一个**
    # 落单反引号（取 bad[-1]）；note 里落单处多于 4 个时根本修不完，输出仍有奇数段
    # （实测 note 开头 7 个反引号里那个 ` 始终没被处理）。改成按长度跑到收敛。
    for _ in range(len(t) + 2):
        # 2026-11-18 修（用户贴的真实卡片抓到）：原来是「去掉最后那个」，
        # 实测 note「实测 reason 里 3 个 ``` 会漏」被输出成「3 个  会漏」——
        # 三个反引号整个消失，读者根本不知道说的是什么。配平不能靠删内容，
        # 改成替换成全角 ｀（视觉等价、不构成 markdown 标记、原文不丢）。
        if t.count("```") % 2 == 1:             # 围栏奇数 → 最后一个换全角
            i = t.rfind("```")
            if i < 0:
                break
            t = (t[:i] + "｀｀｀" + t[i + 3:]).strip()
            continue
        parts = t.split("```")
        bad = [k for k in range(0, len(parts), 2)
               if parts[k].count("`") % 2 == 1]
        if not bad:                             # 围栏外的反引号都成对了
            break
        k = bad[-1]
        j = parts[k].rfind("`")
        parts[k] = parts[k][:j] + "｀" + parts[k][j + 1:]
        t = "```".join(parts)
    return t.strip()


def _finalize(s: str, cap: int) -> str:
    """收尾：配平反引号 + 保证不超 cap（截断时不能再把围栏切坏）。

    2026-11-18 加：模糊测试（3000 组随机内容 × 6 种 cap）发现两处兜底截断
    会把刚补上的闭合围栏切掉。cap 小到放不下「``` + 闭合```」时，干脆把
    反引号全去掉 —— 宁可少几个符号，也不能输出半截围栏让整张卡被吞。
    """
    s = _balance_fence(s)
    if not cap or len(s) <= cap:
        return s
    # 2026-11-18 修（整卡模糊测试复现）：原来是「配平 → 最后再 s[:cap] 硬切一刀」，
    # 那一刀正好把刚补上的闭合 ``` 切掉，且切完**不再配平** —— 实测
    # _clip('|```\r\n\n``**`````python\n', 200) → '|```\n\n```````python'（围栏 3 个，奇数），
    # 后面整张卡又被吞。现在改成「切一刀→重新配平」循环，实在放不下才去掉反引号。
    for _ in range(3):
        if len(s) <= cap:
            break
        s = s[:cap]
        s = _balance_fence(s, drop_lone=True)
    if len(s) > cap:
        s = s[:cap]
    if s.count("```") % 2 == 1 or any(
            p.count("`") % 2 == 1 for p in s.split("```")[::2]):
        s = s.replace("`", "")[:cap]       # 最后兜底：宁可没反引号，也不留半截围栏
    # 2026-11-18 加（2000 组整卡模糊抓到 572 组）：上面的兜底截断只看了反引号，
    # 没管 ** —— 切在 ** 中间会输出单个 *，切掉配对的那个也会剩一个孤立的 **，
    # 那一行之后整段变粗体。这里统一再配一次。
    _bp = _bold_pos(s)
    if len(_bp) % 2 == 1:
        # 这里已经是「被截断过」的文本：落单的 ** 是切在粗体中间留下的语法标记，
        # 后面没有正文，直接去掉（换成 ＊＊ 会输出「：＊＊代…」这种像错字的东西）。
        k = _bp[-1]
        s = s[:k] + s[k + 2:]
    if s.endswith("*") and not s.endswith("**"):
        s = s.rstrip("*")                  # 被切成一半的 *
    return s if s.strip() else ""


def _clip(s, cap: int = 200, ellipsis: bool = True) -> str:
    """注入交接卡时用的截断（与 `_clean` 不同：它**保留换行**）。

    为什么单独写一个（2026-11-18）：
      1) `_clean` 把换行压成空格 —— 一条含列表/分段的回复被压平后，
         上一行结尾和下一行开头会粘成一个词。实测：「结论：通过」+ 换行 +
         「通过 14 / 失败 0」被压成「结论：通过 通过 14 / 失败 0」，
         读起来像我自己重复了一遍。
      2) 直接 `[:cap]` 截断**不留任何标记** —— 读的人（模型）会以为那就是全文；
         而且常常断在反引号中间，留下未闭合的 ` 把后文 markdown 全吞掉。
         实测真实交接卡里 24 条有 13 条是硬截断，其中多条反引号数为奇数。
    所以这里：按行压缩 → 超长补「…」→ 反引号配平。
    """
    text = str(s or "")
    if not text:
        return ""

    # 逐行规范化：压掉行内多余空白，但**保留行首缩进**和**段落之间的空行**。
    # 2026-11-18 修：原来用 " ".join(ln.split()) 会把缩进一起吃掉，
    # 嵌套列表变成平级（实测「- 第一层 /   - 第二层」输出后两层缩进全没了）；
    # 又把所有空行删掉，导致两段之间没有空行、markdown 里粘成一段。
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
    if not lines:
        return ""
    if not cap:
        return _balance_fence("\n".join(lines))

    # 按**行**累积，尽量不在行中间切断（markdown 的列表/表格/围栏才不会被撕开）
    reserve = 1 if ellipsis else 0
    budget = max(1, cap - reserve)
    out, total, cut = [], 0, False
    for ln in lines:
        add = len(ln) + (1 if out else 0)
        if total + add <= budget:
            out.append(ln)
            total += add
            continue
        cut = True
        room = budget - total - (1 if out else 0)
        if room > 0:
            seg = ln[:room]
            if seg.count("`") % 2 == 1:        # 行内代码：去掉未闭合的那个
                i = seg.rfind("`")
                if i >= 0:
                    seg = seg[:i] + seg[i + 1:]
            if seg.strip():
                out.append(seg)
        break

    body = "\n".join(out).rstrip("\n").rstrip()
    if not cut:
        # 2026-11-18 修：原来是「配平后直接 body[:cap]」——cap 小时会把刚补上的
        # 闭合围栏切掉。模糊测试最小复现：cap=5、输入「```」→ 输出 "```\n`"，
        # 围栏又变成 1 个，后面整张卡照样被吞。统一交给 _finalize 收尾。
        return _finalize(body, cap)
    # 截断了：先按「要补围栏就预留围栏长度」算出能留多少，再配平，
    # 否则刚补上的 ``` 会被下面的兜底截掉（实测 count=1，围栏又断了）
    need = body.count("```") % 2 == 1
    keep = cap - reserve - (len("\n```") if need else 0)
    if cap and len(body) > keep:
        body = body[:max(0, keep)].rstrip("\n").rstrip()
    # 2026-11-18 修（真实卡片抓到）：原来这里会「在被丢掉的部分含奇数个 ** 时，
    # 给 body 开头补一个 **」。实测 dialog[15]「我先核对基线，再写一批**新用例**…」
    # 本来 ** 是配对的，被补成「**我先核对基线，再写一批**新用例**…」——
    # 粗体从第 0 字开始，把正确的格式改坏了。补在开头只会让更多文字变粗体，
    # 方向就是错的。落单的 ** 交给 _balance_fence 换全角 ＊＊ 收尾即可。
    # 这里是**真的被切过**的地方：落单的 ** 是切在粗体中间留下的开标记，
    # 后面只剩半个词，换成 ＊＊ 会输出「：＊＊代…」这种像错字的东西，直接去掉。
    body = _balance_fence(body, drop_lone=True)
    return _finalize(body + ("…" if ellipsis else ""), cap)


def _similar(a: str, b: str) -> bool:
    """两条消息是不是「同一件事被记了两遍」。

    中断后重试会产生两条内容高度重复的 assistant 消息（实测：前 40 字
    完全相同，只是长度差几十字）。原样列两遍会让模型以为做了两轮。
    """
    if a == b:
        # 短消息重复往往是「用户催了两次 / 确认了两次」，是真实轮次，
        # 合并会让轮数少算（实测连发两次「继续」被并成一条 → 2 轮显示成 1 轮）。
        # 但 12 字以上完全相同，基本只可能是「中断后重试」，应该合并
        # （实测 36 字的重试消息被 40 的阈值漏掉，同一件事报了两遍）。
        return len(a) >= 12
    if not a or not b:
        return False
    # 一条正好是另一条的开头 → 中断后重试/续写，几乎肯定同一件事。
    # （实测：重试那条只在末尾多一句「（重试版）」，30 字，原来被 n<40 漏掉）
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
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = LAST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST)          # 原子替换，避免写一半断电把交接卡写坏
    except Exception:
        logger.debug("silent-except", exc_info=True)                           # 交接卡写失败**不能影响**正常对话/改代码


# =====================================================================
#  1) 记录一次改动（自开发工具写盘成功后调用）
# =====================================================================
def record_change(rel: str, note: str = "", added: int = 0, removed: int = 0,
                  act: str = "write") -> None:
    # 2026-11-18：存盘时就把反引号配平，别把「落单的 `」这种脏数据写进 JSON
    # （渲染端 _fmt_change 还有一道兜底，因为旧数据里已经存进去了）
    # 2026-11-18 修：原来用 _balance_fence(_clean(rel)) —— 单行 rel 里遇到未闭合围栏
    # 会补一个 "\n```"，于是存进 JSON 的 rel 带换行（渲染端虽然能兜住，但脏数据不该
    # 落盘，而且「同一文件合并」是按 rel 字符串比较的，带换行就合并不上）。
    # 2026-11-18 修：_inline 现在把落单反引号换成全角 ｀（为了不丢 note 内容），
    # 但 rel 是**文件路径**，反引号本来就非法、必须剔除。只 replace("`") 的话
    # 全角 ｀ 会被留下 —— 实测 rel="````" 变成 "｀｀｀｀" 当成文件名存进了 JSON。
    rel = _inline(rel, 120).replace("`", "").replace("｀", "").replace("*", "").strip()
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
    if ch and ch[-1].get("rel") == rel:
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
        role = str(m.get("role") or "")
        # 这里**不补省略号**（否则注入时再截一次会出现「……」，见 render）
        text = _clip(m.get("content"), 1200, ellipsis=False)
        if role not in ("user", "assistant") or not text:
            continue
        # 同角色 + 内容高度重复 → 只留一条（长的），避免同一件事报两遍
        if out and out[-1]["role"] == role and _similar(out[-1]["text"], text):
            if len(text) > len(out[-1]["text"]):
                out[-1] = {"role": role, "text": text}
            continue
        out.append({"role": role, "text": text})
    return out[-(MAX_DIALOG * 2):]


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
            # 2026-11-18 修：原来是 _clean(reason, 120) —— 硬截断不留标记，落盘后
            # 渲染端已经看不出「这里被截过」，读卡的人以为那就是完整原因。
            d["reason"] = _inline(reason, 120)
        _write(d)
        _LAST_TOUCH["session"] = session
        _LAST_TOUCH["ts"] = now
    except Exception:
        logger.debug("silent-except", exc_info=True)


# =====================================================================
#  3) 重启前快照
# =====================================================================
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
      这样即使调用方只传了中文 reason，也能被正确归类。
    """
    try:
        d = _read()
        if session:
            d["session"] = _clean(session, 64)
            d["dialog_session"] = _clean(session, 64)
        if reason:
            # 2026-11-18 修：同 touch()，_clean 是硬截断、不留标记，统一改用 _inline
            d["reason"] = _inline(reason, 120)
        # ★ 结构化原因：bootaudit 靠它决定跑不跑自检（见 bootaudit.should_run）
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
    # 2026-11-18 修：改动清单的 note/rel 原来直接拼进 markdown，**完全没配平**。
    # 实测真实卡片第 38 行「实测短消息里落单的 ` 会吞掉整张卡」——note 里这个
    # 落单的 ` 让整行反引号变成 3 个（奇数），后面剩余清单和 ⚠️ 提示全被当成
    # 行内代码吞掉。同时超长 note 是硬截断、不留标记（实测 323 字，无「…」）。
    # 2026-11-18 修：改用 _inline。原来 _clip 会补 "\n```"，而 rel/note 是单行上下文，
    # 补出来的闭合围栏跑到下一行，把清单条目撕成两行、整卡围栏数还变成奇数。
    # rel 外层已经用 ` 包了一层，所以它自己不能再带反引号（模糊测试里出现过
    # rel="````" → 输出 "- ````（+1 -0 行）" 这种）。
    # 2026-11-18 修：_inline 现在把落单反引号换成全角 ｀，所以这里必须连全角一起剔，
    # 否则旧数据里 rel 带反引号时（如 "```python\n"）整行反引号还是奇数。
    # 2026-11-18 加：路径里本来就不该有 *（Windows 非法字符）。剔掉它，
    # 免得 rel 的星号和 note 的 **粗体** 混在一起、整行配平时把 note 的闭合 ** 删掉
    # （实测 note="**重点** 说明" 被输出成 "**重点 说明"，粗体标记丢了）。
    rel = (_inline(it.get("rel"), 120)
           .replace("`", "").replace("｀", "")
           .replace("*", "").replace("＊", "").strip())
    note = _inline(it.get("note"), 160)
    while "  " in note:                    # 删掉落单 ` 后会留两个空格（实测「落单的  会吞掉」）
        note = note.replace("  ", " ")
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
    t = str(it.get("time") or "")
    if t:
        line += "　[%s]" % t[5:16]      # 只留「月-日 时:分」
    # 2026-11-18 加（2400 组模糊抓到 32 组）：rel/note 各自配平了，但**拼成一行后**
    # 还会互相干扰 —— 实测 rel 里带 "***"（路径里不该有，但旧数据可能有）时，
    # 整行 ** 变成 5 个（奇数），这一行之后的内容全被当成粗体。整行再配一次。
    _bp = _bold_pos(line)
    if len(_bp) % 2 == 1:
        # 同上：只算正文里的 **（note 里的 `` `**` `` 不算）；不删内容，换全角 ＊＊
        k = _bp[-1]
        line = line[:k] + "＊＊" + line[k + 2:]
    return line


def _last_assistant(dialog: list) -> str:
    for m in reversed(dialog):
        if m.get("role") == "assistant" and str(m.get("text") or "").strip():
            return str(m.get("text") or "").strip()
    return ""


def _tail(text: str, cap: int = TAIL_CAP) -> str:
    """取一段文字的最后 cap 字（跨会话时补「上次停在这里」用）。

    2026-11-18 修：原来用 " ".join(split()) 把换行**全压成空格**，
    列表/分段/代码块的结构全丢（和 `_clean` 那个 bug 同类，但这处漏了）。
    现在保留换行、按行对齐缩进、并配平反引号。
    """
    t = str(text or "")
    if not t.strip():
        return ""
    if len(t) > cap:
        dropped = t[:-cap]
        t = t[-cap:]
        nl = t.find("\n")
        if 0 <= nl < len(t) - 1:        # 别从半行开始
            dropped += t[:nl + 1]
            t = t[nl + 1:]
        t = "…" + t
        # 2026-11-18 修（真实卡片抓到）：截断把开头的 ** 切掉、只剩结尾的 **，
        # 实测输出「…这个标题在说谎**。现在修它。」→ 后面整段被当成粗体。
        # 只按「整串 ** 奇偶」判断抓不到这种（那段里共有 4 个 **，是偶数），
        # 必须看**被丢掉的前缀**里有没有落单的 **，有就补一个开头 ** 回来。
        # 2026-11-18 修（真实卡片抓到）：原来是 dropped.count("**") % 2 —— 又把
        # 行内代码里的 `**` 数进去了，于是**误判**成「开头有个落单粗体」，补了
        # 一个 "…**"，补完反而变奇数，最后被换成全角 ＊＊（输出「生成的＊＊。」）。
        # 只统计正文里的 **（_bold_pos 会跳过 `…` 与 ``` 围栏）。
        if len(_bold_pos(dropped)) % 2 == 1:
            t = "…**" + t[1:]
    t = _balance_fence(t)
    if "\n" in t:                        # 续行缩进，避免破坏「· 那个会话…」这一行的结构
        t = "\n".join(("  " + ln) if ln else "" for ln in t.split("\n"))
    return t


def render(session: str = "") -> str:
    """生成要注入的交接卡文本；没有可注入的内容就返回空串。"""
    d = _read()
    if not d:
        return ""
    try:
        ts = float(d.get("ts") or 0)
    except Exception:
        ts = 0
    if not ts or (time.time() - ts) > KEEP_DAYS * 86400:
        return ""                          # 太久以前的重启，别再占上下文

    changes = [c for c in (d.get("changes") or []) if isinstance(c, dict)]
    dialog = [m for m in (d.get("dialog") or []) if isinstance(m, dict)]
    if not changes and not dialog:
        return ""

    restarted = str(d.get("restarted_at") or "")
    if restarted:
        lines = ["【自我迭代交接卡 · 上次重启 %s】" % restarted]
    else:
        lines = ["【自我迭代交接卡 · 上次对话 %s（没有重启记录，可能是异常退出）】"
                 % (d.get("live_at") or "时间未知")]
    if d.get("reason"):
        # 2026-11-18 修：原来是 str(...)[:120] —— 硬截断不留标记，且 reason 里
        # 若有落单的 ` 会把这行之后的 markdown 全吞掉。统一走 _clip（配平+省略号）
        lines.append("原因：%s" % _inline(d.get("reason"), 120))

    # 这段前文是不是**当前这段对话**自己的 —— 是的话，它就是"刚才"，权重更高
    same = bool(session) and str(d.get("dialog_session") or "") == str(session)
    if dialog:
        # 落盘时已经去过重，但**旧数据**是旧代码写的、可能还留着「中断 + 重试」
        # 的两条重复（实测那份真实卡里第 13/14 条都是 assistant、内容重复）。
        # 这里再兜一次，保证注入出去的东西干净。
        _dd = []
        for _m in dialog:
            _t = str(_m.get("text") or "")
            if (_dd and _dd[-1].get("role") == _m.get("role")
                    and _similar(str(_dd[-1].get("text") or ""), _t)):
                if len(_t) > len(str(_dd[-1].get("text") or "")):
                    _dd[-1] = _m
                continue
            _dd.append(_m)
        dialog = _dd
        # 轮数按**真实用户消息条数**算。以前用 len(dialog)//2，一旦有条目
        # 被去重或多出一条 assistant，就会显示成 12 轮而实际只有 11 轮。
        _rounds = sum(1 for m in dialog if m.get("role") == "user") or 1
        lines.append("· %s（最近 %d 轮）："
                     % ("本会话刚才在聊" if same else "另一个会话当时在聊",
                        _rounds))
        n = len(dialog)
        for i, m in enumerate(dialog):
            who = "用户" if m.get("role") == "user" else "你"
            cap = LAST_CAP if i >= n - 2 else TEXT_CAP
            # 保留换行（别把分段/列表压成一长串），多行时缩进对齐
            body = _clip(m.get("text"), cap)
            _bl = body.split("\n")
            if len(_bl) > 24:                  # 长代码块/长列表：折叠，别把上下文占满
                _head = "\n".join(_bl[:24])
                # 2026-11-18 修：「原文共 N 行」原来用的是 _clip **之后**的行数，
                # 实测原文 60 行、clip 到 200 字后剩 30 行 → 提示谎报「共 30 行」。
                # 这里取原文行数（两者取大，保证不小于实际显示的行数）。
                _orig = len(str(m.get("text") or "").split("\n"))
                _note = "…（原文共 %d 行，已折叠显示前 24 行）" % max(_orig, len(_bl))
                # 折叠点可能正落在 ``` 代码块里 → 先补闭合围栏，提示放到围栏**外面**
                # （否则那句提示会被当成代码内容显示，实测就是这个样子）
                if _head.count("```") % 2 == 1:
                    body = _head + "\n```\n" + _note
                else:
                    body = _head + "\n" + _note
            if "\n" in body:
                # 续行缩进对齐；空行留空，别写成 "  "（markdown 里是两个空格＝硬换行）
                body = "\n".join(("  " + ln) if ln else "" for ln in body.split("\n"))
            lines.append("  %s：%s" % (who, body))

    # 同一段会话里，最后那句助手回复上面已经列过了，再补一遍纯属浪费上下文；
    # 只有「另一个会话」时才补一句结尾，帮模型接上别人那条线。
    last = _last_assistant(dialog)
    if last and not same:
        lines.append("· 那个会话最后停在这里：%s" % _tail(last))

    if changes:
        # 2026-11-18 修：原来一律写「那次重启前改过的文件」，但清单取的是「最近
        # SHOW_CHANGES 条」，里面**混着重启之后才做的改动**（实测真实卡片：上次重启
        # 02:34:17，清单里却有 02:36/02:37/02:41 共 6 条）。读卡的人会以为这些
        # 都发生在重启前、已经生效过，判断依据就错了。现在按 restarted_at 分成两组。
        _rt = str(d.get("restarted_at") or "")
        _pre_all, _post_all = [], []
        for _c in changes:
            _t = str(_c.get("time") or "")
            if _rt and _t and _t > _rt:
                _post_all.append(_c)       # 时间格式固定 "YYYY-MM-DD HH:MM:SS"，可直接比
            else:
                _pre_all.append(_c)
        # 2026-11-18 修（上一版拆分时我自己引入的回归）：原来是先取「最近
        # SHOW_CHANGES 条」再分组 —— 重启后又改了 12 条以上时，「重启前」那组
        # 被整个挤掉，标题只剩「重启之后又改过的」，交接卡最该交代的「重启前动过
        # 什么」反而没了（实测真实卡片就变成这样）。现在两组都有时，固定给
        # 「重启前」留 4 个位置，剩下的给「重启后」。
        if _pre_all and _post_all:
            _pre = _pre_all[-4:]
            _post = _post_all[-(SHOW_CHANGES - len(_pre)):]
        else:
            _pre = _pre_all[-SHOW_CHANGES:]
            _post = _post_all[-SHOW_CHANGES:]
        if _pre:
            _h = ("· 那次重启前改过的文件（最近 %d 个）：" % len(_pre)) if _rt \
                else ("· 最近改过的文件（最近 %d 个）：" % len(_pre))
            lines.append(_h)
            lines.extend(_fmt_change(c) for c in _pre)
        if _post:
            lines.append("· 重启之后又改过的文件（最近 %d 个）：" % len(_post))
            lines.extend(_fmt_change(c) for c in _post)

    lines.append(
        "⚠️ 这张卡是**上一次会话的背景**，用来接上前文（用户可能正在接着问刚才的事）；"
        "和用户当前这句话冲突时**以当前为准**。不要主动复述这张卡，"
        "除非用户问「刚才聊到哪 / 改了些什么」。")
    return "\n".join(lines)
