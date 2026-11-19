"""推荐新功能 —— 专门构思「能提高体验的全新功能」。

用户 2026-11-16 的要求（原话整理）：
  · 必须是**新功能**，优化不算；
  · 模型要先想「用户可能需要什么」→「什么样的功能更能提高效率」→ 再构思功能本身；
  · 不一定要照市面上来，要有创新性；**只要理论可行，哪怕完全没有这个概念也可以大胆想**；
  · 参考论文可以、不参考也行 —— 论文只是提供思路；
  · 有计划了就点亮按钮提示，点开能看详情、选择批准/不批准、也能写备注微调；
  · **每次任务中都要时刻思考**（不是定时，是跟着任务走）。

和 self_iterate 的分工：
  · self_iterate = 「哪里坏了 / 怎么改」+「顺手加的小功能」
  · featradar    = 「这个产品还能变成什么样」—— 只产出**用户可感知的新能力**

为什么单独一条通道（而不是塞进反思的 prompt）：
  2026-11-16 实测过三次 —— 只要把「痛点/失败/摩擦」和「想新功能」放在同一个
  prompt 里，模型必然优先修补，产出的"新功能"全是兜底、过滤、计数这类用户
  感知不到的东西。单独一条不带痛点数据的通道，才想得出真正的新交互。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# 每次最多产出几条（多了用户看不过来，也容易稀释质量）
_MAX_PER_ROUND = 3
# 待批准的超过这个数就不再自动生成 —— 否则堆一堆没人看，白烧 token
_BACKLOG_CAP = 12


# ------------------------------------------------------------------ 存取

def _path() -> Path:
    """数据文件路径。

    ⚠️ config.data() 返回的是 **str**（不是 Path）—— 2026-11-16 在 selfloop
    里踩过一次：对它调 .exists() / .read_text() 会抛 AttributeError，而异常
    被静默吞掉，导致状态从来没存下来过。这里统一先 Path() 包一层。
    """
    from . import config
    return Path(config.data("data", "feat_radar.json"))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _blank() -> dict:
    return {"items": [], "ts": 0, "last_think": "", "seen_titles": []}


def load() -> dict:
    p = _path()
    try:
        if not p.is_file():
            return _blank()
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return _blank()
        base = _blank()
        base.update(d)
        if not isinstance(base.get("items"), list):
            base["items"] = []
        if not isinstance(base.get("seen_titles"), list):
            base["seen_titles"] = []
        return base
    except Exception:                                   # noqa: BLE001
        logger.debug("featradar load failed", exc_info=True)
        return _blank()


def save(d: dict) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:                                   # noqa: BLE001
        logger.warning("featradar save failed", exc_info=True)


# ------------------------------------------------------------------ 去重

def _norm(s: str) -> str:
    """归一化标题，用来粗去重（去掉标点空格，只留汉字字母数字）。"""
    out = []
    for ch in str(s or ""):
        if ch.isalnum():
            out.append(ch.lower())
    return "".join(out)


def _too_similar(a: str, b: str) -> bool:
    """两条标题是否像到该判重复。

    用「短的那个有多少字出现在长的里面」来判断 —— 中文标题短，编辑距离
    反而容易误判，包含率更稳。
    """
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) < 4:
        return short in long_
    # 短标题里超过 80% 的字在长标题里出现过 → 判重
    hit = sum(1 for ch in set(short) if ch in long_)
    return hit / max(1, len(set(short))) >= 0.8


def _already_known(d: dict, title: str) -> bool:
    for it in d.get("items") or []:
        if _too_similar(title, it.get("title") or ""):
            return True
    for t in d.get("seen_titles") or []:
        if _too_similar(title, t):
            return True
    return False


# 向量粗筛阈值 —— 2026-11-20 用 bge-small-zh 在真实数据上标定的，不是拍的：
#   · 库里 14 条**互不相同**的功能，两两相似度中位 0.663、最高 0.799；
#   · 我手写的「换个说法、其实是同一个功能」的候选，命中真身落在 0.73~0.87。
#   两者**有重叠**（0.80 vs 0.73），所以单一阈值必然要么误杀要么漏放：
#     ≥0.86 → 可以断定是同一件事，直接拦；
#     ≥0.68 → 灰区，交给模型判「做的是不是同一件事」。
#   ⚠️ 实测过 n-gram Jaccard：同义改写只有 0.035，完全不可用，别退回去。
_SIM_HARD = 0.86
_SIM_GRAY = 0.68


def _llm_judge_dups(pairs: list) -> dict:
    """让模型判断「候选功能 vs 已有功能」是不是同一件事。返回 {序号: True/False}。

    模型判不了就退回保守阈值（≥0.84 才判重）—— 宁可漏放一个重复，
    也不能把真正的新功能误杀（用户要的是「别重复加」，不是「别加」）。
    """
    lines = []
    for k, (cand, old_title, old_text, _sim) in enumerate(pairs):
        lines.append(
            "【第 %d 组】\n候选功能：%s\n候选做法：%s\n已有功能：%s\n已有做法：%s"
            % (k + 1, cand.get("title") or "",
               str(cand.get("idea") or "")[:220],
               old_title, str(old_text or "")[:220]))
    prompt = (
        "下面是几组「候选新功能」和「已有功能」。判断每组两者**是不是同一个功能**："
        "做的是同一件事、只是换了说法或换了入口，就算同一个；"
        "只要有实质不同的能力或交互方式，就算不同。\n\n"
        + "\n\n".join(lines)
        + "\n\n只输出 JSON：{\"same\": [判为同一个功能的组号，从 1 开始]}\n"
          "都不相同就输出 {\"same\": []}"
    )
    try:
        from . import self_iterate as _si
        raw, err = _si._llm_text(prompt, max_tokens=800, temperature=0.1,
                                 want_json=True)
        if err or not raw:
            raise RuntimeError(str(err or "模型返回空"))
        from . import jsonfix
        j = jsonfix.parse(raw)
        same = set(int(x) for x in ((j or {}).get("same") or []))
        return {k: ((k + 1) in same) for k in range(len(pairs))}
    except Exception as e:                              # noqa: BLE001
        logger.warning("模型判重没跑成，退回保守阈值：%s: %s", type(e).__name__, e)
        return {k: (float(pairs[k][3]) >= 0.84) for k in range(len(pairs))}


def _cos_dups(d: dict, cands: list, extra_olds=None) -> list:
    """语义查重：候选功能 vs 库里已有功能，返回 [(候选下标, 原因)]。

    ★ 用户 2026-11-20 要求「推荐功能加入前查一下是否已经有这个功能，
      防止重复加入」。只比标题不够 —— 「一键打包」和「成品打包」是同一个
      功能，字面却完全不同。这里用本机中文向量模型算相似度，灰区再让模型判。

    ★ 2026-11-20 补（用户：「推荐功能时也需要复查一下推荐的功能是否已经加了」）：
      extra_olds 传**迭代历史里已经做完的功能名**。实测真实数据：迭代历史
      5 个功能（一句话多步任务 / 会等你回来的工作台 / 回复形态闸门 …）
      **一个都不在 featradar 的 items 里** —— 语义查重原来完全看不到它们，
      只能靠标题级兜，换个说法就又推荐一遍。现在把它们一起喂进向量比对。
    """
    olds = []
    for it in (d.get("items") or []):
        txt = ("%s %s" % (it.get("idea") or "", it.get("how") or "")).strip()
        if len(txt) >= 12:
            olds.append((str(it.get("title") or ""), txt))
    for t in (extra_olds or []):
        tt = str(t or "").strip()
        if len(tt) >= 4 and not any(tt == o[0] for o in olds):
            # 迭代历史里只有功能名、没有做法正文 —— 拿名字本身当文本比
            olds.append((tt, tt))
    if not olds or not cands:
        return []
    try:
        from . import embed as _emb
        import numpy as _np
        texts = [("%s %s" % (c.get("idea") or "", c.get("how") or "")).strip()
                 for c in cands] + [t for _, t in olds]
        mat = _emb.embed_texts(texts)
        if mat is None:
            return []
        norms = _np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        unit = mat / norms[:, None]
        n = len(cands)
        sim = unit[:n] @ unit[n:].T                     # (候选数, 已有数)
    except Exception:                                   # noqa: BLE001
        logger.debug("语义查重没跑成（跳过这一步）", exc_info=True)
        return []

    hard, gray = [], []
    for i in range(len(cands)):
        row = sim[i]
        j = int(row.argmax())
        best = float(row[j])
        if best >= _SIM_HARD:
            hard.append((i, "和已有的「%s」几乎是同一个功能（相似度 %.2f）"
                            % (olds[j][0], best)))
        elif best >= _SIM_GRAY:
            gray.append((i, j, best))
    if not gray:
        return hard

    verdict = _llm_judge_dups([(cands[i], olds[j][0], olds[j][1], best)
                               for i, j, best in gray])
    out = list(hard)
    for k, (i, j, _best) in enumerate(gray):
        if verdict.get(k):
            out.append((i, "和已有的「%s」其实是同一件事（模型判定）"
                           % olds[j][0]))
    return out


def _cos_self_dups(cands: list) -> list:
    """**同一轮生成的多条推荐**之间互相查重，返回 [(候选下标, 原因)]。

    ★ 2026-11-20 实测缺口：_cos_dups 只拿候选和**历史**比，本轮 cands 之间
      完全不比 —— 造出「一键打包」和「成品打包」两条同义推荐时，实测返回 []，
      **两条会一起入库**，用户看到的就是「同一个功能被推了两遍」。
      这里补上：只保留前一条，后面的判重。

    标记的是**下标大的那条**（保留先出现的那条），和 _cos_dups 的语义一致。
    """
    if not cands or len(cands) < 2:
        return []
    try:
        from . import embed as _emb
        import numpy as _np
        texts = [("%s %s" % (c.get("idea") or "", c.get("how") or "")).strip()
                 for c in cands]
        mat = _emb.embed_texts(texts)
        if mat is None:
            return []
        norms = _np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        unit = mat / norms[:, None]
        sim = unit @ unit.T
    except Exception:                                   # noqa: BLE001
        logger.debug("同轮互比没跑成（跳过这一步）", exc_info=True)
        return []

    hard, gray = [], []
    for j in range(1, len(cands)):
        # 只看它和**前面**那些候选的相似度：和后面的比，保留下标小的那条
        best_i, best = -1, 0.0
        for i in range(j):
            if float(sim[j][i]) > best:
                best_i, best = i, float(sim[j][i])
        if best_i < 0:
            continue
        if best >= _SIM_HARD:
            hard.append((j, best_i, best))
        elif best >= _SIM_GRAY:
            gray.append((j, best_i, best))
    out = [(j, "和本轮另一条推荐「%s」几乎是同一个功能（相似度 %.2f）"
               % (str(cands[i].get("title") or ""), best))
           for j, i, best in hard]
    if gray:
        verdict = _llm_judge_dups(
            [(cands[j], str(cands[i].get("title") or ""),
              ("%s %s" % (cands[i].get("idea") or "",
                          cands[i].get("how") or "")).strip(), best)
             for j, i, best in gray])
        for k, (j, i, _b) in enumerate(gray):
            if verdict.get(k):
                out.append((j, "和本轮另一条推荐「%s」其实是同一件事（模型判定）"
                               % str(cands[i].get("title") or "")))
    return out


def _implemented_titles(d: dict) -> list:
    """「已经做完 / 正在做」的功能名 —— 防止把已实现的功能又推荐一遍。

    三个来源：
      ① 本模块自己的条目（已批准 / 执行中 / 已完成）；
      ② 迭代队列的历史与报告（真跑完的功能名）；
      ③ 队列里排着的（正在等做的，别重复加）。
    """
    out = []
    for it in (d.get("items") or []):
        if str(it.get("state") or "") in ("approved", "running", "done",
                                          "executing", "verified"):
            t = str(it.get("title") or "").strip()
            if t:
                out.append(t)
    try:
        from . import iter_runner as _ir
        out.extend(_ir.done_titles(limit=60))
    except Exception:                                   # noqa: BLE001
        logger.debug("读迭代历史查重失败（跳过这一步）", exc_info=True)
    seen, res = set(), []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        res.append(t)
    return res


def _dup_reason(d: dict, title: str, idea: str = "", how: str = "") -> str:
    """这条推荐是不是「已经有了 / 已经做过了」。返回原因，空串表示不重复。

    用户 2026-11-20 要求：「推荐功能加入前查一下是否已经有这个功能，
    防止重复加入」。这里只做**标题级**硬判重（确定性、不花钱）：
      ① 想过 / 在待批准里 / 已批准的标题；
      ② 已经跑完的功能（迭代历史 + 报告 + 队列）。
    「换个说法但其实是同一件事」交给 _cos_dups()（向量 + 模型判）——
    实测字面判重对同义改写完全无效，别指望标题能兜住。
    """
    t = str(title or "").strip()
    if not t:
        return "标题为空"
    if _already_known(d, t):
        return "标题和已有/想过的功能太像"
    for old in _implemented_titles(d):
        if _too_similar(t, old):
            return "「%s」已经做过" % old
    return ""


def _dup_running_reason(title: str, exclude_id: str = "") -> str:
    """**批准前**的最后一道复查：这个功能是不是已经在跑 / 在排队 / 做完了。

    ★ 2026-11-20 用户：「推荐功能时也需要复查一下推荐的功能是否已经加了，
      不要推送已经加进去的功能」。入库时已经查过两道，但那是**当时**；
      从推荐到用户点批准之间，他可能已经批准过另一条同义的、或者队列里
      已经排上了同名任务。批准是会真去改代码的动作，这一步必须再查。

    ⚠️ 这里**刻意比入库判重更保守**（只认「完全同名」和相似度 ≥ _SIM_HARD）：
      入库拦错了只是少一条推荐，批准拦错了是「点了没反应」—— 后者用户
      2026-11-16 已经报过一次，不能再犯。灰区一律放行。
    """
    t = str(title or "").strip()
    if not t:
        return ""
    nt = _norm(t)
    names = []
    try:
        from . import iter_runner as _ir
        names = [str(x) for x in (_ir.done_titles(limit=80) or []) if x]
    except Exception:                                   # noqa: BLE001
        logger.debug("批准前查重读队列失败（跳过这一步）", exc_info=True)

    for old in names:
        if nt and nt == _norm(old):
            return "「%s」已经在队列里 / 已经做过了，别重复执行" % old

    try:
        from . import embed as _emb
        import numpy as _np
        texts = [t] + names
        if len(texts) >= 2:
            mat = _emb.embed_texts(texts)
            if mat is not None:
                norms = _np.linalg.norm(mat, axis=1)
                norms[norms == 0] = 1.0
                unit = mat / norms[:, None]
                sim = unit[0] @ unit[1:].T
                k = int(sim.argmax())
                best = float(sim[k])
                if best >= _SIM_HARD:
                    return ("「%s」和它几乎是同一个功能（相似度 %.2f）"
                            "，别重复执行" % (names[k], best))
    except Exception:                                   # noqa: BLE001
        logger.debug("批准前向量查重没跑成（跳过这一步）", exc_info=True)
    return ""


# ------------------------------------------------------------------ 构思

_PROMPT = """你在帮一个「私人 AI 助理应用」想**新功能**。这个应用长这样：

