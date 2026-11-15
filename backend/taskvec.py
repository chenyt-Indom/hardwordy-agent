"""任务经验向量库：把「我过去做过什么任务、成没成、怎么做的、踩了什么坑」向量化。

为什么需要（2026-11-15 用户要求）：
    「不仅是论文，包括平时工作任务上的效率以及成果，是否能够向量检索思考
      并成功与其他内容结合如论文进行构思」

在此之前，反思只吃两样东西：
    · 论文（papervec，外部理论）—— 有向量检索
    · 经验（experience_brief，内部实践）—— 只有**聚合统计**
      （平均完成度、摩擦点计数 top5），是「压成一段纯文本」，
      检索不到「上次遇到类似问题时我具体是怎么做的、成没成」。

结果就是：模型每轮都在从零构思，做过的坑会重复踩。
这里补上内部经验的向量检索，让 reflect 能按当前痛点召回
「历史上相似的任务 + 结果 + 当时的问题与改法」。

数据源（都是真实运行数据，不是自评）：
    ① data/self_iterate/actlog.jsonl   自动改动的动作记录
       （title / feature 打算做什么 / changelog 实际改了什么 / result / detail）
    ② data/self_iterate/experience.json  体验评估
       （turns[]：task / completion / efficiency / problem / fix / evidence）

检索算法与 papervec 保持一致（余弦 + 字面覆盖 + 2-gram 三阈值），
因为同一套阈值在中文语料上已经调稳了，没必要再造一套。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

import numpy as np

from . import embed

logger = logging.getLogger(__name__)

_DATA = Path(__file__).resolve().parent.parent / "data" / "self_iterate"
_VEC = _DATA / "_task_vec.npy"
_META = _DATA / "_task_meta.json"
_ACTLOG = _DATA / "actlog.jsonl"
_EXP = _DATA / "experience.json"
_PLANS = _DATA / "plans.json"

# 任务文本是中文，和 query 同语言，语义分比英文论文稳，阈值可以稍严。
MIN_VEC = 0.45
MAX_ACT = 400          # 最多取多少条动作记录（老记录价值低，按时间倒序截）
MAX_TURN = 300         # 最多取多少条评估轮次


# ---------------------------------------------------------------- 基础工具

def _read_json(p: Path, default):
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[taskvec] 读 %s 失败：%s", p.name, e)
    return default


def _write_json(p: Path, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[taskvec] 写 %s 失败：%s", p.name, e)


def _clip(s, n):
    s = str(s or "").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _changelog_text(cl) -> str:
    """changelog 有两种形态：None 或 {"summary":..., "files":[...]}。"""
    if not cl:
        return ""
    if isinstance(cl, dict):
        return _clip(cl.get("summary") or "", 300)
    return _clip(cl, 300)


# ---------------------------------------------------------------- 数据装载

def _is_temp_file(rel) -> bool:
    """临时验证脚本（_c3.py / _chk_target2.py / _itp_a.py 这类）。

    为什么过滤：actlog 里有「新建标记文件 2 | 涉及文件：backend/_chk_target2.py
    | 结果：成功」这种探针记录。它们被当成「任务经验」后，模型会看到一堆
    「新建标记文件」的成功案例，把真正有价值的经验（如「改了什么信息不可见」
    这类构思）挤下去。
    """
    s = str(rel or "").replace("\\", "/").strip()
    if not s:
        return False
    base = s.rsplit("/", 1)[-1]
    if base.startswith("__"):
        return False
    if "/_feature_tests/" in s:
        return False
    return base.startswith("_") and base.endswith(".py")


def _load_actions() -> list:
    """读自动改动日志，产出统一 item。"""
    out = []
    if not _ACTLOG.is_file():
        return out
    rows = []
    try:
        with _ACTLOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:                           # noqa: BLE001
                    continue
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[taskvec] 读 actlog 失败：%s", e)
        return out

    for r in rows[-MAX_ACT:]:
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        ok = str(r.get("result") or "") == "ok"
        feature = _clip(r.get("feature") or "", 400)
        change = _changelog_text(r.get("changelog"))
        detail = _clip(r.get("detail") or "", 300)
        files = r.get("files") or []
        if not isinstance(files, list):
            files = []
        _raw = [str(f) for f in files]
        files = [f for f in _raw if not _is_temp_file(f)]
        if _raw and not files:
            continue                    # 只动了临时探针，不算任务经验
        outcome = "成功" if ok else "失败"
        out.append({
            "key": "act:%s:%s" % (r.get("ts") or "", r.get("action") or ""),
            "kind": "action",
            "ts": str(r.get("ts") or ""),
            "title": title,
            "ok": ok,
            "outcome": outcome,
            "text": "\n".join(x for x in [
                "任务：" + title,
                ("打算做什么：" + feature) if feature else "",
                ("实际改了什么：" + change) if change else "",
                ("涉及文件：" + "、".join(str(f) for f in files[:6])) if files else "",
                ("结果：" + outcome),
                ("说明：" + detail) if detail else "",
            ] if x),
            "files": [str(f) for f in files[:6]],
        })
    return out


def _load_turns() -> list:
    """读体验评估轮次，产出统一 item。"""
    out = []
    doc = _read_json(_EXP, {}) or {}
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return out
    flat = []
    for ev in items:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id") or ev.get("ts") or "")
        ts = str(ev.get("ts") or "")
        for t in (ev.get("turns") or []):
            if isinstance(t, dict):
                flat.append((eid, ts, t))
    for eid, ts, t in flat[-MAX_TURN:]:
        task = str(t.get("task") or "").strip()
        if not task:
            continue
        comp = t.get("completion")
        eff = t.get("efficiency")
        problem = _clip(t.get("problem") or "", 300)
        fix = _clip(t.get("fix") or "", 300)
        evd = _clip(t.get("evidence") or "", 200)
        senti = str(t.get("sentiment") or "")
        try:
            comp_n = float(comp)
        except Exception:                                   # noqa: BLE001
            comp_n = None
        outcome = "完成度%s/10 效率%s/10" % (
            comp if comp is not None else "?", eff if eff is not None else "?")
        out.append({
            "key": "turn:%s:%s" % (eid, t.get("i") or ""),
            "kind": "turn",
            "ts": ts,
            "title": task,
            "ok": (comp_n is not None and comp_n >= 7),
            "outcome": outcome,
            "score": comp_n,
            "text": "\n".join(x for x in [
                "任务：" + task,
                "结果：" + outcome + (("，用户情绪：" + senti) if senti else ""),
                ("问题：" + problem) if problem else "",
                ("当时想到的改法：" + fix) if fix else "",
                ("证据：" + evd) if evd else "",
            ] if x),
            "files": [],
        })
    return out


def _load_ideas() -> list:
    """读历史反思计划里的 features（构思），并关联执行结果。

    这是最有价值的一类经验：它记录了「当时想到什么功能、为什么值得做、
    对应哪个痛点、打算怎么做、验证怎么写、引了哪篇论文」，
    再和 actlog 的标题对上，就能得出「这个构思最后成没成」。
    """
    doc = _read_json(_PLANS, {}) or {}
    plans = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(plans, list):
        return []

    # 先建「标题 -> 执行结果」索引（actlog 的 title 与 feature 的 title 同名）
    done = {}
    for a in _load_actions():
        t = str(a.get("title") or "").strip()
        if t:
            done.setdefault(t, a)

    out, seen = [], {}
    for p in plans:
        if not isinstance(p, dict):
            continue
        ts = str(p.get("ts") or "")
        theory_map = {}
        _fa = p.get("feature_actions")
        if isinstance(_fa, list):
            for fa in _fa:
                if isinstance(fa, dict) and fa.get("title"):
                    theory_map[str(fa["title"]).strip()] = str(fa.get("theory") or "")
        _fs = p.get("features")
        if not isinstance(_fs, list):
            continue
        for f in _fs:
            if not isinstance(f, dict):
                continue
            title = str(f.get("title") or "").strip()
            if not title:
                continue
            # 同一功能在多轮里反复出现，只留最新那次（plans 是按新到旧）
            if title in seen:
                continue
            seen[title] = True
            why = _clip(f.get("why") or "", 400)
            pain = _clip(f.get("pain") or "", 200)
            how = _clip(f.get("how") or "", 400)
            verify = _clip(f.get("verify") or "", 200)
            theory = _clip(theory_map.get(title) or f.get("theory") or "", 200)
            hit = done.get(title)
            if hit is None:
                outcome, ok = "已构思，未执行", None
            elif hit.get("ok"):
                outcome, ok = "构思→执行成功", True
            else:
                outcome, ok = "构思→执行失败：" + _clip(hit.get("detail") or "", 80), False
            out.append({
                "key": "idea:%s:%s" % (ts, title[:40]),
                "kind": "idea",
                "ts": ts,
                "title": title,
                "ok": ok,
                "outcome": outcome,
                "text": "\n".join(x for x in [
                    "构思的功能：" + title,
                    ("对应痛点：" + pain) if pain else "",
                    ("为什么值得做：" + why) if why else "",
                    ("打算怎么做：" + how) if how else "",
                    ("怎么验证：" + verify) if verify else "",
                    ("论文依据：" + theory) if theory else "",
                    ("最终结果：" + outcome),
                ] if x),
                "files": [str(x) for x in (f.get("files") or [])[:6]],
            })
    return out


def load_all() -> list:
    """构思 + 动作记录 + 评估轮次，按时间倒序（新的优先）。"""
    items = _load_ideas() + _load_actions() + _load_turns()
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return items


def _doc_text(it: dict) -> str:
    return "%s\n%s" % (it.get("title") or "", it.get("text") or "")


# ---------------------------------------------------------------- 索引

def _text_hash(items) -> str:
    h = 0
    for it in items:
        for ch in _doc_text(it):
            h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return "%d_%d" % (len(items), h)


def build_index(force: bool = False) -> dict:
    """重建向量索引。内容没变就复用，不重复烧算力。"""
    items = load_all()
    if not items:
        return {"ok": True, "n": 0, "embedded": 0, "note": "还没有任务记录"}
    sig = embed.signature()
    th = _text_hash(items)
    old = _read_json(_META, {}) or {}
    if (not force and sig == old.get("sig") and th == old.get("text_hash")
            and _VEC.is_file()):
        return {"ok": True, "n": len(items), "embedded": 0, "reused": True}

    texts = [_doc_text(it) for it in items]
    mat = embed.embed_texts(texts)
    if mat is None:
        return {"ok": False, "n": len(items), "embedded": 0,
                "error": "向量模型不可用（检索会退化为关键词）"}
    try:
        np.save(_VEC, mat)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[taskvec] 保存向量失败：%s", e)
    _write_json(_META, {"sig": sig, "ts": time.time(), "n": len(items),
                        "text_hash": th, "dim": int(mat.shape[1]),
                        "items": [{"key": it.get("key"), "title": it.get("title")}
                                  for it in items]})
    return {"ok": True, "n": len(items), "embedded": len(texts)}


# ---------------------------------------------------------------- 检索

def _chars(s) -> set:
    return {c for c in str(s or "") if not c.isspace()}


def _coverage(qchars: set, text: str) -> float:
    if not qchars:
        return 0.0
    return len(qchars & _chars(text)) / float(len(qchars))


def _grams(s, n: int = 2) -> set:
    s = "".join(str(s or "").split())
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _gram_hits(qgrams: set, text: str) -> int:
    if not qgrams:
        return 0
    tg = _grams(text)
    return len(qgrams & tg)


def search(query: str, top_k: int = 6) -> list:
    """按当前痛点召回相似的历史任务经验（语义 + 关键词混合）。"""
    query = str(query or "").strip()
    if not query:
        return []
    items = load_all()
    if not items:
        return []

    sig = embed.signature()
    meta = _read_json(_META, {}) or {}
    mat = None
    if sig == meta.get("sig") and _VEC.is_file():
        try:
            arr = np.load(_VEC)
            if arr.ndim == 2 and arr.shape[0] == len(items):
                mat = arr
        except Exception:                                   # noqa: BLE001
            mat = None

    qchars = _chars(query)
    cov = np.asarray([_coverage(qchars, _doc_text(i)) for i in items],
                     dtype=np.float32)

    def _score_with(arr):
        if arr is None or arr.ndim != 2 or arr.shape[0] != len(items):
            return None
        qv = embed.embed_texts([query], is_query=True)
        if (qv is not None and getattr(qv, "shape", (0,))[0] == 1
                and len(qv[0]) == arr.shape[1]):
            return (arr @ qv[0]).astype(np.float32)
        return None

    vec = _score_with(mat) if mat is not None else None
    if vec is None:
        # 索引缺失 / 维度对不上（数据变了却没重建）/ 向量模型不可用 —— 重建一次再试。
        # ⚠️ 2026-11-15 实测踩过：数据从 242 条变成 221 条后索引对不上 → mat=None，
        #   而下面的阈值全以 v 为准（此时 v 恒为 0）→ **一条都召不回**，
        #   docstring 里写的「退化为关键词」其实根本没发生，通道静默失效。
        try:
            if build_index().get("ok") and _VEC.is_file():
                vec = _score_with(np.load(_VEC))
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)

    final = 0.75 * vec + 0.25 * cov if vec is not None else cov.copy()
    qgrams = _grams(query)
    gram = [_gram_hits(qgrams, _doc_text(i)) for i in items]

    picked = []
    for i in range(len(items)):
        v = float(vec[i]) if vec is not None else 0.0
        c = float(cov[i])
        if vec is None:
            # 向量彻底不可用 → 纯关键词检索。不给这条分支的话 v 恒为 0，
            # 三个阈值全不满足，整个通道会**静默失效**（返回空却不报错）。
            ok = (c >= 0.70 and gram[i] > 0) or c >= 0.90
        else:
            ok = False
            if v >= 0.58:
                ok = True                  # 强语义，不要求字面重合
            elif v >= MIN_VEC and gram[i] > 0 and c >= 0.50:
                ok = True                  # 中等语义 + 2-gram + 字面过半
            elif v >= MIN_VEC and c >= 0.70:
                ok = True                  # 语义够 + 字面覆盖高
        if ok:
            it = dict(items[i])
            it["score"] = round(float(final[i]), 4)
            it["sem"] = round(v, 4)
            it["cov"] = round(c, 4)
            picked.append(it)

    picked.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return picked[:max(1, int(top_k))]


def brief_for_prompt(top_n: int = 5, query: str = "") -> str:
    """把召回的任务经验压成一段 prompt 文本。

    query 为空时退化为「最近的高价值经验」，保证至少给模型一点内部实践参考。

    ⚠️ 参数顺序是 (top_n, query)，和 workvec.brief_for_prompt(query, top_n) **相反**。
    实测（2026-11-15）：按 workvec 的顺序调用会抛
    `ValueError: invalid literal for int() with base 10: '...'` —— 这正是本模块
    写好后一直没被接入反思的直接原因。这里做类型守卫，两种顺序都接受。
    """
    if isinstance(top_n, str):          # 被按 (query, top_n) 调用了
        top_n, query = (query if isinstance(query, int) else 5), top_n
    top_n = int(top_n or 5)
    got = search(query, top_k=top_n) if query else []
    if not got:
        # 退化：取最近的成功 + 最近的失败各若干，别让这一段彻底空着
        items = load_all()
        ok = [i for i in items if i.get("ok")][:3]
        bad = [i for i in items if not i.get("ok")][:3]
        got = ok + bad
    if not got:
        return ""
    lines = ["【过往任务经验（向量召回，真实执行记录）】"]
    for it in got[:top_n]:
        tag = "✅" if it.get("ok") else "⚠️"
        lines.append("%s %s｜%s" % (tag, it.get("title") or "", it.get("outcome") or ""))
        body = (it.get("text") or "").split("\n")
        for ln in body:
            if ln.startswith(("问题：", "当时想到的改法：", "说明：", "实际改了什么：")):
                lines.append("   " + _clip(ln, 160))
    return "\n".join(lines)


def stats() -> dict:
    items = load_all()
    n_act = sum(1 for i in items if i.get("kind") == "action")
    n_turn = sum(1 for i in items if i.get("kind") == "turn")
    ok = sum(1 for i in items if i.get("ok"))
    meta = _read_json(_META, {}) or {}
    return {"n": len(items), "actions": n_act, "turns": n_turn,
            "ok": ok, "fail": len(items) - ok,
            "indexed": bool(_VEC.is_file() and meta.get("sig") == embed.signature()),
            "dim": meta.get("dim"), "built_at": meta.get("ts")}
