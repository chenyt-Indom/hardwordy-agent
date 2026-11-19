# -*- coding: utf-8 -*-
"""技术论文/情报：持久化 + 向量检索 + **四维评估** + 落地计划。

用户 2026-11-15 的要求（原话要点）
--------------------------------
· 「抓取到的那些论文也需要使用向量化提高召回率」——
  不能每次反思只把**这一次**抓到的 30 条塞进 prompt，历史抓过的就丢了；
  要能按主题从**累积库**里召回相关的（哪怕它是两周前抓的）。
· 「智能体需要主动思考论文的可行性以及创新性，是否能够给用户带来效益
  以及工作效率的提高」——四维打分，且必须说清理由。
· 「然后思考如何自迭代并给出周密计划」——每条高价值论文要给
  分步骤计划（改哪个文件、风险、工作量、**怎么验证**）。

为什么单独一个模块
------------------
`self_iterate.py` 已经 1700+ 行，反思流程本身很绕。论文这块有独立的
存储、索引、评估三条链路，塞进去会让人不敢改。这里只依赖 `embed`。

存储
----
`data/self_iterate/papers.jsonl` —— 一行一篇（追加写），按 url/title 去重。
`data/self_iterate/_paper_vec.npy` + `_paper_meta.json` —— 向量与元信息。

⚠️ 和 `kb.py` / `hivevec.py` 一样：**embedding 不可用时退化为关键词检索**，
不能让"没装模型"把整个情报功能弄挂。
"""
import os
import re
import json
import time
import hashlib
import logging
import threading
from pathlib import Path

import numpy as np

from . import embed
from . import jsonfix as _jsonfix
logger = logging.getLogger(__name__)

_LOG = logging.getLogger("uvicorn.error")

_DATA = Path(__file__).resolve().parent.parent / "data" / "self_iterate"
_PAPERS = _DATA / "papers.jsonl"
_VEC = _DATA / "_paper_vec.npy"
_META = _DATA / "_paper_meta.json"

MAX_KEEP = 600              # 最多保留多少篇（超了按时间淘汰最老的）
MIN_VEC = 0.40              # 论文摘要短、信息稀，阈值比知识库(0.46)松一点
MIN_COV = 0.42

_lock = threading.Lock()


# ------------------------------------------------------------------ 基础
def _ensure():
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("silent-except", exc_info=True)


def _read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(p, obj):
    try:
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, str(p))
        return True
    except Exception as e:
        _LOG.warning("[papervec] 写 %s 失败：%s", p, e)
        return False


def _key(it: dict) -> str:
    """去重键：优先 url，其次归一化标题。

    ⚠️ 2026-11-19 实测缺陷：arXiv 同一篇论文的 v1 / v2 是**两条不同的 url**，
    原来直接拿整条 url 当键，于是同一篇论文在库里存了 2 条
    （实测 2611.03103 的 v1 与 v2 各一条，标题、作者完全相同）。
    后果：检索时一篇占掉两个 top_k 名额，四维评估也算两遍（白花算力）。
    这里先把 arXiv 的版本号去掉再比 —— v1/v2/v3 就是同一篇。
    """
    u = str(it.get("url") or "").strip().lower()
    if u:
        u = re.sub(r"(arxiv\.org/abs/[^/?#\s]+?)v\d+(?=$|[?#])", r"\1", u)
        return "u:" + u.rstrip("/")
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(it.get("title") or "").lower())
    return "t:" + t[:120]


def load_all() -> list:
    _ensure()
    out = []
    try:
        if _PAPERS.is_file():
            with open(_PAPERS, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        j = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(j, dict) and j.get("title"):
                        out.append(j)
    except Exception as e:
        _LOG.warning("[papervec] 读库失败：%s", e)
    return out


def _rewrite(items: list):
    """整体重写（去重/淘汰时用）。"""
    _ensure()
    try:
        tmp = str(_PAPERS) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, str(_PAPERS))
    except Exception as e:
        _LOG.warning("[papervec] 重写库失败：%s", e)


# ------------------------------------------------------------------ 入库
def add_batch(items, source_hint: str = "") -> dict:
    """把一批技术动态入库（去重）。返回 {added, dup, total}。"""
    _ensure()
    items = [i for i in (items or []) if isinstance(i, dict) and i.get("title")]
    if not items:
        return {"added": 0, "dup": 0, "total": len(load_all())}
    with _lock:
        old = load_all()
        seen = {_key(x): x for x in old}
        added = 0
        now = time.time()
        for it in items:
            k = _key(it)
            if k in seen:
                # 已经有的：更新一下最近出现时间（用于判断"又被提到"= 可能重要）
                seen[k]["seen_ts"] = now
                seen[k]["seen_n"] = int(seen[k].get("seen_n") or 1) + 1
                continue
            row = {
                "title": str(it.get("title") or "")[:300],
                "url": str(it.get("url") or "")[:500],
                "summary": str(it.get("summary") or "")[:1200],
                "source": str(it.get("source") or source_hint or "")[:40],
                "ts": now,
                "seen_ts": now,
                "seen_n": 1,
                "eval": None,            # 四维评估结果（评估过才有）
                "eval_ts": 0,
            }
            # ★ 2026-11-19 用户要求「按作者/年份/来源找指定论文」：
            #   原来入库只留 title/url/summary/source，作者和年份全丢了，
            #   导致「找张三 2024 年的那篇」根本无从过滤。这里补两个字段。
            #   authors 统一存**列表**，抓取层没给就留空列表（不编造）。
            _au = it.get("authors") or []
            if isinstance(_au, str):
                _au = [x.strip() for x in re.split(r"[,，;；、]", _au) if x.strip()]
            row["authors"] = [str(x)[:80] for x in _au[:12] if str(x).strip()]
            try:
                row["year"] = int(it.get("year") or 0)
            except Exception:                               # noqa: BLE001
                row["year"] = 0
            row["venue"] = str(it.get("venue") or "")[:80]
            old.append(row)
            seen[k] = row
            added += 1
        if len(old) > MAX_KEEP:
            old.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
            old = old[:MAX_KEEP]
        _rewrite(old)
        total = len(old)
    if added:
        try:
            build_index(force=False)
        except Exception:
            logger.debug("silent-except", exc_info=True)
    return {"added": added, "dup": len(items) - added, "total": total}


# ------------------------------------------------------------------ 索引
# 否定表述：出现在中文要点里时，整句都不能进索引文本。
# ⚠️ 为什么必须有这条（2026-11-19 实测）：172 篇里有 **31 篇**（18%）的
#    zh_points 是「…和本地多模态助手、**口型同步**、文档生成、知识库检索
#    完全**无关**。」这种否定句。否定句里恰好堆满了主题词，于是查「口型同步」
#    时它靠这句话拿到 gram=3 / cov=1.00，把「清理你的电视」「FAA 全国停飞令」
#    「微软故障致氯气中毒」全召回了 —— 语义与用户要的**完全相反**。
#    实测修复前「口型同步」top5 全无关，修复后只剩真正讲口型/唇动的。
_NEG_RE = re.compile(r"(无关|不相关|不适用|不具备|不涉及|并非|没有直接|无直接|无关联|关系不大)")
# 指代「本项目」的词：只有和否定词同时出现，才判定为「与本项目的关系判断句」。
# ⚠️ 为什么需要这两张表配合（2026-11-19 实测第二轮）：第一版只列了「无关/不相关」，
#    结果还有 3 篇用的是「均**无技术关联**」「**没有任何**可迁移的」「**没有可识**」——
#    换个说法就漏了。但反过来，只凭「没有」二字就删句会误伤论文正文
#    （如「该方法与基线相比没有显著提升」是有效内容）。所以要求：句子里
#    必须出现指代本项目的词，才说明这句是在讲「和我们有没有关系」。
_SELF_REF = ("本地多模态助手", "本项目", "本功能", "该项目", "本助手", "技术栈", "助手")
_NEG_LOOSE = ("均无", "没有任何", "无任何", "没有可", "无法确认", "没有", "无技术", "不适用")
_SENT_SPLIT = re.compile(r"[。；;\n]+")


