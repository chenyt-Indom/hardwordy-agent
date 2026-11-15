# -*- coding: utf-8 -*-
"""工作成效向量库：把「做过什么、做成没做成、效率如何」变成可检索的记忆。

用户 2026-11-15 的要求（原话要点）
--------------------------------
· 「不仅是论文，包括平时工作任务上的效率以及成果，是否能够向量检索思考
   并成功与其他内容结合如论文进行构思」
· 「还有工作过程以及效率也要」

问题出在哪（改造前的真实状态）
------------------------------
反思的输入有 7 路：最近对话 / 技术动态 / 改动日志 / 用户体验评估 /
论文四维评估 / 能力缺口 / 焦点。**唯独没有「历史上做过同类事的结果」**。
于是模型每次都在凭空提功能：它不知道这个功能两轮前已经做过、失败了、
以及为什么失败。结果就是同一件事被反复立项（真实数据里
「回复形态闸门」重复立项 3 次，self_iterate.py 被改 17 次）。

数据源（全部是真实记录，不是模型自述）
--------------------------------------
`data/self_iterate/actlog.jsonl`  每次动作：标题 / 意图 / 文件 / 结果 / 原因 / 改动摘要
`data/self_iterate/plans.json`    每轮计划：summary / weaknesses / features
`data/selfdev_journal.jsonl`      每次改文件：rel + note

接口
----
`sync(force)`           建 / 更新索引（文本指纹变了才重算）
`search(query, top_k)`  按语义检索历史工作
`eff_stats(window)`     效率统计：成功率 / 反复改的文件 / 时间跨度
`brief_for_prompt(q)`   拼成给反思 prompt 的文本块
`status()`              给接口用的状态

⚠️ 和 kb / hivevec / papervec 一致：**embedding 不可用时退化为关键词检索**，
不能让「没装模型」把这条通道整个弄挂。
"""
import os
import re
import json
import time
import hashlib
import logging
import threading
from collections import Counter
from pathlib import Path

import numpy as np

from . import embed

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "self_iterate"
_ACTLOG = _DATA / "actlog.jsonl"
_PLANS = _DATA / "plans.json"
_JOURNAL = _ROOT / "data" / "selfdev_journal.jsonl"
_VEC = _DATA / "_work_vec.npy"
_META = _DATA / "_work_meta.json"

JOURNAL_KEEP = 400          # 改动日志只取最近这么多条（965KB 全量没必要）
MIN_VEC = 0.48              # 工作记录比论文更杂；0.36 实测会把无关查询(0.41)放进来
MIN_COV = 0.34

_lock = threading.Lock()


# ------------------------------------------------------------------ 基础
def _read_jsonl(p, limit=None):
    if not p.is_file():
        return []
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    logger.debug("silent-except", exc_info=True)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return out[-limit:] if limit else out


def _read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(p, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("[workvec] 写 %s 失败：%s", p.name, e)


# ------------------------------------------------------------------ 条目
def _short(s, n=160):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n]


def _is_temp_file(rel):
    """是不是临时验证脚本（_c3.py / _chk_target2.py / _itp_a.py 这类）。

    为什么需要过滤：actlog 里 31 个被改文件中有 13 个（42%）是这类一次性
    探针。它们被向量化后，「历史工作成果」里会混进「改文件 _c3.py：临时：
    语法+导入+符号验证」，模型会误以为改临时脚本也是正常工作内容，
    同时把真正的成果（self_iterate.py / main.py）挤出 top 榜。
    """
    s = str(rel or "").replace("\\", "/").strip()
    if not s:
        return False
    base = s.rsplit("/", 1)[-1]
    if base.startswith("__"):            # __init__.py 之类，是真的
        return False
    if "/_feature_tests/" in s:          # 正式的功能验收断言，不算临时
        return False
    return base.startswith("_") and base.endswith(".py")


