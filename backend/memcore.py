# -*- coding: utf-8 -*-
"""记忆条目的**统一比较内核**：把"一条记忆"抽象成 Fact，把"两条的关系"收敛成一个 relation()。

为什么要这个模块
----------------
memory.py 原来没有"条目表示"这层抽象，每条规则各自解析一遍字符串：
`_already` 一套、`_neg_flip` 一套、`_supersede` 一套、`_locate` 一套。
同一个问题（"这两条说的是不是同一件事"）有 4 份实现、4 套口径，
修好一处就被另一处绕过。实测踩过的连锁（2026-11-18 ~ 11-19）：

  1. 修好「不喜欢咖啡 / 喜欢咖啡」的纠正 → 被「无锡」绕过（地名含"无"）；
  2. 修好「无锡」→「（推断）」标记被当括号剥掉，推断降权失效；
  3. 「条目1」被「条目10」当成"信息更全的同一件事"，旧条目被吃掉。

这类 bug 的数量随"特判个数"超线性增长，所以根治办法不是继续加特判，
而是把**表示**和**比较**各收敛成一份。

设计
----
Fact 把一条记忆拆成 6 个字段：

    raw      原始文本（原样保留）
    body     归一化：去首尾空白与句末标点
    subject  冒号前的短标签（"专业：人工智能" → "专业"），没有就是 ""
    core     核心字面：剔掉否定词与尾部语气词，假否定词换成占位符
    neg      否定标记的**个数**（假否定词不计）
    num      结尾编号（"条目12" → "12"），没有就是 ""

relation(a, b) 只返回 5 种结果：

    SAME      字面完全相同
    COVERED   b 已被 a 涵盖（不用再记）
    SUPERSET  a 被 b 涵盖，且 b 的扩展部分是**补充说明**（原地替换成 b）
    FLIP      a 与 b 是同一件事的正反说法（原地替换成 b —— 用户改了口径）
    DIFF      不同的事

关键判据：**扩展部分是"补充说明"还是"另一个东西"**
    「计划考网络工程师」+「，报名费800元」  → 扩展以"，"开头 = 补充 → SUPERSET
    「条目1」          +「0」               → 扩展以数字开头 = 另一个编号 → DIFF
    「喜欢咖啡」        +「豆」              → 扩展是中文 = 另一个词 → DIFF
这一条同时解决了"加信息反而丢信息"和"编号前缀互吃"两个反向的 bug ——
它们在旧代码里是两处互相矛盾的特判。

纯函数、不做任何 IO，方便单独测。
"""
import re
import functools

__all__ = [
    "SAME", "COVERED", "SUPERSET", "FLIP", "DIFF",
    "Fact", "parse", "relation",
    "has_duplicate", "find_flip", "find_superset", "locate", "overlap",
]

# ---------------------------------------------------------------------------
#  关系常量
# ---------------------------------------------------------------------------
SAME = "same"
COVERED = "covered"
SUPERSET = "superset"
FLIP = "flip"
DIFF = "diff"

# 否定标记单字。「不/没/无/未/别」覆盖中文里绝大多数否定表述。
_NEG_MARKS = ("不", "没", "无", "未", "别")

# 「含否定字、但不是否定」的常见词：地名 / 食品 / 技术词 / 固定搭配。
# 不剔掉它们，「无锡」「无糖」「无线」会把整句判成否定句 ——
# 于是「喜欢无锡」和「不喜欢无锡」被判成**同为否定**，纠正识别不出来。
_NEG_FALSE = (
    "无锡", "无糖", "无线", "无人机", "无数", "无论", "无效", "无关", "无限",
    "无疑", "无损", "无奈", "无所谓", "未来", "别人", "别的",
    "区别", "类别", "级别", "性别", "告别", "识别", "别墅", "不错",
    "不断", "不仅", "不但", "不少", "不久", "不同", "不论", "不光",
    "没关系", "没错", "没准", "没意思",
    # 含「别」的实词。`_NEG_MARKS` 里有「别」，不剔掉的话
    # 「分别是什么」「两者差别」「个别情况」都会被判成否定句。
    "分别", "差别", "个别", "鉴别", "辨别", "离别", "送别", "惜别",
    "道别", "作别", "阔别", "久别", "临别", "拜别", "辞别", "话别",
)
# 按长度降序替换：「无人机」必须先于「无人」，否则会被截成「机」。
_NEG_FALSE_SORTED = tuple(sorted(_NEG_FALSE, key=len, reverse=True))