def _is_neg_sentence(p: str) -> bool:
    if _NEG_RE.search(p):
        return True
    return any(w in p for w in _SELF_REF) and any(w in p for w in _NEG_LOOSE)


def _strip_neg(text: str) -> str:
    """剔除「与本项目无关」这类关系判断句（整句丢弃，不做局部删词）。"""
    s = str(text or "")
    if not s:
        return ""
    parts = _SENT_SPLIT.split(s)
    kept = [p.strip() for p in parts if p.strip() and not _is_neg_sentence(p)]
    return "。".join(kept)


def _doc_text(it: dict) -> str:
    """索引用的文本 = 原标题 + 中文标题 + 原摘要 + 中文要点（去掉否定句）。

    ⚠️ 为什么必须加中文（2026-11-15 实测）：库里 30 条里 arXiv 那 8 条是
    纯英文摘要，而用户提问是中文。用 bge-small-zh 检索「向量检索」「显存」
    在英文摘要上**一条都召不回**（中文的字一个都不在英文文本里，语义分也
    只有 0.30 上下）。同一个查询换成英文（"memory efficient model"）
    立刻能到 0.54。所以中文这层不是锦上添花，是能不能用的分界线。
    """
    parts = [it.get("title") or ""]
    if it.get("zh_title"):
        parts.append(str(it["zh_title"]))
    if it.get("summary"):
        parts.append(str(it["summary"]))
    if it.get("zh_points"):
        # 见 _NEG_RE 上方注释：否定句会把「完全无关」变成强相关证据
        parts.append(_strip_neg(it["zh_points"]))
    return "\n".join(p for p in parts if p)


def build_index(force: bool = False) -> dict:
    items = load_all()
    if not items:
        return {"ok": True, "n": 0, "embedded": 0}
    sig = embed.signature()
    # 文本指纹：论文被补上中文标题/要点后，向量必须重算 —— 只比条数会漏。
    th = hashlib.sha1("|".join(_doc_text(i) for i in items).encode("utf-8")).hexdigest()[:16]
    old = _read_json(_META, {}) or {}
    old_items = old.get("items") or []
    old_mat = None
    if ((not force) and sig == old.get("sig") and old.get("text_hash") == th
            and len(old_items) == len(items) and _VEC.is_file()):
        try:
            arr = np.load(_VEC)
            if arr.ndim == 2 and arr.shape[0] == len(items):
                old_mat = arr
        except Exception:
            old_mat = None
    if old_mat is not None:
        return {"ok": True, "n": len(items), "embedded": 0, "reused": True}

    texts = [_doc_text(it) for it in items]
    mat = embed.embed_texts(texts)
    if mat is None:
        return {"ok": False, "n": len(items), "embedded": 0,
                "error": "向量模型不可用（检索会退化为关键词）"}
    try:
        np.save(_VEC, mat)
    except Exception as e:
        _LOG.warning("[papervec] 保存向量失败：%s", e)
    _write_json(_META, {"sig": sig, "ts": time.time(), "n": len(items),
                        "text_hash": th, "dim": int(mat.shape[1]), "items": [
                            {"title": it.get("title"), "url": it.get("url")}
                            for it in items]})
    return {"ok": True, "n": len(items), "embedded": len(texts)}


def _chars(s):
    return [c for c in str(s or "") if ("\u4e00" <= c <= "\u9fff") or c.isalnum()]


_STOP = set("的了吗呢吧啊呀是在有和与及对把被从到就都很也还要会能可以这那个我你他她它们什么怎么如何为哪")


_EN_WORD = re.compile(r"[A-Za-z]{3,}")


def _coverage(q, text):
    """查询覆盖度：中文按**字**，英文按**整词**。

    ⚠️ 英文为什么必须按词（2026-11-19 实测，这是「口型同步」翻车的真凶）：
    原来一律按字符算，而中文查询会被 need2query 扩展成英文（「口型同步」→
    `lip sync`）。"lip sync" 拆成 l/i/p/s/y/n/c 七个字母，**任何英文摘要**
    都能覆盖满，cov 直接 1.00 —— 这道门槛对英文查询等于完全不存在，
    于是「清理你的电视」「FAA 停飞令」全被当成口型同步的命中。
    改成按词后：`lip`/`sync` 两个词在库里一篇都没有 → cov=0，正确拒绝。
    """
    q = str(q or "")
    zh = [c for c in q if "\u4e00" <= c <= "\u9fff"]
    en = _EN_WORD.findall(q)
    scores = []
    if zh:
        qs = [c for c in zh if c not in _STOP] or zh
        ts = set(_chars(text))
        scores.append(sum(1 for c in qs if c in ts) / float(len(qs)) if ts else 0.0)
    if en:
        tl = str(text or "").lower()
        scores.append(sum(1 for w in en if _en_word_in(w.lower(), tl)) / float(len(en)))
    return max(scores) if scores else 0.0


def _grams(s, n=2):
    """**只对中文连续段**生成 2-gram。

    ⚠️ 拉丁字符不生成（2026-11-19 实测）：`lip sync` 的 `li`/`ip`/`sy`/`nc`
    在英文摘要里几乎必然出现（in / encoder / limiting 全中），
    `_GENERIC_GRAM` 又只收了中文词，英文 2-gram 门槛形同虚设。
    英文交给 `_en_hits` 按整词判。
    """
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", str(s or "")):
        if len(seg) >= n:
            out.extend(seg[i:i + n] for i in range(len(seg) - n + 1))
    return out


def _en_word_in(w: str, tl: str) -> bool:
    """整词判断（带边界）。⚠️ 不能用 `w in tl`：子串匹配下 `lip` 会命中
    `ellipse`（e-l-l-i-p-s-e 里正好有 l-i-p），实测把「椭圆采样」判成了口型同步。"""
    return re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", tl) is not None


def _en_hits(q, text):
    """英文查询有多少个**完整单词**出现在文本里（长度>=3，泛词不计）。"""
    ws = [w.lower() for w in _EN_WORD.findall(str(q or ""))]
    ws = [w for w in ws if w not in _GENERIC_EN]
    if not ws:
        return 0
    tl = str(text or "").lower()
    return sum(1 for w in ws if _en_word_in(w, tl))


# 通用词 bigram：命中它们**不算**有效命中。
# ⚠️ 实测（2026-11-15）：查「量子计算」时某篇论文的中文要点里恰好有「计算」，
#   bigram 命中 1 个、覆盖率也到了 0.5 —— 靠一个泛词召回等于没拦。
#   把这类词排掉后，「显存占用」仍靠「显存」命中，「视频生成」靠「视频」命中，
#   而「量子计算」剩 0 个有效命中，正确拒绝。
_GENERIC_GRAM = {
    "计算", "系统", "方法", "模型", "数据", "技术", "问题", "效果", "提升", "应用",
    "优化", "加速", "生成", "处理", "分析", "能力", "平台", "工具", "研究", "方案",
    "结果", "方式", "过程", "结构", "性能", "质量", "效率", "成本", "时间", "内容",
    "信息", "知识", "学习", "训练", "推理", "支持", "实现", "设计", "开发", "服务",
    "功能", "需求", "用户", "任务", "工作", "产品", "项目", "管理", "不同", "主要",
    # ⚠️ 2026-11-19 补：实测查「口型同步」时，某篇 VLA 延迟基准的中文要点里
    #    有「同步」二字 → zh_gram=1、cov=0.75，于是被判成命中。
    #    这类词在科技文本里到处都是，单独出现不能证明主题相关。
    "同步", "测试", "部署", "框架", "算法", "架构", "流程", "指标", "场景",
    "模块", "接口", "版本", "配置", "检测", "识别", "分割", "压缩", "检索",
    "问答", "对话", "翻译", "摘要", "分类", "预测", "评估", "标注", "存储",
    "传输", "加密", "权限", "日志", "监控", "调度", "并发", "缓存", "集群",
}