def _items():
    """把三类真实记录统一成可检索条目。

    每条都带 kind / ts / text / ok，方便检索后回显「做过没做成」。
    """
    out = []

    # 1) 动作：最有价值的一类 —— 它同时含「想做什么」和「做成没有」
    for r in _read_jsonl(_ACTLOG):
        title = _short(r.get("title"), 90)
        feat = _short(r.get("feature"), 220)
        files = r.get("files") or []
        if isinstance(files, str):
            files = [files]
        _raw_files = [str(x) for x in files]
        files = [x for x in _raw_files if not _is_temp_file(x)]
        if _raw_files and not files:
            continue                     # 只动了临时探针，不算真实工作成果
        res = str(r.get("result") or "")
        detail = _short(r.get("detail"), 200)
        chg = r.get("changelog") or {}
        chg_s = _short((chg or {}).get("summary"), 200) if isinstance(chg, dict) else ""
        parts = ["动作：%s" % title]
        if feat:
            parts.append("意图：%s" % feat)
        if files:
            parts.append("改动文件：%s" % "、".join(str(x) for x in files[:6]))
        parts.append("结果：%s" % ("成功" if res == "ok" else "失败"))
        if detail:
            parts.append("说明：%s" % detail)
        if chg_s:
            parts.append("实际改动：%s" % chg_s)
        out.append({
            "kind": "action",
            "ts": r.get("ts") or "",
            "ok": res == "ok",
            "title": title,
            "files": [str(x) for x in files[:6]],
            "text": "\n".join(parts),
        })

    # 2) 计划：含「当时认为的问题」和「打算做的功能」
    d = _read_json(_PLANS, {}) or {}
    plans = d.get("items") or []
    for p in plans:
        summ = _short(p.get("summary"), 180)
        wk = [(_short(w.get("title"), 80)) for w in (p.get("weaknesses") or []) if isinstance(w, dict)]
        fts = [(_short(f.get("title"), 80)) for f in (p.get("features") or []) if isinstance(f, dict)]
        acts = p.get("actions") or []
        if isinstance(acts, list):
            ok_n = sum(1 for a in acts if isinstance(a, dict) and str(a.get("status") or "") == "done")
            act_s = "动作 %d 条，完成 %d 条" % (len(acts), ok_n)
        else:
            act_s = ""
        parts = ["计划：%s" % summ]
        if wk:
            parts.append("认定的问题：%s" % "；".join(wk[:5]))
        if fts:
            parts.append("提出的功能：%s" % "；".join(fts[:5]))
        if act_s:
            parts.append("执行：%s" % act_s)
        out.append({
            "kind": "plan",
            "ts": p.get("ts") or "",
            "ok": None,
            "title": summ,
            "files": [],
            "text": "\n".join(parts),
        })

    # 3) 改动日志：最细粒度的「工作过程」
    for j in _read_jsonl(_JOURNAL, limit=JOURNAL_KEEP):
        rel = str(j.get("rel") or "")
        note = _short(j.get("note"), 200)
        if not rel and not note:
            continue
        out.append({
            "kind": "journal",
            "ts": j.get("ts") or "",
            "ok": None,
            "title": rel,
            "files": [rel] if rel else [],
            "text": "改文件 %s：%s" % (rel, note),
        })

    return out


# ------------------------------------------------------------------ 索引
def _sig():
    try:
        return embed.signature()
    except Exception:
        return ""


def sync(force: bool = False) -> dict:
    items = _items()
    if not items:
        return {"ok": True, "n": 0, "embedded": 0}
    texts = [it["text"] for it in items]
    th = hashlib.sha1("|".join(texts).encode("utf-8")).hexdigest()[:16]
    old = _read_json(_META, {}) or {}
    if ((not force) and old.get("sig") == _sig() and old.get("text_hash") == th
            and _VEC.is_file()):
        try:
            arr = np.load(_VEC)
            if arr.ndim == 2 and arr.shape[0] == len(items):
                return {"ok": True, "n": len(items), "embedded": 0, "reused": True}
        except Exception:
            logger.debug("silent-except", exc_info=True)

    try:
        mat = embed.embed_texts(texts)
    except Exception as e:
        return {"ok": False, "n": len(items), "embedded": 0,
                "error": "向量模型异常：%s" % e}
    if mat is None:
        return {"ok": False, "n": len(items), "embedded": 0,
                "error": "向量模型不可用（检索会退化为关键词）"}
    try:
        np.save(_VEC, mat)
    except Exception as e:
        logger.warning("[workvec] 保存向量失败：%s", e)
    _write_json(_META, {"sig": _sig(), "ts": time.time(), "n": len(items),
                        "text_hash": th, "dim": int(mat.shape[1])})
    return {"ok": True, "n": len(items), "embedded": len(texts)}


# ------------------------------------------------------------------ 关键词兜底
def _chars(s):
    return [c for c in str(s or "") if ("\u4e00" <= c <= "\u9fff") or c.isalnum()]


_STOP = set("的了吗呢吧啊呀是在有和与及对把被从到就都很也还要会能可以这那个我你他她它们什么怎么如何为哪")


def _coverage(qchars, text):
    if not qchars:
        return 0.0
    qs = [c for c in qchars if c not in _STOP] or list(qchars)
    ts = set(_chars(text))
    return sum(1 for c in qs if c in ts) / float(len(qs))