- 单人使用，跑在用户自己的 Windows 电脑上，浏览器界面；
- 能聊天（本地 30B / 8B 模型 + 云端 DeepSeek 双通道）、能看图片、能跑 Python、
  能联网搜索、能读用户自己的知识库、能画图 / 剪视频 / 做 PPT / 写 Word / 做表格、
  能记住用户、能定时跑任务、能改自己的源码（有护栏和回滚）；
- 用户是重度使用者，一天到晚在用，最在意「效率」和「少重复劳动」。

你的任务：**想出一个能明显提高他用这个应用体验的全新功能**。

════════ 思考顺序（必须按这个顺序想，不要跳）════════
第 1 步：想「他现在用这个东西，哪件事最费劲、最重复、最需要人盯着」。
        要具体到场景（例：每次让我做 PPT，都要先花五分钟说清楚要几页、给谁看）。
第 2 步：想「如果有一个功能，能把这件事从"要他说"变成"不用他说"」——
        什么样的功能能真正省掉他的动作？
第 3 步：才构思功能本身：他会在界面上看到什么、点什么、得到什么。

════════ 硬要求 ════════
1. **必须是新功能**。「把 X 优化一下」「让 Y 更快」这类不算，要的是他现在
   根本做不了、或者要绕好几步才能做到的事。