# 英文泛词：理由同上（2026-11-19 实测）。need2query 会把「口型同步」扩成
# `talking head generation`，而 `head`/`generation` 在论文标题里遍地都是，
# 命中一个就算过门槛 → 无关论文被召回。主体名词（video/image/model）**故意
# 不列**，否则「视频生成」这类真查询会被削成 0。
_GENERIC_EN = {
    "generation", "generative", "head", "based", "approach", "method", "methods",
    "framework", "frameworks", "system", "systems", "data", "learning", "training",
    "train", "inference", "task", "tasks", "performance", "efficiency", "efficient",
    "quality", "analysis", "large", "neural", "network", "networks", "deep",
    "towards", "using", "via", "for", "and", "the", "with", "from", "into",
    "sync", "synchronization", "synchronized",
}


def _gram_hits(qgrams, text):
    """有多少个 2-gram 真的出现在文本里（通用词不算）。

    ⚠️ 为什么必须有这条（2026-11-15 实测）：论文库**全是科技文本**，
    所以任意中文科技查询跟任意论文的语义相似度都在 0.40~0.46 ——
    查「量子计算」拿到 0.454，查「口型同步」拿到 0.358，全都够过阈值，
    于是返回一堆毫不相关的论文（用户看到的是"它在瞎编"）。
    而这两条查询的 2-gram 在库里**一个都没有**；真正的命中
    （「显存占用」→ 命中「显存」）一定有。所以用 2-gram 做硬门槛。
    """
    if not qgrams:
        return 0
    ts = "".join(_chars(text))
    if not ts:
        return 0
    return sum(1 for g in qgrams if g in ts and g not in _GENERIC_GRAM)


# ------------------------------------------------ 按作者/年份/来源过滤（2026-11-19）
# 用户要求：「检索时能按作者或其他内容找出指定论文」。
# 判据全部基于真实字段，不做拼音/模糊联想 —— 宁可查不到，也不要张冠李戴。
def _norm_name(s: str) -> str:
    """人名归一：去空格、点、连字符，转小写。

    ⚠️ 为什么必须归一（真实数据）：arXiv 返回的作者名有 "Yao, Shunyu" 和
    "Shunyu Yao" 两种写法，GitHub 的 owner 又是 "openai" 这种全小写；
    不归一的话「Shunyu Yao」搜不到 "Yao, Shunyu"。
    """
    s = str(s or "").strip().lower()
    return re.sub(r"[\s\.\-_,，、]+", "", s)


def _name_tokens(s: str) -> list:
    """按空格/逗号/点/连字符切词（人名用）。"""
    return [t for t in re.split(r"[\s\.\-_,，、]+", str(s or "").strip().lower())
            if t]


def _name_eq(a: str, b: str) -> bool:
    """两个人名是否指同一人 —— **词序无关**。

    ⚠️ 2026-11-19 实测缺陷：上面 `_norm_name` 的注释说「不归一的话
    『Shunyu Yao』搜不到『Yao, Shunyu』」，但归一后是 shunyuyao / yaoshunyu，
    `want in got` 两个方向**都不成立** —— 按英文作者名查其实永远 0 条。
    这里补一条词序无关的判据：切词后正序或逆序相等即算同一人。
    中文名没有空格、切词只有一个 token，仍走原子串逻辑，不受影响。
    """
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    return ta == tb or ta == list(reversed(tb))


def _year_of(it: dict) -> int:
    """取条目的年份：优先显式 year 字段，否则从 ts 推算。"""
    try:
        y = int(it.get("year") or 0)
        if y > 1900:
            return y
    except Exception:                                   # noqa: BLE001
        pass
    try:
        import datetime
        return datetime.datetime.fromtimestamp(
            float(it.get("ts") or 0)).year
    except Exception:                                   # noqa: BLE001
        return 0


def _author_match(it: dict, author: str) -> bool:
    """这条论文的作者里是否包含 author。

    · 比 authors 列表（词序无关，兼容「姓, 名」与「名 姓」）；
    · **有 authors 字段就只认字段**，不再退回摘要里找。

    ⚠️ 2026-11-19 实测缺陷（旧注释写着「authors 为空时退回标题/摘要」，
    但代码是**无条件**退回，和注释不符）：
      搜作者「OpenAI」命中了《The Compression Paradox in LLM Inference》，
      它的作者是 Warren Johnson —— 只是摘要里提到 OpenAI 而已。
      「摘要里提到某人」≠「他是作者」，这种误命中会让「按作者找指定论文」
      变得不可信。
    为什么现在敢改严格：库里 118/171 条已有真实 authors（arXiv 107 条按
    arXiv 官方 API 回填、GitHub 12 条按仓库 API 回填），覆盖了全部论文类
    条目；剩下 53 条是 HN / 中文媒体的**新闻**，本来就没有论文作者，
    让它们出现在「按作者查论文」的结果里只会误导。
    """
    want = _norm_name(author)
    if not want:
        return True
    for a in (it.get("authors") or []):
        # 用 _name_eq：兼容「Yao, Shunyu」与「Shunyu Yao」两种写法
        if _name_eq(a, author):
            return True
    return False


def filter_items(author: str = "", year: int = 0, source: str = "",
                 top_k: int = 20) -> list:
    """不带主题、只按条件筛（用户说「找张三 2024 年的那篇」但没给题目时用）。

    排序：有评估分的按均分降序，其余按入库时间降序 —— 保证「最值得看的」在前。
    """
    items = load_all()
    if author:
        items = [i for i in items if _author_match(i, author)]
    if year:
        items = [i for i in items if _year_of(i) == int(year)]
    if source:
        _s = str(source).strip().lower()
        items = [i for i in items if _s in str(i.get("source") or "").lower()]

    def _k(it):
        ev = it.get("eval") or {}
        try:
            avg = float(ev.get("avg") or 0)
        except Exception:                               # noqa: BLE001
            avg = 0.0
        return (avg, float(it.get("ts") or 0))

    items.sort(key=_k, reverse=True)
    return items[:int(top_k)]


# ------------------------------------------------ 核心提炼（2026-11-19）
# 用户原话：「即使不照搬原文也要能精准点出其中的核心」。
# 所以这里不复制摘要，而是给出：一句话核心 + 3 条要点 + 一句「对我们有没有用」。
_VERDICT_ZH = {
    "adopt_now": "现在就值得做",
    "evaluate": "值得做个小实验验证",
    "watch": "先记着，等它成熟",
    "skip": "和我们关系不大",
}
_PT_SPLIT = re.compile(r"[。；;\n]+")


def _split_sents(text: str, n: int = 3) -> list:
    """把中文要点切成句子，丢掉否定句和过短的碎片。"""
    out = []
    for p in _PT_SPLIT.split(str(text or "")):
        p = p.strip(" 　·-—、,，")
        if len(p) < 4 or _is_neg_sentence(p):
            continue
        out.append(p)
        if len(out) >= n:
            break
    return out


