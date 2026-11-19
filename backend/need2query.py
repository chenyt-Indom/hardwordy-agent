# -*- coding: utf-8 -*-
"""需求 → 检索词 → arXiv 定向抓取。

为什么要这个模块（2026-11-15）
------------------------------
用户要求「自主创新要能参考论文构思」，但实测下来论文链路是**空的**：

· 论文库只有 32 篇，其中 16 篇正文不足 20 字；
· 内容全是「SpaceX 星舰」「折纸飞机」「英伟达员工请愿」这类**新闻**，
  跟「自迭代 / 代码修复 / 回复截断」毫无关系；
· 于是 `papervec.search(痛点)` 对具体痛点**经常返回 0 条**，
  模型写新功能时 `theory` 只能填「无论文，工程可行」。

根因不是召回算法坏了，而是**入库的源不对口**：`fetch_tech_all` 抓的是
「今天技术圈发生了什么」，不是「我这个痛点有没有人研究过」。

所以这里补一条**按需定向**的路：拿到痛点 → 转成英文检索词 →
直接查 arXiv 官方 API → 入库。这样召回的每一篇都和当前痛点相关。

设计取舍
--------
· **纯规则映射，不调 LLM**：反思流程本身已经很慢（一次 20~50 秒），
  这里再加一次模型调用不划算；规则表覆盖常见软件工程概念就够用。
· **失败一律返回空，绝不抛异常**：arXiv 连不上不能把整个反思流程弄挂
  （和 `papervec` 的降级思路一致）。
· **不写死单条 query**：一条痛点会产出多个检索词，逐条抓、去重入库。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# 中文痛点关键词 → 英文检索词。命中任一中文词就带上对应英文词。
# 顺序无所谓，但**长词要放在短词前面**（如「自动修复」先于「修复」），
# 否则短词会先把长词的语义吃掉，产出过宽的 query。
_ZH2EN = (
    (("自动修复", "代码修复", "改代码", "程序修复"), ("automated program repair",)),
    (("自迭代", "自我迭代", "自我改进", "自我进化", "自主改进"), ("self-improving agent", "self-refinement")),
    (("截断", "长度上限", "太长", "超长"), ("long text generation", "output length control")),
    (("回复", "回答", "输出形态"), ("response generation",)),
    (("回归", "测试用例", "单测"), ("regression testing", "test generation")),
    (("论文", "文献", "情报"), ("literature retrieval", "scientific literature")),
    (("检索", "召回", "向量"), ("retrieval augmented generation", "dense retrieval")),
    (("记忆", "长期记忆"), ("long-term memory", "agent memory")),
    (("工具调用", "函数调用", "调用工具"), ("tool use", "function calling")),
    (("多模态",), ("multimodal large language model",)),
    (("数字人", "口型", "唇形", "嘴型"), ("lip sync", "talking head generation")),
    (("视频",), ("video generation",)),
    (("语音", "配音", "tts"), ("text to speech",)),
    (("规划", "任务分解", "计划"), ("task planning", "agent planning")),
    (("反思", "复盘"), ("self-reflection", "self-critique")),
    (("幻觉", "编造"), ("hallucination",)),
    (("提示词", "prompt"), ("prompt engineering",)),
    (("效率", "加速", "延迟", "太慢"), ("inference latency", "inference efficiency")),
    (("成本", "token", "省"), ("token efficiency",)),
    (("安全", "权限", "越权"), ("safety", "access control")),
    (("验证", "护栏", "防回滚"), ("verification", "guardrail")),
    (("断点", "续跑", "落盘", "中间状态", "checkpoint"), ("checkpointing", "fault tolerant execution")),
    (("物理", "穿模", "碰撞", "重力"), ("physics simulation", "physically plausible")),
    (("一致性", "时序", "连贯"), ("temporal consistency",)),
    (("进度", "长任务", "耗时"), ("long running task", "task scheduling")),
    (("架构", "重构"), ("software architecture", "refactoring")),
    # ★ 2026-11-19 用户要求「检索质量与准确度」：实测「大模型」「显存优化」
    #   原来一条英文词都产不出来 → 查 arXiv/GitHub 必然 0 条（那两个站全英文）。
    #   这里补一批最常用的技术词；放在表尾，让上面更具体的长词先匹配。
    (("大语言模型", "大模型", "llm"), ("large language model",)),
    (("显存", "vram", "gpu内存"), ("GPU memory", "memory efficient inference")),
    (("量化",), ("quantization",)),
    (("微调", "finetune"), ("fine-tuning",)),
    (("推理",), ("inference",)),
    (("知识库",), ("knowledge base",)),
    (("文档", "排版", "导出"), ("document generation",)),
    (("图像生成", "画图"), ("image generation", "diffusion model")),
    (("语音识别",), ("speech recognition",)),
    (("部署", "上线"), ("deployment", "model serving")),
    (("代码",), ("code generation",)),
    (("搜索", "检索增强"), ("retrieval augmented generation",)),
)

# 英文检索词最短长度：太短的（如 a / of）没意义
_MIN_EN = 4

_ARXIV_API = "https://export.arxiv.org/api/query"


def build_queries(need, gaps=None):
    """把「需求 + 能力缺口」转成 3~5 组检索词（每组是非空元组）。

    与 `to_queries` 的分工
    ----------------------
    · `to_queries` 返回**扁平英文词**，用于向量检索的 query 扩展（一次一个串）；
    · `build_queries` 返回**分组词元组**，用于 arXiv 的组合检索
      （`all:"a" AND all:"b"`），并且**保留中文原话**，便于人工核对
      「模型到底按什么去搜的」。

    约束（断言会验）：
      · 纯函数、确定性 —— 同样输入永远同样输出；
      · 至少 3 组、最多 5 组，每组非空；
      · 中文原词永远在第 1 组，保证检索意图可读。
    """
    text = str(need or "")
    gap_txt = " ".join(str(g) for g in (gaps or []))
    full = (text + " " + gap_txt).strip()
    if not full:
        return []

    out = []

    # 组 1：中文关键词（保留原话）
    zh = []
    for w in re.findall(r"[\u4e00-\u9fa5]{2,8}", full):
        if w not in zh:
            zh.append(w)
    if zh:
        out.append(tuple(zh[:6]))

    # 组 2..n：规则表命中的英文检索词，一组对应一条规则
    for zh_words, ens in _ZH2EN:
        if any(w in full for w in zh_words):
            grp = tuple(e for e in ens if e)
            if grp and grp not in out:
                out.append(grp)

    # 兜底：文本里本来就有的英文术语
    en = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", full):
        if w.lower() not in [x.lower() for x in en]:
            en.append(w)
    if en:
        grp = tuple(en[:4])
        if grp not in out:
            out.append(grp)

    # 不足 3 组时，把中文词拆成单组补齐（保证返回形状稳定）
    if len(out) < 3:
        for w in zh:
            if len(out) >= 3:
                break
            grp = (w,)
            if grp not in out:
                out.append(grp)

    return out[:5]
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def to_queries(need, max_n=4):
    """把一段中文痛点转成英文检索词列表。纯规则，不联网、不调模型。

    返回去重后的列表；一个词都提不出来时返回空列表（调用方据此跳过抓取）。
    """
    text = str(need or "")
    if not text.strip():
        return []
    low = text.lower()
    out, seen = [], set()

    for zh_words, ens in _ZH2EN:
        if any(w in text for w in zh_words):
            for en in ens:
                if en not in seen:
                    seen.add(en)
                    out.append(en)

    # 文本里本来就有的英文技术词也收进来（用户/模型常直接写英文术语）
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text):
        wl = w.lower()
        if wl in seen or len(wl) < _MIN_EN:
            continue
        seen.add(wl)
        out.append(wl)

    return out[:max_n] if max_n > 0 else out


def fetch_arxiv(query, max_n=4, timeout=20):
    """按关键词查 arXiv 官方 API，返回入库格式的列表。

    ⚠️ 任何异常都返回空列表 —— 联网失败不能让反思流程挂掉。
    """
    q = str(query or "").strip()
    if not q:
        return []
    try:
        params = urllib.parse.urlencode({
            "search_query": 'all:"%s"' % q,
            "start": 0,
            "max_results": max(1, min(int(max_n), 20)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        req = urllib.request.Request(
            _ARXIV_API + "?" + params,
            headers={"User-Agent": "local-multimodal/1.0 (need2query)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as e:                                # noqa: BLE001
        logger.info("[need2query] arXiv 抓取失败 query=%r: %s", q, e)
        return []

    items = []
    for e in root.findall("a:entry", _ATOM):
        try:
            title = (e.findtext("a:title", default="", namespaces=_ATOM) or "").strip()
            summary = (e.findtext("a:summary", default="", namespaces=_ATOM) or "").strip()
            link = (e.findtext("a:id", default="", namespaces=_ATOM) or "").strip()
            if not title:
                continue
            items.append({
                "title": re.sub(r"\s+", " ", title),
                "url": link,
                "summary": re.sub(r"\s+", " ", summary)[:1200],
                "source": "arXiv",
                "query": q,
            })
        except Exception:                                 # noqa: BLE001
            continue
    return items


def harvest(pains, max_queries=4, per_query=4, timeout=20):
    """痛点 → 检索词 → 抓 arXiv → 入库。返回统计，永不抛异常。

    返回：{"queries": [...], "fetched": n, "added": n, "dup": n, "total": n, "error": ""}
    """
    out = {"queries": [], "fetched": 0, "added": 0, "dup": 0, "total": 0, "error": ""}
    try:
        qs = to_queries(pains, max_queries)
        # ★ 2026-11-15 接线 build_queries：to_queries 会在 max_queries 处截断，
        #   而 build_queries 是**按规则分组**的，能覆盖被截掉的那些组。
        #   这里把分组里的英文词补进候选（中文词对 arXiv 检索没用，过滤掉）。
        try:
            for grp in build_queries(pains, []):
                for w in grp:
                    if isinstance(w, str) and w and w.isascii() and w not in qs:
                        qs.append(w)
        except Exception:                                 # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
        qs = qs[:max(1, max_queries) * 2]
        out["queries"] = qs
        if not qs:
            out["error"] = "痛点里提不出可用检索词"
            return out

        items = []
        for q in qs:
            items.extend(fetch_arxiv(q, per_query, timeout=timeout))
        out["fetched"] = len(items)
        if not items:
            out["error"] = "arXiv 没返回结果（可能断网或关键词过窄）"
            return out

        from . import papervec
        res = papervec.add_batch(items, "arXiv定向")
        out["added"] = int(res.get("added") or 0)
        out["dup"] = int(res.get("dup") or 0)
        out["total"] = int(res.get("total") or 0)
        return out
    except Exception as e:                                # noqa: BLE001
        logger.warning("[need2query] harvest 失败：%s: %s", type(e).__name__, e)
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