2. **要有创新性**。不要只是把市面上的产品照搬过来 —— 当然可以参考，但要
   结合这个应用的独有能力（本机跑代码、能改自己源码、有长期记忆、能定时）
   想出别人做不到的形态。
3. **理论可行就行**。哪怕这个功能现在完全没有概念、市面上不存在，也可以
   大胆提；只要你能说清"用现有的能力大致怎么拼出来"。
4. **不要提**这些已经有的东西：记忆、知识库、联网搜索、跑代码、画图、
   剪视频、做 PPT/Word/Excel、定时任务、语音朗读、数字人视频、群聊、
   技能、自开发、迭代想法、计划缓存。也别提"优化界面""加快速度"这类。
   5. **files 必须写这个仓库里真实存在的路径**（如 backend/self_iterate.py、
      frontend/app.js、frontend/style.css）。执行器会按它去读文件再动手，
      写错或瞎编文件名，这一步必然白跑。新建文件才写还不存在的路径。

════════ 输出格式（严格 JSON，不要代码围栏、不要解释）════════
{"items": [{
  "title": "功能名（8 字以内，好记，用户一看就知道是什么）",
  "scene": "第 1 步的场景：他现在怎么费劲（30~80 字，要具体）",
  "idea": "第 3 步：他会在界面上看到什么、点什么、得到什么（60~120 字）",
  "why": "这个功能为什么能省他的事 / 提高效率（30~80 字）",
  "how": "大致怎么实现：用本应用已有的哪些能力拼出来，要改哪几处（60~140 字）",
  "files": ["backend/xxx.py", "frontend/xxx.js"],
  "value": 1-10 的整数（对他的价值）,
  "novelty": 1-10 的整数（有多新，照搬市面上的给低分）,
  "feasibility": 1-10 的整数（用现有能力做出来的把握）,
  "paper": "如果这个想法受某篇论文/某个研究启发，写出来；没有就空字符串"
}]}