def core_of(it: dict) -> dict:
    """提炼一篇论文的核心：{core, points, useful}。

    · core   —— 一句话核心贡献（优先用评估时的一句话结论，其次中文要点首句，
                再退回英文摘要首句）
    · points —— 最多 3 条要点（中文要点切句；没有就退回摘要）
    · useful —— 一句「对我们有没有用」：有评估就给 verdict 中文 + 理由，
                没评估就如实说「还没评估」，不编结论。
    """
    ev = it.get("eval") or {}
    zh = str(it.get("zh_points") or "")
    summ = str(it.get("summary") or "")

    core = str(ev.get("verdict_reason") or "").strip()
    sents = _split_sents(zh, 6)
    if not core and sents:
        core = sents[0]
    if not core and summ:
        core = _PT_SPLIT.split(summ)[0][:160]
    if not core:
        # ⚠️ 2026-11-19 实测：中文媒体（机器之心/量子位）抓来的条目 summary 是空串，
        #    zh_points 也没评估过 → core 会是空字符串，调用方显示成「核心：」。
        #    这里如实标注「只有标题」，而不是编一段内容出来。
        _t = str(it.get("title") or "").strip()
        core = ("（库内只有标题，没有摘要）" + _t) if _t else "（库内这条没有可用信息）"
    core = core[:180]

    points = sents[1:4] if (sents and sents[0] == core) else sents[:3]
    if not points and summ:
        # 英文摘要：按句号切，保留原文（不翻译、不改写）
        points = [x.strip()[:150] for x in re.split(r"(?<=[.!?])\s+", summ)
                  if len(x.strip()) > 20][:3]

    v = str(ev.get("verdict") or "").strip()
    if v:
        useful = "%s（%s）" % (_VERDICT_ZH.get(v, v), ev.get("verdict_reason") or "")
    else:
        useful = "还没做过四维评估，暂时给不出「有没有用」的结论"
    return {"core": core, "points": points[:3], "useful": useful[:200],
            "verdict": v or ""}


def search(query: str, top_k: int = 8, only_unevaluated: bool = False,
           author: str = "", year: int = 0, source: str = "") -> list:
    """按主题召回历史论文（语义 + 关键词混合）。

    2026-11-19 用户要求「按作者或其他条件找指定论文」：新增 author / year /
    source 三个过滤条件。**过滤必须和向量行用同一套下标**（见下面 idx 注释），
    否则会重演 only_unevaluated 那次的广播错误。
    """
    query = str(query or "").strip()
    all_items = load_all()
    if not all_items or not query:
        return []
    sig = embed.signature()
    meta = _read_json(_META, {}) or {}
    mat = None
    if sig == meta.get("sig") and _VEC.is_file():
        try:
            arr = np.load(_VEC)
            if arr.ndim == 2 and arr.shape[0] == len(all_items):
                mat = arr
        except Exception:
            mat = None

    # ⚠️ only_unevaluated 必须**按同一套下标**同时筛 items 和向量行。
    #    2026-11-15 实测踩过：只筛了 items（30→25），mat 还是 30 行，
    #    `mat @ qv[0]` 得到 30 维，和 25 个候选对不上，直接
    #    `ValueError: operands could not be broadcast together with shapes (30,) (25,)`，
    #    而且它是在 reflect 里被 except 吞掉的 —— 表现就是"论文简报一片空白"。
    idx = list(range(len(all_items)))
    if only_unevaluated:
        idx = [i for i in idx if not all_items[i].get("eval")]
    # ★ 2026-11-19：作者/年份/来源过滤。⚠️ 一律作用在 idx 上，
    #   不要单独去筛 items —— mat[idx] 用的就是这份 idx，分开筛必然错位。
    if author:
        idx = [i for i in idx if _author_match(all_items[i], author)]
    if year:
        idx = [i for i in idx if _year_of(all_items[i]) == int(year)]
    if source:
        _s = str(source).strip().lower()
        idx = [i for i in idx
               if _s in str(all_items[i].get("source") or "").lower()]
    if not idx:
        return []
    items = [all_items[i] for i in idx]

    # ★ 2026-11-15 修：中文 query 检索**英文摘要**召回为 0。
    #   实测：库里有 9 篇 arXiv 论文（automated program repair 等，纯英文），
    #   而查询是「自动修复 代码」。bge-small-zh 在纯英文文本上语义分只有 0.30
    #   上下，字面覆盖和 2-gram 更是 0 —— 三个阈值全过不了，一条都召不回。
    #   这里把 query 做一次中文→英文扩展（need2query 的规则表），
    #   多个 query 各算一遍**取最大值**，这样中文提问也能命中英文论文。
    queries = [query]
    try:
        from . import need2query
        queries += [q for q in need2query.to_queries(query, 3) if q not in queries]
    except Exception:
        logger.debug("silent-except", exc_info=True)

    # 文本只算一次：下面 cov / gram 都要用，原实现对每篇重复调 6 次 _doc_text
    texts = [_doc_text(i) for i in items]
    cov = np.asarray([max(_coverage(q, texts[k]) for q in queries)
                      for k in range(len(items))], dtype=np.float32)
    vec = None
    if mat is not None:
        qv = embed.embed_texts(queries, is_query=True)
        if (qv is not None and getattr(qv, "shape", (0,))[0] == len(queries)
                and len(qv[0]) == mat.shape[1]):
            sub = mat[idx] if len(idx) != mat.shape[0] else mat
            vec = (sub @ qv.T).max(axis=1)

    if vec is not None:
        final = 0.75 * vec + 0.25 * cov
    else:
        final = cov.copy()

    # 硬门槛：中文靠 2-gram，英文靠整词（见 _grams / _en_hits 注释）
    qgrams_list = [_grams(q) for q in queries]
    gram = [max([_gram_hits(qg, texts[k]) for qg in qgrams_list]
                + [_en_hits(q, texts[k]) for q in queries])
            for k in range(len(items))]

    picked = []
    for i in range(len(items)):
        v = float(vec[i]) if vec is not None else 0.0
        c = float(cov[i])
        ok = False
        if v >= 0.55:
            ok = True                      # 强语义信号，不要求字面重合
        elif v >= MIN_VEC and gram[i] > 0 and c >= 0.50:
            # 中等语义 + 至少一个 2-gram 命中 + **字面覆盖过半**。
            # ⚠️ 为什么还要 c>=0.50（2026-11-15 实测）：查「量子计算」时，
            #   某篇论文的中文要点里恰好有「计算」这个泛词，bigram 命中 1 个，
            #   但覆盖率只有 0.25 —— 靠一个泛词就召回等于没拦。
            ok = True
        elif c >= 0.75 and gram[i] > 0:
            # 字面覆盖率极高**且**有连续词命中。
            # ⚠️ 只靠覆盖率不行（2026-11-15 实测）：4 字查询「量子计算」在
            #   某篇论文要点上命中「量/计/算」三个**散字**，cov=0.75 就放行了，
            #   但它跟那篇讲渲染加速的论文毫无关系。加 gram>0 就挡住了。
            ok = True
        if not ok:
            continue
        picked.append((float(final[i]), i, v, c))
    picked.sort(key=lambda x: x[0], reverse=True)
    out = []
    for f, i, v, c in picked[:int(top_k)]:
        it = dict(items[i])
        it["score"] = round(f, 4)
        it["vec_score"] = round(v, 4)
        it["kw_score"] = round(c, 4)
        out.append(it)
    return out