# 假否定词的占位符：**保留位置**而不是直接删掉。
# 直接删的话「喜欢无锡」和「喜欢」的 core 都是「喜欢」，两者会被误判成同一件事；
# 用占位符则 core 是「喜欢\x01」vs「喜欢」，能区分开。
_PH = "\x01"

# 句末语气词：去掉才能让「打算考四级」和「不打算考四级**了**」的 core 对齐。
_TAIL_PARTICLES = "了啦呢吧啊呀哦的嘛"

# 扩展部分是"补充说明"的开头字符。其余（数字/字母/汉字）一律视为"另一个东西"。
#   「…工程师」+「，报名费800元」→ 补充
#   「条目1」  +「0」            → 另一个编号
#   「喜欢咖啡」+「豆」           → 另一个词
_SUPPLEMENT_PREFIX = "，,、：:（(【[「『 \t　-—"

# 程度副词：**直接删掉**，不占位。
# 它们在 `_NEG_FALSE` 里只是因为含"非/不"，本身不影响极性 ——
# 若按实词占位，「非常喜欢跑步」的 core 会变成「用户\x01喜欢跑步」，
# 和「不喜欢跑步」的「用户喜欢跑步」对不上，正反说法就识别不出来了（对比测试实测）。
_DEGREE_WORDS = ("非常", "特别", "十分", "极其", "格外", "相当", "有点", "稍微", "比较")

_SUBJECT_RE = re.compile(r"^([^：:\s]{1,8})[：:]\s*(.+)$")
_NUM_RE = re.compile(r"(?:第)?([0-9]+)\s*(?:条|个|项|章|节|号|款|步|轮|批)?$")

_BODY_STRIP = "。.；;，,、！!？?　 -•*"


# ---------------------------------------------------------------------------
#  Fact
# ---------------------------------------------------------------------------
class Fact:
    """一条记忆的解析结果。构造请用 `parse()`，它带缓存。"""

    __slots__ = ("raw", "body", "core", "neg", "subject", "num")

    def __init__(self, raw, body, core, neg, subject, num):
        self.raw = raw
        self.body = body
        self.core = core
        self.neg = neg
        self.subject = subject
        self.num = num

    def __repr__(self):
        return ("Fact(body=%r, core=%r, neg=%d, subject=%r, num=%r)"
                % (self.body, self.core, self.neg, self.subject, self.num))

    def __eq__(self, other):
        return isinstance(other, Fact) and (
            self.body == other.body and self.core == other.core
            and self.neg == other.neg and self.num == other.num)

    def __hash__(self):
        return hash((self.body, self.core, self.neg, self.num))


def _has_neg_mark(s):
    for m in _NEG_MARKS:
        if m in s:
            return True
    return False


def _strip_false_neg(s, placeholder):
    """把假否定词换成 placeholder（None 表示直接删掉）。"""
    t = s
    for w in _NEG_FALSE_SORTED:
        if w in t:
            t = t.replace(w, _PH if placeholder else "")
    return t