只给 %d 条，宁可少而精，不要凑数。
这些标题已经想过或已有，**必须避开**：%s
"""


def think(force: bool = False, reason: str = "") -> dict:
    """构思一批新功能，写进待批准列表。

    用户要求「每次任务中都要时刻思考」，所以这个方法会被 selfloop 在每轮
    任务结束后调用。为了不烧穿 token，这里有三道闸：
      ① 待批准堆太多（>= _BACKLOG_CAP）就先不生成，让用户先处理；
      ② 距上次构思不到 20 分钟就不再想（任务密集时不会每轮都调）；
      ③ 手动点按钮（force=True）无视 ②，但仍受 ① 约束。
    """
    with _LOCK:
        d = load()
        pend = [x for x in (d.get("items") or []) if x.get("state") == "pending"]
        if len(pend) >= _BACKLOG_CAP and not force:
            return {"ok": True, "skipped": "待批准的新功能已堆到 %d 条，先处理一下" % len(pend),
                    "added": 0}

        now = time.time()
        last = float(d.get("ts") or 0)
        if not force and last and (now - last) < 20 * 60:
            return {"ok": True, "skipped": "距上次构思不到 20 分钟", "added": 0}

        # 已经想过的标题，喂给模型让它避开
        known = [str(x.get("title") or "") for x in (d.get("items") or [])][-40:]
        known += [str(t) for t in (d.get("seen_titles") or [])][-40:]
        # ★ 2026-11-20 用户要求「防止重复加入」：把**已经做完的功能**也喂进去，
        #   让模型一开始就避开；否则它会给一个换个说法、其实是已实现功能的点子。
        try:
            known += _implemented_titles(d)[-30:]
        except Exception:                               # noqa: BLE001
            logger.debug("收集已实现功能失败（跳过）", exc_info=True)
        known_txt = "、".join([k for k in known if k]) or "（暂无）"

        prompt = _PROMPT % (_MAX_PER_ROUND, known_txt)
        if reason:
            prompt += "\n\n补充背景（这轮刚发生的事，可参考但别被带偏）：%s" % str(reason)[:400]

        try:
            from . import self_iterate as _si
            # ⚠️ _llm_text 返回的是 (文本, 错误) **元组**，不是字符串。
            #    2026-11-16 实测：直接当字符串传给 jsonfix.parse，解析永远失败，
            #    还误以为是「模型返回的不是合法 JSON」，白查了一轮。
            raw, err = _si._llm_text(prompt, max_tokens=4096,
                                     temperature=0.9, want_json=True)
        except Exception as e:                          # noqa: BLE001
            logger.warning("featradar 构思失败：%s: %s", type(e).__name__, e)
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "added": 0}
        if err:
            logger.warning("featradar LLM 报错：%s", err)
            return {"ok": False, "error": str(err)[:300], "added": 0}
        if not raw:
            return {"ok": False, "error": "模型返回了空内容", "added": 0}

        try:
            from . import jsonfix
            j = jsonfix.parse(raw)
        except Exception:                               # noqa: BLE001
            j = None
        if not isinstance(j, dict) or not isinstance(j.get("items"), list):
            # ⚠️ dump_raw 的签名是 (txt, outdir, where) —— 2026-11-16 实测
            #    原来写成 dump_raw("featradar", raw)，参数全反了，异常被吞，
            #    原始输出一次都没存下来，排查时无证可查。
            try:
                from . import jsonfix
                from . import config as _cfg
                jsonfix.dump_raw(str(raw), Path(_cfg.data("data", "self_iterate")),
                                 "featradar")
            except Exception:                           # noqa: BLE001
                logger.debug("dump_raw failed", exc_info=True)
            return {"ok": False, "error": "模型返回的不是合法 JSON", "added": 0}

        added = []
        dups = []
        # 第一道：标题级硬判重（确定性、不花钱）—— 先筛掉一眼就是重复的
        cands = []
        for it in (j.get("items") or [])[:_MAX_PER_ROUND]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()[:24]
            idea = str(it.get("idea") or "").strip()
            if not title or len(idea) < 20:
                continue                            # 空壳，不要
            _why = _dup_reason(d, title, idea, str(it.get("how") or ""))
            if _why:
                dups.append({"title": title, "why": _why})
                logger.info("推荐被查重挡下（标题级）：%s（%s）", title, _why)
                continue
            cands.append(it)
        # 第二道：语义级判重 —— 换个说法、其实是同一个功能的也挡掉
        _bad = {}
        if cands:
            # ★ 2026-11-20（用户：「推荐功能时也要复查一下是否已经加了」）：
            #   把迭代历史里**已经做完的功能**也喂进语义比对。实测真实数据
            #   5 个历史功能一个都不在 items 里，原来这一道完全看不到它们。
            _impl = []
            try:
                _impl = _implemented_titles(d)
            except Exception:                       # noqa: BLE001
                logger.debug("收集已实现功能失败（语义查重用）", exc_info=True)
            try:
                for _idx, _why in _cos_dups(d, cands, extra_olds=_impl):
                    _bad[_idx] = _why
            except Exception:                           # noqa: BLE001
                logger.debug("语义判重异常（跳过这一道）", exc_info=True)
            # 第三道：本轮几条推荐之间互相查重。
            # ★ 2026-11-20 实测缺口：原来只跟历史比，本轮两条同义推荐
            #   （「一键打包」/「成品打包」）会**一起入库**，用户看到重复推送。
            try:
                _orig = [i for i in range(len(cands)) if i not in _bad]
                _rest = [cands[i] for i in _orig]
                for _idx, _why in _cos_self_dups(_rest):
                    if _idx < len(_orig):
                        _bad[_orig[_idx]] = _why
            except Exception:                       # noqa: BLE001
                logger.debug("同轮互比异常（跳过这一道）", exc_info=True)
        for _idx, it in enumerate(cands):
            title = str(it.get("title") or "").strip()[:24]
            idea = str(it.get("idea") or "").strip()
            if _idx in _bad:
                dups.append({"title": title, "why": _bad[_idx]})
                logger.info("推荐被查重挡下（语义级）：%s（%s）", title, _bad[_idx])
                continue
            item = {
                "id": "f" + time.strftime("%Y%m%d_%H%M%S") + "_%d" % len(added),
                "title": title,
                "scene": str(it.get("scene") or "")[:400],
                "idea": idea[:600],
                "why": str(it.get("why") or "")[:400],
                "how": str(it.get("how") or "")[:600],
                # ★ 2026-11-16 用户报「批准了没反应」时查出的根因之一：
                #   功能计划原来没有 files，执行器只能从文本里正则抠路径，
                #   而 how 写的是「改前端输入组件与后端会话记录模块」这种话，
                #   抠不到任何路径 → 执行直接报「没有可读的目标文件」。
                "files": _clean_files(it.get("files")),
                "paper": str(it.get("paper") or "")[:200],
                "value": _score(it.get("value")),
                "novelty": _score(it.get("novelty")),
                "feasibility": _score(it.get("feasibility")),
                "state": "pending",
                "note": "",
                "ts": _now(),
                "source": "manual" if force else "auto",
                "approved_at": "",
                "executed_at": "",
            }
            d.setdefault("items", []).insert(0, item)
            d.setdefault("seen_titles", []).append(title)
            added.append(item)

        d["ts"] = now
        d["last_think"] = _now()
        d["seen_titles"] = list(d.get("seen_titles") or [])[-200:]
        save(d)
        out = {"ok": True, "added": len(added), "items": added}
        if dups:
            # 让界面能说清「为什么这几条没加进去」，而不是静默少几条
            out["dups"] = dups[:10]
            out["dup_count"] = len(dups)
        return out


def _record_stage_safe(si_mod, plan_id: str = "", stage: str = "",
                       reason: str = "") -> None:
    """安全地记录执行阶段 —— 兼容 self_iterate 里可能不存在的 record_stage。

    ★ 2026-11-16 用户报「批准了没反应」排查时发现：这里直接调
      `_si.record_stage(...)`，但 self_iterate.py 里根本没有 record_stage
      这个函数 → 每次都抛 AttributeError。外面套了 except 就静默失效，
      阶段日志一条都没写下来，排查时看不到「已进队列 / 被拦下」的痕迹。

    这里做三件事：
      ① 先 getattr 探测，没有就退化成写 logger（至少日志里能看到）；
      ② 有的话按签名调用，参数不匹配也不炸；
      ③ 任何异常都吞掉但记 debug —— 阶段记录是辅助信息，绝不能影响主流程。
    """
    # ★ 2026-11-17 修：原来只在 `si_mod`（= self_iterate）上找 `record_stage`，
    #   但那个函数真正住在 **actlog**（actlog.record_stage(plan_id, stage, detail)），
    #   所以 getattr 永远拿不到 → 每次都只写一行 logger，「已进队列 / 被拦下」
    #   一条阶段流水都没落盘，时间线卡片永远是空的。
    #   这里改成：优先 actlog，si_mod 只作兜底（万一以后 self_iterate 也导出同名函数）。
    fn = None
    try:
        from . import actlog as _actlog
        fn = getattr(_actlog, "record_stage", None)
    except Exception:                               # noqa: BLE001
        logger.debug("actlog 导入失败", exc_info=True)
    if not callable(fn):
        fn = getattr(si_mod, "record_stage", None)
    if not callable(fn):
        logger.info("featradar stage=%s plan=%s reason=%s（找不到 record_stage，仅记日志）",
                    stage or "-", plan_id or "-", reason or "-")
        return
    try:
        # actlog 的签名是 (plan_id, stage, detail="")，按位置传最稳
        fn(plan_id, stage, reason)
    except Exception:                               # noqa: BLE001
        logger.debug("record_stage 调用失败（已忽略）", exc_info=True)


def _score(v) -> int:
    """把模型给的分归一化到 1~10；**没给就返回 0（未评估），不编 5 分**。

    ★ 2026-11-16 用户问「为什么有些推荐的没有价值/新颖/可行这三项指标」时查出：
      原来缺失一律 return 5，前端又把 0/缺失也兜成 5，结果「模型没打分」
      在界面上看起来就是「价值 5 新颖 5 可行 5」—— 一个假分数。
      宁可显示「未评估」，也不能让用户以为这是模型真的评过。
    """
    if v is None or v == "":
        return 0
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(1, min(10, n))


def _clean_files(v) -> list:
    """把模型给的 files 洗成干净相对路径。

    2026-11-16：执行器靠这个字段去读文件内容，脏路径会让整条动作白跑，
    所以在入库这一步就挡住：只收 backend/ frontend/ tools/ 开头的相对路径，
    去掉反斜杠、前导 ./ 和绝对路径前缀。
    """
    out = []
    for x in (v or []):
        s = str(x or "").strip().replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        s = s.strip()
        if not s or ".." in s:
            continue
        if not re.match(r"^(backend|frontend|tools|data)/[\w./\-]+\.\w+$", s):
            continue
        if s not in out:
            out.append(s)
    return out[:6]


# ------------------------------------------------------------------ 增删改查

def _verify_done(d: dict) -> bool:
    """把「标了 done 但其实一条都没跑」的记录纠正成 failed。

    ★ 2026-11-16：旧版本 approve() 提交成功就无条件写 done，卡片上显示
      「已提交执行 / 已改完」，而执行器其实 0 秒就退出了（没有已批准的动作）。
      用户看到的就是「批准了，什么都没发生」。这里在读列表时纠一次，
      最多查 5 条，避免每次打开抽屉都扫一遍所有计划。
    """
    from . import self_iterate as _si
    changed, n = False, 0
    for it in (d.get("items") or []):
        if it.get("state") != "done" or n >= 5:
            continue
        pid = str(it.get("plan_id") or "")
        if not pid:
            continue
        n += 1
        try:
            _dd, plan = _si._find_plan(pid)
        except Exception:                               # noqa: BLE001
            continue
        acts = (plan or {}).get("actions") or []
        if not acts:
            continue
        if not any(a.get("status") == "done" and not a.get("skipped")
                   for a in acts):
            it["state"] = "failed"
            it["error"] = (it.get("error")
                           or "当时只是提交进了队列，执行器一条都没跑"
                              "（旧版本的问题，点「重新执行」即可）")
            changed = True
    return changed


def list_items(state: str = "", limit: int = 100) -> dict:
    d = load()
    if _verify_done(d):
        with _LOCK:
            save(d)
    items = d.get("items") or []
    if state:
        items = [x for x in items if x.get("state") == state]
    pend = sum(1 for x in (d.get("items") or []) if x.get("state") == "pending")
    return {"ok": True, "items": items[:limit], "total": len(items),
            "pending": pend, "last_think": d.get("last_think") or ""}


def _find(d: dict, iid: str):
    for it in d.get("items") or []:
        if str(it.get("id")) == str(iid):
            return it
    return None


def _watch_plan(iid: str, pid: str, timeout: int = 900):
    """盯住这次执行，把**真实结果**写回推荐记录。

    ★ 2026-11-16 用户报「点了批准为什么没反应」：
      根因之一是 approve() 提交后**无条件**标成 done + executed_at，而执行器
      其实一条都没跑。现在提交只标 submitted，由这个线程盯到真结果再改状态，
      所以卡片上「执行中 / 已改完 / 失败原因」都是真的。

    只写三样：state（done/failed）、error（失败原因）、files（改动的文件）。
    超时 15 分钟按失败处理，不会永远停在「执行中」。
    """
    from . import self_iterate as _si
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        try:
            _d, plan = _si._find_plan(pid)
            acts = (plan or {}).get("actions") or []
            if not acts:
                break
            running = [a for a in acts
                       if a.get("status") in ("pending", "approved")]
            if running:
                continue                        # 还在跑，继续等
            done = [a for a in acts if a.get("status") == "done"]
            fails = [a for a in acts if a.get("status") == "failed"]
            review = [a for a in acts if a.get("status") == "need_review"]
            files = []
            for a in done:
                for f in (a.get("files") or []):
                    if f and f not in files:
                        files.append(f)
            if done and not fails:
                st, err = "done", ""
            elif review and not done:
                st = "failed"
                err = str((review[0].get("review_why")
                           or "被护栏拦下，等人工复核后再执行"))[:300]
            else:
                st = "failed"
                err = str((fails[0].get("error") or fails[0].get("result")
                           or "执行失败（未改到文件）"))[:300] if fails else "执行失败"
            with _LOCK:
                d2 = load()
                x = _find(d2, iid)
                if x:
                    x.update({"state": st, "finished_at": _now(),
                              "error": err, "files": files[:12]})
                save(d2)
            return
        except Exception:                       # noqa: BLE001
            logger.debug("featradar 盯执行结果出错（继续等）", exc_info=True)
    with _LOCK:
        try:
            d2 = load()
            x = _find(d2, iid)
            if x and x.get("state") == "submitted":
                x.update({"state": "failed", "finished_at": _now(),
                          "error": "执行超时（%d 分钟没等到结果）" % (timeout // 60)})
            save(d2)
        except Exception:                       # noqa: BLE001
            logger.debug("silent-except", exc_info=True)


_LOCATE_PROMPT = """你要判断「实现下面这个功能，需要改哪几个文件」。