# ------------------------------------------------------ 引用核验（相关性打分）
# ⚠️ 为什么需要这一层（2026-11-15 用户要求）：原来判断「这个创新有论文依据吗」
#    只靠 `"《" in theory` 这种字面检查，于是「（无论文直接支撑X，工程可行）
#    可参考《Y》的…」也被算成「有论文支撑」—— 模型自己都说了不是直接依据，
#    我们却当成背书。更糟的是它无法识别**编造的论文标题**。
#    这里做两件事：① 标题是否真在库里（抓编造）② 与功能文本的语义相关度
#    （抓「引了但无关」）。判据全部来自真实数据，不靠关键词猜。
# ⚠️ 只收「明确否认自己是依据」的说法（2026-11-15 实测踩过）：
#   最初把「未给出具体」「不提供本功能」也列进来，结果误伤了一条真实引用 ——
#   「有论文支撑：《A Comprehensive Survey…》（可作为参考框架，但未给出具体
#   摘要算法）」只是在**补充局限性**，却被判成「模型否认依据」。收紧后只认
#   明确的否认句，宁可漏判也不能把真引用打成假的。
_DISCLAIMERS = (
    "无论文", "非本功能直接依据", "非直接依据",
    "非针对本功能", "不针对本功能",
)


def _looks_like_title(s: str) -> bool:
    """判断一段文字像不像论文标题（用来挡掉散文片段被误抽）。

    ⚠️ 为什么需要（2026-11-15）：theory 里「；」后面既有第二篇论文标题，也有
    「两者均需训练模型，工程上以规则模板近似」这种说明。若把后者当标题，它会
    落进 missing（库里查不到），让结果凭空多出一条「疑似编造」的警告。
    实测库里 95 篇有 90 篇英文标题，所以「ASCII 字母 >= 8 个」对英文够干净。
    """
    s = str(s or "").strip()
    if len(s) < 10:
        return False
    return sum(1 for c in s if c.isascii() and c.isalpha()) >= 8


# ★ 2026-11-15 补：原实现有个已知风险 —— 紧跟「论文支撑：」的第 0 段**不做**
#   形态过滤（为了抓中文编造标题），代价是模型若在冒号后直接写散文
#   （「有论文支撑：两者均需训练模型，工程上以规则模板近似」），那段会被当成
#   标题去库里查，查不到就**诬告「疑似编造」**。这不是判错档位，是冤枉模型。
#   解法：第 0 段改用「散文判定」——不像散文就当标题候选，既保住中文编造
#   标题的检出，又不再把说明性文字当引用。
#   判据来自真实数据标定：库里 8 条被引用的真标题**没有一条含中文逗号**，
#   而唯一那条散文「两者均需训练模型，工程上以规则模板近似」含逗号。
#   ⚠️ 顿号「、」**不能**当散文信号：库里真实新闻标题「推理拉满、算力翻5倍」
#   就含顿号，实测会漏检。
_PROSE_HEADS = (
    "两者", "二者", "我们", "该", "这", "其", "因此", "但是", "不过",
    "如果", "由于", "需要", "可以", "建议", "应当", "应该", "实际上",
    "工程上", "总之", "所以", "而且", "并且", "目前", "现在", "同时",
    "鉴于", "除非", "否则", "此项", "上述",
)
_PROSE_WORDS = (
    "需要", "可以", "应当", "应该", "建议", "均可", "均需", "依赖", "近似",
    "相当于", "意味着", "无法", "尚未", "可行", "实现", "处理",
)


def _looks_like_prose(s: str) -> bool:
    """判断一段文字像不像说明性散文（而不是论文标题）。

    三条判据（任一命中即判散文）：
      ① 含中文逗号「，」—— 实测 8 条真标题 0 命中，唯一那条散文命中
      ② 以虚词/代词/连接词开头（两者、我们、该、因此、如果…）
      ③ 中文 >= 6 字且含谓词性词（需要、可以、建议、近似…）
    """
    s = str(s or "").strip()
    if not s:
        return False
    if "，" in s:
        return True
    if any(s.startswith(h) for h in _PROSE_HEADS):
        return True
    cn = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if cn >= 6 and any(w in s for w in _PROSE_WORDS):
        return True
    return False


def _is_title_candidate(s: str, first: bool = False) -> bool:
    """这段文字值不值得当「被引用的论文标题」去核验。

    first=True 表示它紧跟「论文支撑：」，即模型明说了这是标题 —— 此时放宽形态
    要求（只要不像散文就收），否则「完全不存在的论文 XYZ」这种只有 3 个 ASCII
    字母的中文编造标题会被漏掉。
    """
    s = str(s or "").strip()
    if not s or _looks_like_prose(s):
        return False
    if _looks_like_title(s):
        return True
    cn = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if cn >= 6 and len(s) <= 60:
        return True
    return bool(first and len(s) >= 4)


def _extract_cited(theory: str) -> list:
    """从 theory 文本里抽出被引用的论文标题（去重、保序）。

    真实 theory 有 4 种写法（都取自库里的真实记录）：
      ① 有论文支撑：Title（说明）
      ② 工程可行。检索到的《Title》提供…
      ③ 有论文支撑：TitleA（…）；TitleB（…）   ← 多篇用「；」分隔
      ④ （无论文直接支撑X，工程可行）可参考《Y》的…  ← 带免责声明
    只看「有没有《》」会把 ④ 也算成有依据，所以这里必须把标题抽出来逐个核。
    """
    t = str(theory or "")
    out = []
    for m in re.finditer(r"《([^》]{3,220})》", t):
        out.append(m.group(1).strip())
    # ★ 2026-11-15 实测缺口：模型常写「有论文支撑：A（说明）；B（说明）」，
    #   原来只按「；」切出第一段，**第二篇从来没被核验过**（实测一条 theory
    #   引了 Phantom Guardrails + RESTORE，只查了前者）。现在从「论文支撑」
    #   往后整段按 ；;。换行 切开逐个抽。
    idx = t.find("论文支撑")
    if idx >= 0:
        tail = t[idx + len("论文支撑"):]
        pieces = [x for x in re.split(r"[；;。\n]", tail) if x.strip()]
        for n, piece in enumerate(pieces):
            p = re.split(r"[（(]", piece.lstrip("：: ，, "), 1)[0].strip()
            if not p:
                continue
            # ⚠️ 第 0 段紧跟「论文支撑：」，**不能**过 ASCII 形态判定：实测
            #   「有论文支撑：完全不存在的论文 XYZ（编的）」只有 3 个 ASCII
            #   字母，会被过滤掉，于是编造标题反而漏检（判成「没有引用论文」）。
            # ⚠️ 但也不能无条件收（2026-11-15 修的风险）：模型可能写
            #   「有论文支撑：两者均需训练模型，工程上以规则模板近似」，那段
            #   散文会被当标题去库里查、查不到就**诬告「疑似编造」**。
            #   现在统一走 _is_title_candidate：不像散文才当标题候选。
            if _is_title_candidate(p, first=(n == 0)):
                out.append(p)
    # ★ 2026-11-15 实测 bug：同一个标题会被抽**两次** —— 正则《…》抽一次（不带
    #   书名号，因为取的是 group(1)），「论文支撑」分段又抽一次（**带**书名号，
    #   因为 piece 原样保留了《》）。而 _match_cited 不剥书名号，于是带书名号的
    #   那份在库里查不到 → fabricated 误判「疑似编造」，还会把 strong 降成 weak。
    #   真实 47 条 theory 恰好都没写《》，所以线上没暴露；但「有论文支撑：《X》」
    #   是很常见的写法（真实库里有 5 篇中文新闻标题就是这么被引的），必须修。
    #   修法：归一化时剥掉首尾书名号，再统一去重（顺带让编造标题也只报一次）。
    seen, res = set(), []
    for x in out:
        s = re.sub(r"^《\s*", "", str(x).strip())
        s = re.sub(r"\s*》$", "", s).strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            res.append(s)
    return res