def _grams(s, n=2):
    cs = _chars(s)
    return {("".join(cs[i:i + n])) for i in range(max(0, len(cs) - n + 1))}


def _gram_hits(qg, text):
    if not qg:
        return 0.0
    tg = _grams(text)
    return len(qg & tg) / float(len(qg))


# ------------------------------------------------------------------ 检索
def search(query: str, top_k: int = 6, kinds=None) -> list:
    """语义检索历史工作。

    kinds: 只在这些类型里找（"action" / "plan" / "journal"）。
    ⚠️ 为什么需要它（2026-11-15 实测）：查「回复被截断」时纯语义 top5 全是
    plan —— 计划文本里塞满了「截断/结论前置」这些词，相似度天然更高。
    但用户要的是「这类事做过没有、做成没有」，那只有 action 里有结果。
    所以 brief 会分两组：一组看思路（plan），一组看实际结果（action）。
    """
    q = (query or "").strip()
    items = _items()
    ks = None
    if kinds:
        ks = set(kinds if isinstance(kinds, (list, tuple, set)) else [kinds])
    if not items:
        return []
    if not q:
        # 没查询就返回最近的（按 ts 倒序）
        pool = [i for i in items if (ks is None or i.get("kind") in ks)]
        return sorted(pool, key=lambda x: str(x.get("ts") or ""), reverse=True)[:top_k]

    meta = _read_json(_META, {}) or {}
    vec_ok = False
    mat = None
    if meta.get("text_hash") and _VEC.is_file():
        try:
            mat = np.load(_VEC)
            if mat.ndim != 2 or mat.shape[0] != len(items):
                # ★ 索引过期（实测：数据 481 条、向量只有 479 条）时原来是静默
                #   mat=None → 整条检索退化成字符匹配，向量模型等于白装。
                #   journal 每改一次文件就会多一条，很容易出现这种偏差。
                #   这里自愈：强制重建一次，建完仍不匹配才退化。
                mat = None
                try:
                    if sync(force=True).get("ok"):
                        mat = np.load(_VEC)
                        if mat.ndim != 2 or mat.shape[0] != len(items):
                            mat = None
                except Exception:                   # noqa: BLE001
                    mat = None
        except Exception:
            mat = None
    scored = []
    if mat is not None:
        try:
            qv = embed.embed_texts([q], is_query=True)
            if qv is not None:
                qv = np.asarray(qv, dtype="float32").reshape(-1)
                mn = np.linalg.norm(mat, axis=1)
                qn = float(np.linalg.norm(qv)) or 1.0
                cos = (mat @ qv) / (mn * qn + 1e-9)
                # ★ 混合打分（2026-11-15 实测发现的问题）：
                #   纯向量分时，完全无关的「今天天气怎么样」top1 就有 0.410，
                #   高于当时门槛 0.36，会被当成「相关经验」喂给模型 —— 噪声。
                #   而 bge-small-zh 在短文本间的基础相似度本来就偏高（0.5 上下），
                #   单靠调阈值分不开「勉强沾边」和「真的相关」。
                #   所以语义分为主（0.75）、字面覆盖做纠偏（0.25）：
                #   一个词都没命中说明只是向量空间里凑得近，不是同一件事。
                qc = _chars(q)
                qg = _grams(q)
                ng = max(1, len(qg))
                for i, it in enumerate(items):
                    s = float(cos[i])
                    if s < MIN_VEC:
                        continue
                    cov = _coverage(qc, it["text"])
                    lit = 0.6 * cov + 0.4 * min(1.0, _gram_hits(qg, it["text"]) / float(ng))
                    scored.append((0.75 * s + 0.25 * lit, it))
                vec_ok = True
        except Exception:
            logger.debug("silent-except", exc_info=True)
    if not vec_ok:
        # 退化：字符覆盖 + 2-gram
        qc = _chars(q)
        qg = _grams(q)
        for it in items:
            cov = _coverage(qc, it["text"])
            gh = _gram_hits(qg, it["text"])
            s = 0.6 * cov + 0.4 * gh
            if s >= MIN_COV:
                scored.append((s, it))
    if ks is not None:
        scored = [(s, it) for s, it in scored if it.get("kind") in ks]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, it in scored[:top_k]:
        d = dict(it)
        d["score"] = round(s, 3)
        out.append(d)
    return out


