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
    """去重键：优先 url，其次归一化标题。"""
    u = str(it.get("url") or "").strip().lower()
    if u:
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
def _doc_text(it: dict) -> str:
    """索引用的文本 = 原标题 + 中文标题 + 原摘要 + 中文要点。

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
        parts.append(str(it["zh_points"]))
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


def _coverage(qchars, text):
    if not qchars:
        return 0.0
    qs = [c for c in qchars if c not in _STOP] or list(qchars)
    ts = set(_chars(text))
    if not ts:
        return 0.0
    return sum(1 for c in qs if c in ts) / float(len(qs))


def _grams(s, n=2):
    t = "".join(_chars(s))
    return [t[i:i + n] for i in range(len(t) - n + 1)] if len(t) >= n else []


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


def search(query: str, top_k: int = 8, only_unevaluated: bool = False) -> list:
    """按主题召回历史论文（语义 + 关键词混合）。"""
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
    if not idx:
        return []
    items = [all_items[i] for i in idx]

    qchars = _chars(query)
    cov = np.asarray([_coverage(qchars, _doc_text(i)) for i in items], dtype=np.float32)
    vec = None
    if mat is not None:
        qv = embed.embed_texts([query], is_query=True)
        if qv is not None and getattr(qv, "shape", (0,))[0] == 1 and len(qv[0]) == mat.shape[1]:
            sub = mat[idx] if len(idx) != mat.shape[0] else mat
            vec = sub @ qv[0]

    if vec is not None:
        final = 0.75 * vec + 0.25 * cov
    else:
        final = cov.copy()

    qgrams = _grams(query)
    gram = [_gram_hits(qgrams, _doc_text(i)) for i in items]

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
                                     max_tokens=max_tokens, thinking=False):
            if kind == "content" and txt:
                buf.append(txt)
        return "".join(buf).strip(), None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    if not txt:
        return None
    s = txt.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return None
    return None


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
        out.append({"title": it.get("title"), "url": it.get("url"),
                    "source": it.get("source"), "eval": ev,
                    "avg": round(sum(sc) / 4.0, 2)})
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