def _extract_segments(theory: str) -> list:
    """抽出 theory 里**所有**候选段（不做散文过滤、不做形态判定）。

    专供 check_citation 的「先查库再判形态」补救用：散文判据难免误伤含中文
    逗号的**真实**标题（库里实测有「英伟达上演…，员工集体请愿…！」），而形态
    判定永远不如数据可靠 —— 只要这段在库里查得到，它就是标题。
    """
    t = str(theory or "")
    out = []
    for m in re.finditer(r"《([^》]{3,220})》", t):
        out.append(m.group(1).strip())
    idx = t.find("论文支撑")
    if idx >= 0:
        tail = t[idx + len("论文支撑"):]
        for piece in re.split(r"[；;。\n]", tail):
            p = re.split(r"[（(]", piece.lstrip("：: ，, "), maxsplit=1)[0].strip()
            if p and len(p) >= 4:
                out.append(p)
    # 与 _extract_cited 同样剥书名号：否则「数据优先补救」拿带《》的段去
    # _match_cited_exact 查，永远查不到，等于补救失效。
    seen, res = set(), []
    for x in out:
        s = re.sub(r"^《\s*", "", str(x).strip())
        s = re.sub(r"\s*》$", "", s).strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            res.append(s)
    return res


def _match_cited_exact(cited: str, items: list):
    """严格匹配：只认完全相同或一方是另一方的前缀（长度 >= 12 才允许前缀）。

    为什么不用 _match_cited 的「包含」判定：补救阶段要拿**散文**去库里试，
    包含匹配会让「两者均需训练模型…」这种长句因为撞上标题片段而误命中，
    于是把散文又当成真标题 —— 那就白改了。
    """
    c = str(cited or "").strip().lower()
    if not c:
        return None
    for it in items:
        tl = str(it.get("title") or "").strip().lower()
        if tl and tl == c:
            return it
    if len(c) >= 12:
        for it in items:
            tl = str(it.get("title") or "").strip().lower()
            if tl and (tl.startswith(c) or c.startswith(tl)):
                return it
    return None


def _match_cited(cited: str, items: list):
    """在库里找这篇被引用的论文。精确优先，其次前缀/包含（模型常省略副标题）。"""
    c = str(cited or "").strip().lower()
    if not c:
        return None
    for it in items:
        if str(it.get("title") or "").strip().lower() == c:
            return it
    for it in items:
        tl = str(it.get("title") or "").strip().lower()
        if not tl or len(tl) < 8:
            continue
        if tl.startswith(c[:60]) or c.startswith(tl[:60]) or c[:40] in tl:
            return it
    return None


def check_citation(theory: str, feature_text: str = "",
                   min_strong: float = 0.60, min_weak: float = 0.50) -> dict:
    """核验 theory 引用的论文：是否真实存在、是否与功能相关。

    返回 {titles, found:[{title,score}], missing, score, level, disclaimer, why}
    level 四档：
      · unverified —— 引了但库里查不到（疑似编造），**不算有依据**
      · strong     —— 真实存在且语义相关度 >= min_strong
      · weak       —— 真实存在但相关度中等（min_weak ~ min_strong）
      · none       —— 没引用，或引了但不相关（< min_weak）
    """
    th = str(theory or "")
    titles = _extract_cited(th)
    disclaimer = any(d in th for d in _DISCLAIMERS)
    items = load_all()
    # ★ 2026-11-15 补：散文判据会误伤含中文逗号的**真实**标题（库里实测有
    #   「英伟达上演"奥特曼2.0"，员工集体请愿：让老黄回家！」）。形态判定再
    #   精细也不如数据可靠 —— 被过滤掉的段，只要在库里严格查得到，就加回来。
    #   编造的标题库里查不到，因此不受影响（编造仍会被报 unverified）。
    _extra = [s for s in _extract_segments(th)
              if s not in titles and _match_cited_exact(s, items)]
    if _extra:
        titles = titles + _extra
    if not titles:
        return {"titles": [], "found": [], "missing": [], "score": 0.0,
                "level": "none", "rank": 0, "counts": False,
                "fabricated": False, "disclaimer": disclaimer,
                "why": "没有引用任何论文"}
    found, missing = [], []
    for c in titles[:6]:
        it = _match_cited(c, items)
        (found.append(it) if it else missing.append(c))
    if not found:
        # ★ 2026-11-15：模型写「（无论文直接支撑X，工程可行）可参考《Y》」时，
        #   Y 若不在库里，原来一律返回 unverified「疑似编造」—— 但模型明明已经
        #   声明「不是依据」，把它标成造假是冤枉。免责优先：level 给 disclaimed，
        #   同时 fabricated 仍保留 True（「库里查不到」这个事实不能抹掉）。
        _lv0 = "disclaimed" if disclaimer else "unverified"
        _why0 = ("模型已声明非直接依据；引用的标题在库里查不到：%s" % missing[0][:60]
                 if disclaimer else
                 "引用的论文在库里查不到（疑似编造）：%s" % missing[0][:60])
        return {"titles": titles, "found": [], "missing": missing,
                "score": 0.0, "level": _lv0, "rank": 0, "counts": False,
                "fabricated": True, "disclaimer": disclaimer, "why": _why0}

    # 语义相关度：功能文本 vs 论文正文（标题+英文摘要+中文要点）
    # ★ 阈值必须按真实分布标定，不能凭感觉（2026-11-15 实测 22 条真实引用）：
    #   被引论文得分 0.542~0.720、中位 0.622，排名多为 #1~#5。
    #   我最初拍的 0.62/0.48 会让**全部真实引用都判不成 strong** —— 因为
    #   doc_text 是长文本，和短 query 的余弦天然被稀释。故取 0.60 / 0.50。
    ft = str(feature_text or "").strip()
    score, rank, scope = 0.0, 0, "cited"
    if ft:
        qv = None
        try:
            qv = embed.embed_texts([ft], is_query=True)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        if qv is not None and len(qv) == 1:
            arr = None
            if _VEC.is_file():
                try:
                    _a = np.load(_VEC)
                    if _a.ndim == 2 and _a.shape == (len(items), len(qv[0])):
                        arr = _a
                except Exception:
                    arr = None
            if arr is not None:
                # 复用全库索引：一次 query embedding 就能算出**全库排名**
                s_all = arr @ qv[0]
                scope = "corpus"
                for it in found:
                    try:
                        i = items.index(it)
                    except ValueError:
                        continue
                    s = float(s_all[i])
                    if s > score:
                        score, rank = s, int((s_all > s).sum()) + 1
            else:
                dv = embed.embed_texts([_doc_text(it) for it in found])
                if dv is not None and getattr(dv, "shape", (0,))[0] == len(found):
                    sims = dv @ qv[0]
                    score, rank = float(np.max(sims)), 1
        if score <= 0:      # embedding 不可用 → 退化为字面覆盖
            score = max(_coverage(ft, _doc_text(it)) for it in found)

    rt = ("第%d名" % rank) if scope == "corpus" else ""
    fl = [{"title": str(it.get("title") or "")[:120], "score": round(score, 4)}
          for it in found]
    # ★ 为什么还要看排名（2026-11-15 实测）：同一篇 Phantom Guardrails，用
    #   计划里的原文 feature 算 0.720，换个措辞就掉到 0.587 —— **绝对分受
    #   query 措辞影响很大**，但它始终是全库第 1 名。所以「第 1 名 + 中等分」
    #   也应当算强依据，否则同一篇论文会因为措辞不同忽高忽低。
    top1 = (scope == "corpus" and rank == 1)
    rank_ok = (scope != "corpus") or rank <= 8
    if not ft:
        lvl, why = "weak", "未提供功能文本，仅核对了引用真实性"
    elif disclaimer:
        # ★ 关键一档（2026-11-15）：模型自己写了「无论文直接支撑X…可参考《Y》
        #   非本功能直接依据」，那就是**它自己承认不是依据**。库里实测有 3 条
        #   这种写法，旧的关键词判定全算成「有依据」—— 这正是用户说的假阳性。
        #   它们确实相关（所以不算 none），但不能计入 has_paper。
        lvl = "disclaimed"
        why = "论文真实，但模型自己声明非直接依据，不算依据（相关度 %.2f %s）" % (score, rt)
    elif (score >= min_strong and rank_ok) or (top1 and score >= min_weak):
        lvl = "strong"
        why = "论文真实且高度相关（%.2f %s）" % (score, rt)
    elif score >= min_weak or (top1 and score >= 0.45):
        lvl = "weak"
        why = "论文真实、相关度中等（%.2f %s）" % (score, rt)
    else:
        lvl = "none"
        why = "相关度不足，不构成依据（%.2f %s）" % (score, rt)
    if missing:
        # ⚠️ 实测有「引 1 篇真的 + 1 篇编的」的写法：真实那篇照样相关，但编造
        #   标题说明这次引用不可全信，所以把警示放最前面，别让它埋在一句话末尾。
        #   同时**不允许判 strong** —— 一半是编的还叫"强依据"，等于自欺。
        if lvl == "strong":
            lvl = "weak"
        why = "⚠️ 有 %d 个标题在库里查不到（疑似编造）；%s" % (len(missing), why)
    # counts 才是「能不能算有依据」的唯一口径：只有 strong / weak 算。
    # 单独给一个布尔字段，避免调用方各自拿 level 去猜（之前 has_paper 就是这么错的）。
    return {"titles": titles, "found": fl, "missing": missing,
            "score": round(score, 4), "level": lvl, "rank": rank,
            "counts": lvl in ("strong", "weak"),
            "fabricated": bool(missing),
            "disclaimer": disclaimer, "why": why}