def _core(body):
    """核心字面：剔否定词 + 尾部语气词，假否定词换成占位符。"""
    if not body:
        return ""
    # 快速路径：整句既没有否定标记字、也没有程度副词，就不必跑几十次 replace。
    # ⚠️ 程度副词必须一起判断 ——「用户非常喜欢跑步」不含任何否定标记字，
    # 只查否定标记会走快速路径，「非常」就漏删了，core 和「喜欢跑步」对不上（实测）。
    if not _has_neg_mark(body) and not any(w in body for w in _DEGREE_WORDS):
        return body.strip().rstrip(_TAIL_PARTICLES).strip()
    t = _strip_false_neg(body, placeholder=True)
    # 程度副词直接删（不占位）：「非常喜欢跑步」要和「不喜欢跑步」对得上 core。
    for w in _DEGREE_WORDS:
        if w in t:
            t = t.replace(w, "")
    # 「没有」必须**整词删**：只删单字「没」会剩下「有每天跑步」，和「每天跑步」对不上。
    # 而「不想/不会/不能」**不能**整词删 —— 那会把「想/会/能」也删掉，
    # 于是「想换工作」和「不想换工作」的 core 对不上（实测 3 条纠正全漏）。
    t = t.replace("没有", "")
    for m in _NEG_MARKS:
        t = t.replace(m, "")
    return t.strip().rstrip(_TAIL_PARTICLES).strip()


def _neg_count(body):
    """否定标记的**个数**（假否定词与程度副词不计）。"""
    if not body or not _has_neg_mark(body):
        return 0
    t = _strip_false_neg(body, placeholder=False)
    # ⚠️ 程度副词必须先删：「特别」里的「别」会被 `_NEG_MARKS` 数成一次否定，
    # 于是「特别学过设计」neg=1、「学过设计了」neg=0 → 被误判成同一件事的正反说法。
    for w in _DEGREE_WORDS:
        if w in t:
            t = t.replace(w, "")
    return sum(t.count(m) for m in _NEG_MARKS)


@functools.lru_cache(maxsize=4096)
def parse(text):
    """把一条记忆文本解析成 Fact（带缓存 —— 长期记忆每次 merge 要 parse 上百条）。"""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    raw = raw.strip()
    body = raw.strip(_BODY_STRIP)
    if not body:
        return Fact(raw, "", "", 0, "", "")
    subject = ""
    m = _SUBJECT_RE.match(body)
    if m:
        subject = m.group(1).strip()
    core = _core(body)
    neg = _neg_count(body)
    num = ""
    m2 = _NUM_RE.search(body)
    if m2:
        num = m2.group(1)
    return Fact(raw, body, core, neg, subject, num)


# ---------------------------------------------------------------------------
#  关系判定
# ---------------------------------------------------------------------------
def _is_coverable(tail):
    """扩展部分是"补充说明"、或者根本没有扩展（b 一直延伸到 a 的结尾）吗。

    tail 为空 = 「b 完整地出现在 a 里，且到 a 的结尾为止」——
    这时 b 的信息 a 全都有，属于涵盖关系，**不能**判成"另一个东西"。
    实测（新旧对比 2100 次）：已有「用户姓名：陈宇桐」，新来「陈宇桐」，
    旧实现判涵盖（不重复记），新实现一度判成 DIFF（会存一条无意义的裸名字）。
    """
    return (not tail) or (tail[0] in _SUPPLEMENT_PREFIX)


def relation(a, b):
    """a 是**已有条目**，b 是**新来的要点**，返回 SAME/COVERED/SUPERSET/FLIP/DIFF。"""
    if not a.body or not b.body:
        return DIFF
    if a.body == b.body:
        return SAME

    # ① 正反说法：核心字面相同、否定个数不同 → 同一件事的两个方向。
    #    **必须排在包含判断之前** ——「喜欢咖啡」是「不喜欢咖啡」的子串，
    #    先判包含会把它当成 COVERED，纠正就永远识别不出来。
    if a.core and a.core == b.core and a.neg != b.neg:
        return FLIP

    # ② 包含关系。扩展部分是补充说明才算"涵盖"，否则是另一个东西。
    if a.body in b.body:
        pos = b.body.find(a.body)
        if _is_coverable(b.body[pos + len(a.body):]):
            return SUPERSET
        return DIFF
    if b.body in a.body:
        pos = a.body.find(b.body)
        tail = a.body[pos + len(b.body):]
        if not tail:
            # b 是 a 的**后缀**（「陈宇桐」之于「用户姓名：陈宇桐」）。
            # ⚠️ 但这时要看 a 的前缀有没有否定标记：
            # 「喜欢咖啡」是「**不**喜欢咖啡」的后缀，语义完全相反，
            # 判成涵盖会让用户的纠正被静默吞掉（新旧对比实测）。
            return COVERED if _neg_count(a.body[:pos]) == 0 else DIFF
        if _is_coverable(tail):
            return COVERED
        return DIFF

    return DIFF


