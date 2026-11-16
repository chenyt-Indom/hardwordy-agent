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

════════ 输出格式（严格 JSON，不要代码围栏、不要解释）════════
{"items": [{
  "title": "功能名（8 字以内，好记，用户一看就知道是什么）",
  "scene": "第 1 步的场景：他现在怎么费劲（30~80 字，要具体）",
  "idea": "第 3 步：他会在界面上看到什么、点什么、得到什么（60~120 字）",
  "why": "这个功能为什么能省他的事 / 提高效率（30~80 字）",
  "how": "大致怎么实现：用本应用已有的哪些能力拼出来，可能要改哪些文件（60~120 字）",
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
        for it in (j.get("items") or [])[:_MAX_PER_ROUND]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()[:24]
            idea = str(it.get("idea") or "").strip()
            if not title or len(idea) < 20:
                continue                            # 空壳，不要
            if _already_known(d, title):
                continue
            item = {
                "id": "f" + time.strftime("%Y%m%d_%H%M%S") + "_%d" % len(added),
                "title": title,
                "scene": str(it.get("scene") or "")[:400],
                "idea": idea[:600],
                "why": str(it.get("why") or "")[:400],
                "how": str(it.get("how") or "")[:600],
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
        return {"ok": True, "added": len(added), "items": added}


def _score(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, n))


# ------------------------------------------------------------------ 增删改查

def list_items(state: str = "", limit: int = 100) -> dict:
    d = load()
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
        if it.get("state") not in ("pending", "rejected"):
            return {"ok": False, "error": "这条已经处理过了（%s）" % it.get("state")}
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
        detail = "%s\n\n【怎么做】%s" % (it.get("idea") or "", it.get("how") or "")
        if it.get("note"):
            detail += "\n\n【用户备注（与上面冲突时以这条为准）】%s" % it["note"]
        plan = _si.create_plan(
            summary="新功能：%s" % it.get("title"),
            actions=[{
                "title": it.get("title"),
                "detail": detail[:900],
                "risk": "mid",
                "files": [],
                "status": "pending",
                "state": "pending",
                "note": it.get("note") or "",
                "origin": "feature",
            }],
            trigger="featradar",
        )
        pid = ((plan or {}).get("plan") or {}).get("id") or ""
        if not pid:
            return {"ok": False, "error": "计划没建成", "item": it}
        sub = _ir.submit(pid, source="featradar",
                         title="新功能：%s" % it.get("title"))
        with _LOCK:
            d2 = load()
            x = _find(d2, iid)
            if x:
                x.update({"state": "done", "executed_at": _now(), "plan_id": pid})
            save(d2)
        return {"ok": True, "item": it, "plan_id": pid,
                "submitted": bool((sub or {}).get("ok"))}
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