def stats() -> dict:
    items = load_all()
    ev = [i for i in items if i.get("eval")]
    adopt = [i for i in ev if (i["eval"] or {}).get("verdict") in ("adopt_now", "evaluate")]
    return {"total": len(items), "evaluated": len(ev), "adopted": len(adopt),
            "vec_ready": embed.available(), "embed": embed.info()}


# ------------------------------------------------------------------ 评估
_EVAL_PROMPT = """你是这个本地多模态助手项目的**技术选型负责人**。下面给你一批最新的
技术论文/开源项目，以及本项目的现状。请对**每一条**做四维评估，并给出落地计划。

%s

## 项目当前的真实痛点（来自用户体验评估与自我反思）
%s

## 待评估的技术动态
%s

## 评估要求（很重要，逐条照做）
1. **可行性 feasibility**：在本项目（Python FastAPI + 原生 JS、单机、有 GPU 但显存 12GB）
   落地的难度。要具体到"依赖什么、要不要新模型、显存/CPU 够不够"。
   纯云端方案要说明"要花钱/要 key"。
2. **创新性 novelty**：它是真新东西，还是已有做法的换皮？对**我们这个场景**新在哪。
3. **效益 benefit**：对**用户**（一个要结果、要效率、讨厌绕路的人）具体有什么用。
   说不出具体好处就老实给低分。
4. **效率提升 efficiency**：能不能减少等待、减少 token、减少返工、减少出错。
   给不出量化就写"无法量化"。
5. 四个分数都用 1~10（10 最好），**不要都给 7 分**，该低就低。
6. **verdict**：
   · adopt_now = 现在就值得做，收益明确、风险可控
   · evaluate = 值得做个小实验验证，但别全量上
   · watch = 先记着，等它成熟或等我们有余量
   · skip = 与本项目无关 / 成本远超收益（**大部分应该是这个**，别硬凑）
7. **plan**：verdict 是 adopt_now / evaluate 时给分步骤计划（2~4 步），
   每步写清：改什么、涉及哪些文件（只能是 backend/*.py 或 frontend/*.js|css|html）、
   风险 low/mid/high、工作量 S/M/L、**怎么验证这步成功了**。
   verdict 是 watch/skip 时 plan 给空数组。
8. ⚠️ 只允许改 backend/ 和 frontend/ 下的 .py/.js/.css/.html。
9. ⚠️ 不要编造论文里没有的能力；摘要信息不足时，feasibility 要写"需先读全文确认"。
10. ⚠️⚠️ **上面列了几条，papers 数组就必须有几条，一条都不能省**。
    哪怕你觉得某条完全不相关，也要给它一条 verdict=skip 的记录（这正是结论的一部分）。
    实测：不强调这句时，送 8 条只回 3 条，其余的被静默跳过。

## 输出格式（严格 JSON，不要额外文字）
{
  "papers": [
    {
      "title": "与输入完全一致的标题",
      "title_zh": "这个标题的中文翻译（20字内）",
      "zh_points": "用中文写 3~5 个要点，60~150 字：它做了什么、用了什么方法、对什么场景有效。这是给中文检索用的索引文本，**必须写得像中文提问会用的词**（例如『显存占用』『口型同步』『文档排版』）。",
      "feasibility": {"score": 1, "why": "..."},
      "novelty": {"score": 1, "why": "..."},
      "benefit": {"score": 1, "why": "..."},
      "efficiency": {"score": 1, "why": "..."},
      "verdict": "adopt_now|evaluate|watch|skip",
      "verdict_reason": "一句话结论",
      "plan": [
        {"step": "做什么", "files": ["backend/xxx.py"], "risk": "low|mid|high",
         "effort": "S|M|L", "verify": "怎么验证这步成了"}
      ],
      "risk": "落地风险/前置条件"
    }
  ]
}"""


def _llm(prompt, max_tokens=5000, temperature=0.35):
    try:
        from . import keyvault
        from .deepseek_client import chat_stream, DeepSeekError
    except Exception as e:                              # noqa: BLE001
        return None, "导入 LLM 客户端失败：%s" % e
    try:
        key = keyvault.get_key()
    except Exception:                                   # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key（先在顶栏 🔑 填一个）"
    try:
        buf = []
        for kind, txt in chat_stream([{"role": "user", "content": prompt}],
                                     "deepseek-flash", key, temperature=temperature,
                                     max_tokens=max_tokens, thinking=False,
                                     # 取 JSON 时不能把「（输出达到 max_tokens 上限被截断）」
                                     # 混进正文，那行中文会插在 JSON 中间（2026-11-16）
                                     truncation_notice=False):
            if kind == "content" and txt:
                buf.append(txt)
        return _jsonfix.strip_notice("".join(buf)), None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    """从 LLM 输出里抠出 JSON（含截断修复）。实现见 backend/jsonfix.py。"""
    return _jsonfix.parse(txt)