功能名：%s
他看到的样子：%s
大致怎么做：%s

这个项目的源码文件清单（路径都是相对项目根目录的）：
%s

只输出 JSON，不要解释、不要代码围栏：
{"files": ["backend/xxx.py", "frontend/xxx.js"]}

要求：
1. 只选**真正需要改**的文件，1~4 个，不要凑数；
2. 路径必须从上面清单里原样抄，一个字都不能改；
3. 宁可选少一点 —— 选错了会让执行器去改无关文件，比少选更糟。"""


def _locate_files(it: dict) -> list:
    """功能没写 files 时，用一次轻量调用定位要改哪几个文件。

    ★ 2026-11-16 用户报「我都批准了肯定立刻执行啊」查出的第三个根因：
      早期推荐记录没有 files 字段，而执行器必须有目标文件才能读内容，
      没有就直接报「没有可读的目标文件（动作里没写 files）」→ 一条都改不了。
      让用户「重新想一批」太粗暴，这里补一次定位即可。
    """
    try:
        root = Path(__file__).resolve().parent.parent
        cand = []
        for g in ("backend/*.py", "frontend/*.js", "frontend/*.css",
                  "frontend/*.html"):
            for p in sorted(root.glob(g)):
                cand.append(str(p.relative_to(root)).replace("\\", "/"))
        if not cand:
            return []
        from . import self_iterate as _si
        prompt = _LOCATE_PROMPT % (
            str(it.get("title") or ""), str(it.get("idea") or "")[:300],
            str(it.get("how") or "")[:300], "\n".join(cand))
        raw, err = _si._llm_text(prompt, max_tokens=600,
                                 temperature=0.2, want_json=True)
        if err or not raw:
            logger.warning("featradar 定位文件失败：%s", err)
            return []
        from . import jsonfix
        j = jsonfix.parse(raw)
        if not isinstance(j, dict):
            return []
        got = _clean_files(j.get("files"))
        # 只留清单里真有的，防止模型编路径
        return [f for f in got if f in cand][:4]
    except Exception as e:                              # noqa: BLE001
        logger.warning("featradar 定位文件异常：%s: %s", type(e).__name__, e)
        return []


def approve(iid: str, note: str = "", execute: bool = True) -> dict:
    """批准一条推荐功能 → 变成可执行的动作，交给执行器真去改代码。

    备注会一路带下去（写进动作的 note），执行时模型能看到 —— 这就是用户
    要的「在卡片里备注微改任务」。
    """
    with _LOCK:
        d = load()
        it = _find(d, iid)
        if not it:
            return {"ok": False, "error": "找不到这条推荐"}
        _st = str(it.get("state") or "")
        # ★ 2026-11-20 用户：「不要推送已经加进去的功能」—— 批准这一步也复查。
        #   只在**真要执行**、且不是「用户主动重跑已完成项」时查：done 那条
        #   路径是用户明确要重跑，拦了就是又犯「点了没反应」的老毛病。
        if execute and _st != "done":
            _dupw = _dup_running_reason(str(it.get("title") or ""), iid)
            if _dupw:
                logger.info("批准被查重挡下：%s（%s）", it.get("title"), _dupw)
                return {"ok": False, "error": _dupw, "item": it,
                        "duplicate": True}
        # ★ 2026-11-16 用户报「我都批准了肯定立刻执行啊」的配套修复：
        #   旧版本会把「提交进队列但一条都没跑」的记录标成 done，用户再点批准
        #   只被告知「已经处理过了（done）」—— 这条就永久废了，只能眼睁睁看着。
        #   所以 done 要先核实计划里真有跑成功的动作，否则允许重跑。
        _ran_before = False
        if _st == "done":
            _pid0 = str(it.get("plan_id") or "")
            if _pid0:
                try:
                    from . import self_iterate as _si0
                    _d0, _p0 = _si0._find_plan(_pid0)
                    _ran_before = any(
                        (a.get("status") == "done" and not a.get("skipped"))
                        for a in ((_p0 or {}).get("actions") or []))
                except Exception:                       # noqa: BLE001
                    _ran_before = False
        if _st not in ("pending", "rejected", "failed", "submitted") and _ran_before:
            return {"ok": False, "error": "这条已经处理过了（%s）" % _st}
        it["note"] = str(note or "")[:800]
        it["state"] = "approved"
        it["approved_at"] = _now()
        save(d)

    if not execute:
        return {"ok": True, "item": it}

    # 交给执行器真去改代码：造一个单动作计划 → 提交队列
    # ⚠️ create_plan 生成的动作只带 state，而执行器/approve 读的是 status ——
    #    必须显式两边都写，否则 auto_approve_only 会认为「没有 pending 动作」。
    try:
        from . import self_iterate as _si
        from . import iter_runner as _ir
        detail = "%s\n\n【他为什么需要】%s\n\n【怎么做】%s" % (
            it.get("idea") or "", it.get("scene") or "", it.get("how") or "")
        if it.get("note"):
            detail += "\n\n【用户备注（与上面冲突时以这条为准）】%s" % it["note"]
        # 没有目标文件 → 先定位一次（约 5~10 秒）。老记录都是这种，
        # 不补的话重跑还是会报「没有可读的目标文件」。
        _act_files = _clean_files(it.get("files"))
        if not _act_files:
            _act_files = _locate_files(it)
            if _act_files:
                with _LOCK:
                    _d3 = load()
                    _x3 = _find(_d3, iid)
                    if _x3:
                        _x3["files"] = _act_files
                    save(_d3)
                logger.info("featradar 定位到文件：%s", _act_files)
        # ★ 2026-11-16 用户要求：动作 files=[] 时直接拒绝提交，并写 blocked 阶段。
        #   没有目标文件，执行器必然白跑（读不到任何文件内容），与其提交后
        #   等 15 分钟超时，不如当场拦下，让用户看到「缺少目标文件」。
        if not _act_files:
            with _LOCK:
                _d4 = load()
                _x4 = _find(_d4, iid)
                if _x4:
                    _x4.update({"state": "blocked", "finished_at": _now(),
                                "error": "缺少目标文件"})
                save(_d4)
            _record_stage_safe(_si, plan_id="", stage="blocked",
                               reason="缺少目标文件")
            return {"ok": False, "error": "缺少目标文件", "item": it,
                    "submitted": False}
        plan = _si.create_plan(
            summary="新功能：%s" % it.get("title"),
            actions=[{
                "title": it.get("title"),
                # 900 太短：idea+场景+how 一拼就截断，后半段的落地路径全丢了。
                "detail": detail[:1800],
                "risk": "mid",
                # ★★ 2026-11-16 用户报「我都批准了肯定立刻执行啊」的第二个根因：
                #   这里原来恒为 []，执行器只能从文本里正则抠路径，抠不到就报
                #   「没有可读的目标文件（动作里没写 files）」，一条都改不了。
                #   现在把模型构思时给出的真实文件路径带下去。
                "files": _act_files,
                # ★ 2026-11-16 修 bug（用户报「点了批准没反应」）：
                #   这里原来写 pending，而执行器只跑 status=="approved" 的动作；
                #   iter_runner._worker 又只在 source=="auto" 时才自动批准，
                #   featradar 的 source 是 "featradar" → 没人批准 → 执行器
                #   直接返回「这次反思没有已批准的动作」→ 一条都没跑。
                #   用户在卡片上点「批准」本身就是明确授权，这里直接标 approved。
                "status": "approved",
                "state": "approved",
                "note": it.get("note") or "",
                "origin": "feature",
                # ★ 2026-11-16 用户问「为什么有些推荐的没有价值/新颖/可行三项指标」：
                #   根因就在这儿 —— 造动作时只带了 title/detail/risk/files，
                #   三项评分留在 feat_radar.json 里没跟过来，于是「待批准的新功能」
                #   卡片上一片空白。现在原样带下去，列表接口再透传给前端。
                "value": _score(it.get("value")),
                "novelty": _score(it.get("novelty")),
                "feasibility": _score(it.get("feasibility")),
            }],
            trigger="featradar",
        )
        pid = ((plan or {}).get("plan") or {}).get("id") or ""
        if not pid:
            return {"ok": False, "error": "计划没建成", "item": it}
        sub = _ir.submit(pid, source="featradar",
                         title="新功能：%s" % it.get("title"))
        _sub_ok = bool((sub or {}).get("ok"))
        # ★ 2026-11-16 用户要求：提交到 iter_runner 后立即写 queued 阶段。
        #   这样前端/日志能立刻看到「已进队列」，而不是等执行器真跑起来才有动静。
        if _sub_ok:
            _record_stage_safe(_si, plan_id=pid, stage="queued")
        if not _sub_ok:
            # ★ 2026-11-16 修 bug：原来不管提交成没成，都标 done + executed_at，
            #   于是卡片显示「已提交执行」，实际队列根本没接住 —— 用户看到的就是
            #   「点了没反应」。提交失败必须如实标失败，并留下原因。
            _err = str((sub or {}).get("error") or "提交执行队列失败")
            with _LOCK:
                d2 = load()
                x = _find(d2, iid)
                if x:
                    x.update({"state": "failed", "finished_at": _now(),
                              "plan_id": pid, "error": _err[:300]})
                save(d2)
            return {"ok": False, "error": _err, "item": it,
                    "plan_id": pid, "submitted": False}

        # ★ 提交成功 ≠ 改成功。真结果由执行器异步产出，这里先标「执行中」，
        #   再起一个后台线程盯到真结果回写（done / failed + 改了哪些文件）。
        with _LOCK:
            d2 = load()
            x = _find(d2, iid)
            if x:
                x.update({"state": "submitted", "submitted_at": _now(),
                          "plan_id": pid, "error": ""})
            save(d2)
        threading.Thread(target=_watch_plan, args=(iid, pid), daemon=True,
                         name="featwatch-%s" % iid).start()
        return {"ok": True, "item": it, "plan_id": pid,
                "submitted": True, "state": "submitted",
                "queue_state": (sub or {}).get("state") or ""}
    except Exception as e:                              # noqa: BLE001
        logger.warning("featradar 批准后提交失败：%s: %s", type(e).__name__, e)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "item": it}


def reject(iid: str, note: str = "") -> dict:
    with _LOCK:
        d = load()
        it = _find(d, iid)
        if not it:
            return {"ok": False, "error": "找不到这条推荐"}
        it["state"] = "rejected"
        it["note"] = str(note or "")[:800]
        save(d)
        return {"ok": True, "item": it}


def update_note(iid: str, note: str = "", title: str = "") -> dict:
    """只改备注 / 标题，不动状态（卡片里随手改两笔就用这个）。"""
    with _LOCK:
        d = load()
        it = _find(d, iid)
        if not it:
            return {"ok": False, "error": "找不到这条推荐"}
        if note is not None:
            it["note"] = str(note or "")[:800]
        if title:
            it["title"] = str(title)[:24]
        save(d)
        return {"ok": True, "item": it}


def delete(iid: str) -> dict:
    with _LOCK:
        d = load()
        before = len(d.get("items") or [])
        d["items"] = [x for x in (d.get("items") or []) if str(x.get("id")) != str(iid)]
        if len(d["items"]) == before:
            return {"ok": False, "error": "找不到这条推荐"}
        save(d)
        return {"ok": True}


def stats() -> dict:
    d = load()
    items = d.get("items") or []
    return {
        "pending": sum(1 for x in items if x.get("state") == "pending"),
        "approved": sum(1 for x in items if x.get("state") == "approved"),
        "done": sum(1 for x in items if x.get("state") == "done"),
        "rejected": sum(1 for x in items if x.get("state") == "rejected"),
        "total": len(items),
        "last_think": d.get("last_think") or "",
    }


# ------------------------------------------------------------------ 前端接口

def unread_count() -> dict:
    """返回未读推荐数 —— 供前端「推荐新功能」按钮的角标使用。

    这里的「未读」= 还没被用户处理过的推荐，也就是 state == "pending"。
    用户批准 / 拒绝 / 删除之后就不再计入，角标自然消失。

    返回结构刻意做得很轻（只有数字 + 时间戳），前端可以高频轮询而不心疼：
      {"ok": True, "count": 3, "last_think": "2026-11-16 12:00:00"}

    任何异常都吞掉并返回 count=0 —— 角标是辅助信息，绝不能因为读盘失败
    把整个前端页面搞崩。
    """
    try:
        d = load()
        items = d.get("items") or []
        n = sum(1 for x in items if x.get("state") == "pending")
        return {"ok": True, "count": n,
                "last_think": d.get("last_think") or ""}
    except Exception:                                   # noqa: BLE001
        logger.debug("featradar unread_count failed", exc_info=True)
        return {"ok": False, "count": 0, "last_think": ""}


def recommend_list(state: str = "", limit: int = 50) -> dict:
    """返回推荐条目列表 —— 供前端「推荐新功能」面板使用。

    和 list_items 的区别：
      · list_items 是给内部 / 调试用的，字段全、会顺手纠正 done 状态；
      · recommend_list 是给前端面板用的，**只吐前端真正要渲染的字段**，
        并且默认按「待批准优先、时间倒序」排好，前端拿到直接渲染即可。

    参数：
      state: 可选，按状态过滤（pending / approved / submitted / done /
             failed / rejected / blocked）。留空 = 全部。
      limit: 最多返回多少条，默认 50。

    返回：
      {"ok": True, "items": [...], "total": N, "pending": M,
       "last_think": "..."}

    每条 item 只保留前端要用的字段，避免把 how / scene 这类长文本
    一股脑塞给前端（面板里点开详情再单独取也行，这里先给摘要）。
    """
    try:
        d = load()
        items = d.get("items") or []
        if state:
            items = [x for x in items if x.get("state") == state]

        # 排序：pending 排最前（用户最该先看），其余按时间倒序。
        # 用 (是否 pending 取反, 时间戳) 做 key —— pending 的排前面。
        def _sort_key(x):
            is_pending = 0 if x.get("state") == "pending" else 1
            return (is_pending, str(x.get("ts") or ""))

        try:
            items = sorted(items, key=_sort_key)
        except Exception:                               # noqa: BLE001
            logger.debug("featradar recommend_list 排序失败（忽略）", exc_info=True)

        out = []
        for x in items[:max(0, int(limit))]:
            out.append({
                "id": x.get("id") or "",
                "title": x.get("title") or "",
                "idea": x.get("idea") or "",
                "why": x.get("why") or "",
                "value": x.get("value"),
                "novelty": x.get("novelty"),
                "feasibility": x.get("feasibility"),
                "state": x.get("state") or "",
                "note": x.get("note") or "",
                "ts": x.get("ts") or "",
                "files": x.get("files") or [],
                "error": x.get("error") or "",
            })

        pend = sum(1 for x in (d.get("items") or [])
                   if x.get("state") == "pending")
        return {"ok": True, "items": out, "total": len(items),
                "pending": pend, "last_think": d.get("last_think") or ""}
    except Exception:                                   # noqa: BLE001
        logger.debug("featradar recommend_list failed", exc_info=True)
        return {"ok": False, "items": [], "total": 0, "pending": 0,
                "last_think": ""}