# ------------------------------------------------------------------ 效率统计
def eff_stats(window: int = 200) -> dict:
    """真实效率指标（不是模型自评）。

    刻意只统计**可验证的东西**：成功率、反复改的文件、失败原因分布。
    这些数字是「效率」最硬的证据 —— 反复改同一个文件 = 前几次没改对。
    """
    rows = _read_jsonl(_ACTLOG, limit=window)
    # 临时探针（_c3.py / _chk_target.py 这类）不计入效率统计：
    # 它们不是产品代码，算进去会让成功率虚高、也会挤掉真实文件在 top 榜的位置。
    _kept = []
    for r in rows:
        _fs = r.get("files") or []
        if isinstance(_fs, str):
            _fs = [_fs]
        _real = [x for x in _fs if not _is_temp_file(x)]
        if _fs and not _real:
            continue
        _kept.append((r, _real))
    total = len(_kept)
    ok = sum(1 for r, _ in _kept if str(r.get("result") or "") == "ok")
    fail = total - ok
    file_fail = Counter()
    file_all = Counter()
    for r, fs in _kept:
        for f in fs:
            file_all[str(f)] += 1
            if str(r.get("result") or "") != "ok":
                file_fail[str(f)] += 1
    ts_list = [str(r.get("ts") or "") for r, _ in _kept if r.get("ts")]
    span_h = 0.0
    if len(ts_list) >= 2:
        try:
            t0 = time.mktime(time.strptime(min(ts_list)[:19], "%Y-%m-%d %H:%M:%S"))
            t1 = time.mktime(time.strptime(max(ts_list)[:19], "%Y-%m-%d %H:%M:%S"))
            span_h = round((t1 - t0) / 3600.0, 2)
        except Exception:
            span_h = 0.0
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "rate": round(ok / float(total), 3) if total else 0.0,
        "span_hours": span_h,
        "top_touched": file_all.most_common(6),
        "top_failed": file_fail.most_common(6),
    }


# ------------------------------------------------------------------ prompt
def brief_for_prompt(query: str = "", top_n: int = 5) -> str:
    """拼成给反思 prompt 的一段。检索失败也不抛，返回空串让主流程照常走。"""
    try:
        st = eff_stats()
        hits = search(query, top_k=top_n) if query else []
        lines = []
        if st.get("total"):
            lines.append(
                "效率概览（近 %d 次真实动作）：成功 %d / 失败 %d，成功率 %.0f%%；"
                "跨时 %.1f 小时。"
                % (st["total"], st["ok"], st["fail"], st["rate"] * 100, st["span_hours"]))
            if st.get("top_touched"):
                lines.append("被改最多的文件：" + "、".join(
                    "%s(%d次)" % (k, v) for k, v in st["top_touched"][:4]))
            if st.get("top_failed"):
                lines.append("失败最多的文件：" + "、".join(
                    "%s(%d次失败)" % (k, v) for k, v in st["top_failed"][:4]))
        if hits:
            lines.append("")
            lines.append("最相似的历史工作（按语义检索，共 %d 条）：" % len(hits))
            for h in hits:
                tag = "✅" if h.get("ok") is True else ("⚠️" if h.get("ok") is False else "·")
                body = re.sub(r"\s+", " ", h.get("text") or "")[:180]
                lines.append("- [%s] %s｜%s" % (str(h.get("ts") or "")[:16], tag, body))
        # 第二组：只看 action —— 「这类事做过没有、做成没有」的硬证据
        try:
            acts = search(query, top_k=3, kinds=("action",)) if query else []
        except Exception:
            acts = []
        if acts:
            lines.append("")
            lines.append("同类动作的实际结果（这是「做成没做成」的硬证据）：")
            for h in acts:
                tag = "✅" if h.get("ok") is True else ("⚠️" if h.get("ok") is False else "·")
                body = re.sub(r"\s+", " ", h.get("text") or "")[:160]
                lines.append("- [%s] %s｜%s" % (str(h.get("ts") or "")[:16], tag, body))
        if not lines:
            return ""
        lines.append("")
        lines.append("⚠️ 上面若有「做过但失败了」的同类工作：先说清上次为什么失败、"
                     "这次凭什么不同；不要把它原样再提一遍。"
                     "若同一功能已成功做过，不要重复立项。")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[workvec] brief 失败：%s", e)
        return ""


def status() -> dict:
    items = _items()
    st = eff_stats()
    meta = _read_json(_META, {}) or {}
    by_kind = Counter(it["kind"] for it in items)
    return {
        "ok": True,
        "n": len(items),
        "by_kind": dict(by_kind),
        "vec": _VEC.is_file(),
        "vec_dim": meta.get("dim"),
        "vec_ts": meta.get("ts"),
        "eff": st,
        "embed": embed.info() if hasattr(embed, "info") else {},
    }