def evaluate(items, project_brief: str = "", pains: str = "", max_n: int = 8) -> dict:
    """对一批技术动态做四维评估 + 落地计划，结果写回库里。

    ⚠️ **分批**（每批 4 条）而不是一次全送：2026-11-15 实测送 8 条时，
    模型只回了 3 条，其余 5 条被静默跳过（prompt 里加了"必须逐条"也不稳）。
    分批后每批少，漏评率明显下降；而且某批失败不影响其它批。

    返回 `{ok, evaluated, papers, error, missed}`。
    """
    items = [i for i in (items or []) if isinstance(i, dict) and i.get("title")][:max_n]
    if not items:
        return {"ok": True, "evaluated": 0, "papers": []}
    BATCH = 4
    all_saved, errs, missed = [], [], []
    for s in range(0, len(items), BATCH):
        part = items[s:s + BATCH]
        r = _evaluate_batch(part, project_brief, pains)
        if not r.get("ok"):
            errs.append(r.get("error") or "未知错误")
            continue
        all_saved.extend(r.get("papers") or [])
        got = {re.sub(r"\s+", "", str(p.get("title") or ""))[:120] for p in (r.get("papers") or [])}
        for it in part:
            k = re.sub(r"\s+", "", str(it.get("title") or ""))[:120]
            if k not in got:
                missed.append(it.get("title"))
    return {"ok": bool(all_saved) or not errs,
            "evaluated": len(all_saved), "papers": all_saved,
            "missed": missed[:10],
            "error": ("；".join(errs[:2]) if errs and not all_saved else "")}


def _evaluate_batch(items, project_brief: str = "", pains: str = "") -> dict:
    """评估一小批（内部用）。"""
    items = [i for i in (items or []) if isinstance(i, dict) and i.get("title")]
    if not items:
        return {"ok": True, "evaluated": 0, "papers": []}
    lines = []
    for i, it in enumerate(items, 1):
        lines.append("%d) [%s] %s\n   链接：%s\n   摘要：%s" % (
            i, it.get("source") or "?", it.get("title"),
            it.get("url") or "（无）",
            (it.get("summary") or "（无摘要，只有标题）")[:600]))
    prompt = _EVAL_PROMPT % (project_brief or "（未提供）",
                             pains or "（暂无）",
                             "\n".join(lines))
    txt, err = _llm(prompt, max_tokens=5000, temperature=0.35)
    if err:
        return {"ok": False, "error": err, "evaluated": 0, "papers": []}
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型返回的不是合法 JSON",
                "raw": (txt or "")[:500], "evaluated": 0, "papers": []}

    got = {}
    for p in (data.get("papers") or []):
        if isinstance(p, dict) and p.get("title"):
            got[re.sub(r"\s+", "", str(p["title"]))[:120]] = p

    now = time.time()
    saved = []
    with _lock:
        all_items = load_all()
        pos = {}
        for k2, it in enumerate(all_items):
            pos[re.sub(r"\s+", "", str(it.get("title") or ""))[:120]] = k2
        papers_list = [p for p in (data.get("papers") or []) if isinstance(p, dict)]
        for k_in, src in enumerate(items):
            k = re.sub(r"\s+", "", str(src.get("title") or ""))[:120]
            p = got.get(k)
            if not p and k_in < len(papers_list):
                # ⚠️ 兜底：模型有时把 `title` 也写成中文（我要求它给 title_zh，
                #    它就顺手把 title 一起翻译了），按标题一个都匹配不上 ——
                #    实测 8 条全丢、一篇都没入库。这时按**位置**对应
                #    （输入顺序 = 输出顺序，prompt 里是编号列表）。
                p = papers_list[k_in]
            if not p:
                continue
            ti = pos.get(k, -1)
            if ti < 0:
                continue
            it = all_items[ti]
            ev = {
                "feasibility": _dim(p.get("feasibility")),
                "novelty": _dim(p.get("novelty")),
                "benefit": _dim(p.get("benefit")),
                "efficiency": _dim(p.get("efficiency")),
                "verdict": str(p.get("verdict") or "watch").lower(),
                "verdict_reason": str(p.get("verdict_reason") or "")[:300],
                "plan": [x for x in (p.get("plan") or []) if isinstance(x, dict)][:4],
                "risk": str(p.get("risk") or "")[:400],
            }
            it["eval"] = ev
            it["eval_ts"] = now
            if p.get("title_zh"):
                it["zh_title"] = str(p["title_zh"])[:200]
            if p.get("zh_points"):
                it["zh_points"] = str(p["zh_points"])[:800]
            saved.append({"title": it.get("title"), "title_zh": it.get("zh_title"),
                          "url": it.get("url"), "source": it.get("source"), "eval": ev})
        _rewrite(all_items)
    if saved:
        # 补了中文就必须重建索引，否则中文查询仍然召回不到（见 _doc_text 的说明）
        try:
            build_index(force=True)
        except Exception:
            logger.debug("silent-except", exc_info=True)
    return {"ok": True, "evaluated": len(saved), "papers": saved}


def _dim(d) -> dict:
    """把 LLM 给的某一维归一成 {score, why}（模型有时给数字、有时给对象）。"""
    if isinstance(d, dict):
        try:
            s = int(float(d.get("score") or 0))
        except Exception:
            s = 0
        return {"score": max(0, min(10, s)),
                "why": str(d.get("why") or d.get("detail") or "")[:400]}
    try:
        s = int(float(d or 0))
    except Exception:
        s = 0
    return {"score": max(0, min(10, s)), "why": ""}


def high_value(top_n: int = 6) -> list:
    """按四维均分排出最值得做的（只取 adopt_now / evaluate）。"""
    out = []
    for it in load_all():
        ev = it.get("eval") or {}
        if ev.get("verdict") not in ("adopt_now", "evaluate"):
            continue
        sc = []
        for k in ("feasibility", "novelty", "benefit", "efficiency"):
            try:
                sc.append(float((ev.get(k) or {}).get("score") or 0))
            except Exception:
                sc.append(0.0)
        # ⚠️ 必须带上 zh_title / zh_points / summary（2026-11-19）：
        #    原来只回 title/url/source，工具层去取中文标题和要点全是空，
        #    卡片上显示英文标题 +「要点：（无）」。
        out.append({"title": it.get("title"), "zh_title": it.get("zh_title"),
                    "url": it.get("url"), "source": it.get("source"),
                    "zh_points": it.get("zh_points"), "summary": it.get("summary"),
                    "eval": ev, "avg": round(sum(sc) / 4.0, 2)})
    out.sort(key=lambda x: x["avg"], reverse=True)
    return out[:top_n]


def brief_for_prompt(top_n: int = 5) -> str:
    """给反思 prompt 用的一段「论文评估摘要」（省 token，只给结论和计划）。"""
    hv = high_value(top_n)
    if not hv:
        st = stats()
        n = int(st.get("evaluated") or 0)
        if n:
            return ("（已四维评估 %d 篇，但**没有一篇**达到 adopt_now / evaluate —— "
                    "结论就是：最近这批新技术对我们当前的痛点没有明确帮助。"
                    "这不是漏评，是真实判断，本轮不要硬把它们包装成 action。）" % n)
        return "（还没有评估过的论文）"
    lines = []
    for it in hv:
        ev = it["eval"]
        lines.append(
            "- 《%s》[%s] 均分 %.1f｜可行性 %s / 创新 %s / 效益 %s / 效率 %s → %s\n"
            "  结论：%s\n  计划：%s"
            % (it["title"], it["source"], it["avg"],
               (ev.get("feasibility") or {}).get("score"),
               (ev.get("novelty") or {}).get("score"),
               (ev.get("benefit") or {}).get("score"),
               (ev.get("efficiency") or {}).get("score"),
               ev.get("verdict"), ev.get("verdict_reason") or "",
               "；".join("%s（%s, %s）" % (s.get("step"), s.get("risk"), s.get("effort"))
                        for s in (ev.get("plan") or []) if isinstance(s, dict)) or "无"))
    return "\n".join(lines)