# ---------------------------------------------------------------------------
#  对一组条目做查询（memory.py 直接调这几个）
# ---------------------------------------------------------------------------
def has_duplicate(items, point):
    """point 是否已被 items 里某条覆盖（SAME 或 COVERED）。"""
    b = parse(point)
    if not b.body:
        return False
    for e in items:
        a = parse(e)
        if not a.body:
            continue
        if relation(a, b) in (SAME, COVERED):
            return True
    return False


def find_flip(items, point):
    """在 items 里找「同一件事、肯定/否定相反」的那条，返回下标；没有返回 -1。"""
    b = parse(point)
    if not b.body or len(b.core) < 3:
        return -1                      # 太短（「咖啡」）容易误配，宁可不改
    for i, e in enumerate(items):
        a = parse(e)
        if not a.body:
            continue
        if relation(a, b) == FLIP:
            return i
    return -1


def find_superset(items, point):
    """在 items 里找「被 point 涵盖、且 point 是补充说明」的那条，返回下标；没有返回 None。"""
    b = parse(point)
    if not b.body:
        return None
    for i, e in enumerate(items):
        a = parse(e)
        if not a.body or a.body == b.body:
            continue
        if relation(a, b) == SUPERSET:
            return i
    return None


# ---------------------------------------------------------------------------
#  相似度（供"定位某一条去改/去删"用）
# ---------------------------------------------------------------------------
def _grams(text, n=2):
    t = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def overlap(a, b):
    """两条要点的相似度 0~1：互相包含 → 1.0，否则取「序列比例」与「2元组覆盖度」的较大值。

    为什么不能只用 SequenceMatcher：模型改写措辞时字序会变，会明显低估 ——
    「考网络工程师」与「已报名网络工程师考试，2027年3月开班，正在看教材」
    的序列比例只有 0.40，刚好卡在匹配阈值下方；2 元组覆盖度是 0.80。
    分母取**较短**那一侧（问的是"短的那句有多少能在长的那句里找到"）。
    """
    a, b = parse(a).body, parse(b).body
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a, b).ratio()
    ga, gb = _grams(a), _grams(b)
    cov = len(ga & gb) / max(1, min(len(ga), len(gb)))
    return max(ratio, cov)


def locate(items, hint, threshold=0.5):
    """在 items 里找与 hint 最像的那条，返回 (下标, 相似度)。

    短定位词保护：`overlap` 里"被包含"直接给 1.0，于是删「用户」会把
    「用户姓名：陈宇桐」整条删掉。宁可改不到，也不能改错 —— 但一刀切拒绝
    又会让「考四级」「陈宇桐」这类 3 字定位词永远匹配不到（forget 删不掉、
    update 只能退化成追加，正是"记了却不更新"的来源）。
    折中：**3 个字**的定位词，若只出现在**唯一一条**里，就认它；2 字仍保守。
    """
    hint_body = parse(hint).body
    best_i, best_s = -1, 0.0
    for i, e in enumerate(items):
        sc = overlap(e, hint)
        if sc > best_s:
            best_i, best_s = i, sc
    if best_i < 0:
        return -1, 0.0
    if len(hint_body) < 4 and parse(items[best_i]).body != hint_body:
        if len(hint_body) >= 3:
            hits = [i for i, e in enumerate(items) if hint_body in parse(e).body]
            if len(hits) == 1:
                return hits[0], 1.0
        return -1, 0.0
    return best_i, best_s
