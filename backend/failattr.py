# -*- coding: utf-8 -*-
"""失败归因表（Failure Attribution Table）。

为什么需要
----------
自动自迭代以前只记「这次失败了」+ 一句原始错误文本（actlog / rounds.jsonl），
**没有结构化的「为什么失败」和「为什么回滚」**。结果是同一类坑反复踩：
补丁锚点没命中、断言写得不可执行、改动过大导致回归失败、文件被 Office 占用…
下一次反思只拿到一句模糊错误，模型只能从零再猜一遍，等于每次都在重新学习。

这张表做三件事：
1. `record()` —— 每次失败 / 回滚落一条结构化记录：环节、归类、失败原因、
   回滚触发条件、可复用教训；
2. `brief()`  —— 把「Top 失败类型 + 最近几条原因与教训」压成一段塞进反思提示词，
   让自动自迭代**重点参考真实归因**，而不是凭感觉换方向；
3. `guard()`  —— 某类失败已重复 N 次时，明确告诉调用方「这条路已经走不通了」。

存储：data/self_iterate/fail_attribution.jsonl（一行一条，追加写）
"""
import os
import json
import re
import time
import threading

from . import config

_LOCK = threading.RLock()
_KEEP_LINES = 2000          # 超过这个行数就裁剪，只留最近 1200 条
# 补丁工具「find 命中多处被拒绝」的报错是动态数字（命中 3 处 / 命中 12 处），
# 子串规则表匹配不到，用正则单独认。
_MULTI_HIT_RE = re.compile(r"命中\s*\d+\s*处")

# ---------------------------------------------------------------------------
#  失败类型表：kind -> (中文名, 默认教训, 关键词)
#  ⚠️ 顺序有意义：越具体的规则排越前（No module named 要先于 ImportError 命中）。
# ---------------------------------------------------------------------------
_RULES = [
    ("missing_dep", "缺依赖 / 环境问题",
     "先用标准库绕开；确实要装依赖时先确认本机有没有，别假设",
     ("no module named", "缺依赖", "pip install", "modulenotfounderror",
      "cannot find module")),
    ("permission", "文件被占用 / 无权限",
     "文件可能正被 Office/WPS 打开；改成另存新文件，不要硬覆盖",
     ("permissionerror", "拒绝访问", "被占用", "permission denied",
      "access is denied", "winerror 32")),
    ("timeout", "执行超时 / 卡死",
     "把大改拆小；单步不要超过工具时限，长任务分批做并留检查点",
     ("超时", "timeout", "timed out", "超过 60 秒", "卡住", "deadline")),
    ("too_big", "改动过大 / 内容被截断",
     "一次只改一处；大改拆成能单独验证的几步，别一次甩整份重写",
     ("改动过大", "内容被截断", "truncat", "too large", "超出长度",
      "token 上限", "文件过大")),
    ("key_error", "字段名/键读错",
      "别凭印象取字段：先打印一次真实返回结构再写 key；不确定就用 .get(k, 默认值)，"
      "不要直接下标 —— 键名猜错会直接抛 KeyError",
      ("keyerror", "键不存在", "no such key", "字段不存在", "没有这个键",
       "缺少键", "missing key", "unknown key")),
    ("patch_not_found", "改动锚点没命中",
     "不要凭印象写 find 原文：先 read 目标段落，再复制真实文本；"
     "或改用整份重写 / 按行号插入",
     ("没找到要替换", "没找到这段原文", "这段原文", "未找到原文",
       "find 未命中", "锚点", "未找到匹配", "命中 0 处", "0 条命中",
      "no match", "not found", "找不到要")),
    ("syntax_error", "语法错误",
     "改完先跑 py_compile / node --check 再谈行为，语法不过等于没改",
     ("syntaxerror", "invalid syntax", "unexpected indent", "语法错误",
      "编译失败", "node --check")),
    ("import_error", "导入失败",
     "改模块接口时同步检查所有 import 处；新函数必须能被真实 import 到",
     ("importerror", "cannot import", "导入失败", "导入错误", "circular import")),
    ("assert_fail", "断言没过",
     "断言要写稳定可执行的判据（返回码 / 字段值 / 数量），不要写「看起来对」",
     ("断言", "assert", "校验没通过", "验收失败", "verify fail", "verify 未通过")),
    ("regression_fail", "行为回归失败",
     "改动前先跑基线，改完跑同一批用例对比；没有基线就没有「没搞坏」的证据",
     ("回归", "regression", "基线", "baseline")),
     # ★ 2026-11-23：scoredrop.py 一直在写 kind="score_regress"，但这里没登记，
     #   落进账本就成「未归类」、教训为空。跑分退步是独立一类：不是「改坏了」，
     #   而是「改了但没赚到分」，修法完全不同，不能混进 regression_fail。
     ("score_regress", "跑分退步（低于上一版）",
      "跑分退步 = 验收不通过，不算完成：先把掉的分补回来再谈新功能；"
      "别在同一轮里既加功能又改评分口径，否则分不清是哪个导致的",
      ("跑分退步", "评分退步", "分数比上一版低", "分数低于上一版",
       "分数掉了", "score_regress", "低于上个版本")),
    ("no_edit", "没产生实际改动",
     "先说清改哪个文件的哪个函数再动手；空转一轮等于失败，不如不跑",
     ("没有改动", "未产生改动", "无改动", "empty diff", "no changes",
      "没有做任何修改")),
    ("model_error", "模型输出不可用",
     "要求 JSON 时加容错解析；输出为空 / 被截断就重试一次，再不行换更小粒度",
     ("json", "解析失败", "返回为空", "空回复", "模型没返回", "输出为空")),
    ("manual_rollback", "人工回滚（效果不达标）",
     "用户主动回滚说明「自检通过但观感/效果不行」—— 下次先出小样给用户看，再全量改",
     ("人工回滚", "用户回滚", "效果不达标")),
]

_KIND_MAP = {k: (label, lesson) for k, label, lesson, _ in _RULES}

_STAGE_ZH = {
    "apply": "应用改动", "syntax": "语法校验", "import": "导入校验",
    "regression": "行为回归", "assert": "功能断言", "agent": "模型直接操作",
    "tool": "工具调用", "rollback": "回滚", "verify": "独立复核",
    "manual": "人工", "score": "跑分", "": "未标注",
}


def _dir() -> str:
    return os.path.join(config.data("data"), "self_iterate")


def _path() -> str:
    return os.path.join(_dir(), "fail_attribution.jsonl")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def classify(err: str, stage: str = "") -> tuple:
    """把一段错误文本归类成 (kind, 中文名, 教训)。

    没有命中任何规则时返回 ("other", "未归类", ...)，**不猜**。
    """
    s = (err or "").lower()
    if not s.strip():
        return ("other", "未归类", "先把失败原因写清楚，没有原因就无法归因")
    for kind, label, lesson, keys in _RULES:
        for kw in keys:
            if kw.lower() in s:
                return (kind, label, lesson)
    # 补丁工具的另一类报错是「命中 3 处：…」（find 命中了多处被拒绝）。
    # 它是动态数字，子串规则匹配不到；不加这条会归成「未归类」，
    # 反思端按 kind 聚类时就聚不到「锚点没命中」这一类上。
    if _MULTI_HIT_RE.search(s):
        _lbl, _lsn = _KIND_MAP.get("patch_not_found", ("改动锚点没命中", ""))
        return ("patch_not_found", _lbl, _lsn)
    return ("other", "未归类", "先把失败原因写清楚，没有原因就无法归因")


def record(kind: str = "", stage: str = "", title: str = "",
           why_failed: str = "", why_rollback: str = "", files=None,
           plan_id: str = "", action_id: str = "", model: str = "",
           lesson: str = "", extra=None, traceback_text: str = "",
           context: str = "") -> dict:
    """写一条失败归因记录。任何异常都不抛出（日志写不进去不能反过来搞挂主流程）。

    ★ 2026-11-22 用户强调「失败归因的原因**一定要详细记录**」：
      原来的上限是 why_failed 1200 字 / why_rollback 600 字，实测会把
      多行 traceback 的后半段（真正说明问题的那几行，如 `KeyError: 'items'`
      所在帧）截掉，只剩「Traceback (most recent call last):」这种没用的开头。
      现在放宽到 2400 / 1200，并且**单独收 traceback 与上下文**：
        · traceback_text —— 完整堆栈（不受 why_failed 长度影响）
        · context        —— 失败前在做什么（哪一步、什么输入、什么参数）
      原因写得太短（<24 字）时自动打 `too_vague` 标记，供反思端要求补全。
    """
    try:
        why_failed = str(why_failed or "")[:2400]
        _kind, _label, _lesson = classify(why_failed, stage)
        kind = str(kind or _kind).strip() or _kind
        label = _KIND_MAP.get(kind, ("", ""))[0] or _label
        lesson = str(lesson or "").strip() or _KIND_MAP.get(kind, ("", ""))[1] or _lesson
        rec = {
            "ts": time.time(),
            "iso": _now(),
            "plan_id": str(plan_id or ""),
            "action_id": str(action_id or ""),
            "title": str(title or "")[:120],
            "files": [str(f) for f in (files or [])][:8],
            "stage": str(stage or ""),
            "stage_zh": _STAGE_ZH.get(str(stage or ""), str(stage or "")),
            "kind": kind,
            "kind_zh": label,
            "why_failed": why_failed,
            "why_rollback": str(why_rollback or "")[:1200],
            "lesson": lesson[:400],
            "model": str(model or "")[:80],
        }
        # 完整堆栈单独存，不吃 why_failed 的额度
        _tb = str(traceback_text or "")[:6000].strip()
        if _tb:
            rec["traceback"] = _tb
        # 失败前在做什么：哪一步、什么输入、什么参数 —— 只看错误文本看不出这个
        _ctx = str(context or "")[:800].strip()
        if _ctx:
            rec["context"] = _ctx
        # 原因太短 = 下次没法据此改进，明确标出来让反思端要求补全
        if len(why_failed.strip()) < 24:
            rec["too_vague"] = True
        if extra:
            rec["extra"] = extra
        with _LOCK:
            os.makedirs(_dir(), exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _maybe_trim()
        return rec
    except Exception as e:                                # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _maybe_trim() -> None:
    """行数超上限时裁掉最老的（在锁内调用）。"""
    p = _path()
    try:
        if not os.path.isfile(p):
            return
        with open(p, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= _KEEP_LINES:
            return
        keep = lines[-1200:]
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, p)
    except Exception:                                     # noqa: BLE001
        pass


# ---------------------------------------------------------------- 改进方法库
# ★ 2026-11-22 用户要求：「自动自迭代功能需要重点参考这份归因表，
#   以**不断构建思路**改进自身」。只记「为什么失败」不够 —— 还要记
#   「上次针对它想到了什么办法、后来有没有奏效」，否则每轮反思都在
#   从零重新构思同一个问题（等于没有积累）。
#   存 data/self_iterate/lessons.jsonl，按 kind 去重覆盖（保留最新想法）。

def _lessons_path() -> str:
    return os.path.join(_dir(), "lessons.jsonl")


def record_lessons(lessons, plan_id: str = "") -> dict:
    """把反思产出的 fail_lessons 写进改进方法库（按 kind 覆盖，保留最新）。"""
    try:
        arr = [x for x in (lessons or []) if isinstance(x, dict)]
        if not arr:
            return {"ok": True, "saved": 0}
        with _LOCK:
            os.makedirs(_dir(), exist_ok=True)
            old = {}
            p = _lessons_path()
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:                 # noqa: BLE001
                            continue
                        k = str(r.get("kind") or "").strip()
                        if k:
                            old[k] = r
            for x in arr:
                k = str(x.get("kind") or "").strip()[:60]
                if not k:
                    continue
                old[k] = {
                    "kind": k,
                    "count": x.get("count"),
                    "root_cause": str(x.get("root_cause") or "")[:800],
                    "fix": str(x.get("fix") or "")[:800],
                    "verify": str(x.get("verify") or "")[:400],
                    "will_fix": bool(x.get("will_fix", True)),
                    "plan_id": str(plan_id or ""),
                    "iso": _now(),
                    "ts": time.time(),
                }
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in old.values():
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
            return {"ok": True, "saved": len(arr), "total": len(old)}
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def load_lessons() -> dict:
    """读回改进方法库：kind -> 记录。"""
    p = _lessons_path()
    out = {}
    if not os.path.isfile(p):
        return out
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:                         # noqa: BLE001
                    continue
                k = str(r.get("kind") or "").strip()
                if k:
                    out[k] = r
    except Exception:                                     # noqa: BLE001
        return {}
    return out


def load(n: int = 0) -> list:
    """读回记录（新的在前）。n=0 表示全部。"""
    p = _path()
    if not os.path.isfile(p):
        return []
    rows = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:                         # noqa: BLE001
                    continue
    except Exception:                                     # noqa: BLE001
        return []
    rows.reverse()
    return rows[:n] if n else rows


def stats(days: int = 30) -> dict:
    """按类型 / 文件 / 环节聚合，并标出「重复 ≥3 次」的高频坑。"""
    since = time.time() - max(1, int(days)) * 86400
    rows = [r for r in load() if float(r.get("ts") or 0) >= since]
    by_kind, by_file, by_stage = {}, {}, {}
    for r in rows:
        k = str(r.get("kind") or "other")
        by_kind[k] = by_kind.get(k, 0) + 1
        st = str(r.get("stage_zh") or r.get("stage") or "未标注")
        by_stage[st] = by_stage.get(st, 0) + 1
        for f in (r.get("files") or []):
            by_file[str(f)] = by_file.get(str(f), 0) + 1
    top_kind = sorted(by_kind.items(), key=lambda x: -x[1])
    top_file = sorted(by_file.items(), key=lambda x: -x[1])
    return {
        "total": len(rows),
        "days": days,
        "by_kind": dict(top_kind),
        "by_kind_zh": {(_KIND_MAP.get(k, ("", ""))[0] or k): v
                       for k, v in top_kind},
        "by_file": dict(top_file[:10]),
        "by_stage": dict(sorted(by_stage.items(), key=lambda x: -x[1])),
        "repeat": [{"kind": k, "count": v,
                    "kind_zh": _KIND_MAP.get(k, ("", ""))[0] or k}
                   for k, v in top_kind if v >= 3],
        "rollback": len([r for r in rows if str(r.get("why_rollback") or "").strip()]),
    }


def guard(kind: str = "", files=None, n: int = 3, days: int = 30) -> dict:
    """某类失败已重复 ≥n 次时返回 blocked=True，并带上历史教训。

    调用方据此要求「先说清上次为什么没做对 + 这次根因」，避免在同一条路上叠加改动。
    """
    try:
        since = time.time() - max(1, int(days)) * 86400
        rows = [r for r in load() if float(r.get("ts") or 0) >= since]
        hit = [r for r in rows if kind and str(r.get("kind")) == str(kind)]
        if files:
            fs = {str(f) for f in files}
            hit = [r for r in hit if fs & set(r.get("files") or [])]
        if len(hit) >= int(n):
            return {"blocked": True, "count": len(hit), "kind": kind,
                    "kind_zh": _KIND_MAP.get(kind, ("", ""))[0] or kind,
                    "lessons": [str(r.get("lesson") or "") for r in hit[:3]
                                if r.get("lesson")],
                    "reason": "%s 近 %d 天已重复失败 %d 次"
                              % (_KIND_MAP.get(kind, ("", ""))[0] or kind,
                                 int(days), len(hit))}
        return {"blocked": False, "count": len(hit), "kind": kind}
    except Exception as e:                                # noqa: BLE001
        return {"blocked": False, "error": "%s: %s" % (type(e).__name__, e)}


def brief(limit: int = 8, days: int = 30, max_chars: int = 2400,
          query: str = "") -> str:
    """压成一段给反思提示词用的紧凑归因摘要（没有记录时返回空串）。

    query：本轮打算做的事（或召回的历史任务 + 执行统计）。给了就走
    `failvec` 的**语义相似检索**，把「换了说法但其实是同一个坑」的历史
    失败也带进提示词 —— 纯按 kind/files 聚类做不到这一点（用户 2026-11-22
    要求「加进去，而且要实现体系化完美运行」）。
    """
    try:
        st = stats(days)
        _empty = not st.get("total")
        if _empty and not load_lessons():
            return ""
        if _empty:
            # ★ 2026-11-22 修：原来这里直接 `return ""`，导致归因表为空期间
            #   「已经构思过的改进方法」永远送不进反思提示词 —— 而 lessons
            #   里恰恰积累着上一轮想好的办法。用户要求「不断积累改进经验」，
            #   空表不等于没经验，不能把整段吞掉。
            lines = ["归因表暂无记录（还没有真实失败被记下来）"]
        else:
            lines = ["共 %d 条失败/回滚记录（近 %d 天）" % (st["total"], days)]
        if st.get("by_kind_zh"):
            top = list(st["by_kind_zh"].items())[:6]
            lines.append("Top 失败类型：" + "、".join("%s×%d" % (k, v)
                                                    for k, v in top))
        if st.get("by_stage"):
            lines.append("失败环节：" + "、".join(
                "%s×%d" % (k, v) for k, v in list(st["by_stage"].items())[:5]))
        if st.get("by_file"):
            lines.append("最常出问题的文件：" + "、".join(
                "%s×%d" % (k, v) for k, v in list(st["by_file"].items())[:5]))
        rep = st.get("repeat") or []
        if rep:
            lines.append("⚠️ 重复 ≥3 次的坑（这些路已经走不通，别再原样重试）："
                         + "、".join("%s(%d)" % (r["kind_zh"], r["count"])
                                    for r in rep[:5]))
        # ★ 语义相似检索（用户 2026-11-22：「加进去，而且要实现体系化完美运行」）。
        #   上面几段都是「按 kind/files 聚类统计」，能说「patch_not_found 失败了
        #   7 次」，但答不了更关键的一问：**我这次要做的事，和历史上哪一次踩的
        #   是同一个坑**。纯分类会把「锚点写太死」和「行号已变」算成同一类。
        #   这里按语义找近邻，把真正可复用的那条教训捞出来。
        _extra = 0
        _q = str(query or "").strip()
        if _q:
            try:
                from . import failvec as _FV
                _sim = _FV.brief_similar(_q, top_k=4,
                                         days=max(int(days), 90), max_chars=900)
                if _sim:
                    lines.append(_sim)
                    _extra = 900        # 给这段单独留额度，别把「最近几条」挤掉
            except Exception:                             # noqa: BLE001
                pass
        # ★ 已构思的改进方法（上一轮反思产出的）——直接给出来，别让模型
        #   每轮从零重新想同一个问题；但要提示「先判断还适用不适用」。
        _ls = load_lessons()
        if _ls:
            _rows = sorted(_ls.values(), key=lambda x: -float(x.get("ts") or 0))[:5]
            _segs = []
            for x in _rows:
                if not (x.get("fix") or x.get("root_cause")):
                    continue
                _segs.append("· [%s] 根因：%s ｜ 上次想到的办法：%s%s" % (
                    x.get("kind") or "-",
                    str(x.get("root_cause") or "-")[:110],
                    str(x.get("fix") or "-")[:150],
                    (" ｜ 验证：" + str(x.get("verify") or "")[:80])
                    if x.get("verify") else ""))
            if _segs:
                lines.append("🧠 已经构思过的改进方法（先判断是否仍然适用；"
                             "适用的直接沿用并落地，不适用的说明为什么变）：")
                lines.extend(_segs)
        # ★ 2026-11-22 修：原来这里直接 `load(limit)` —— 它取的是**文件里
        #   最后写入的 N 条**，完全不看 ts。一条 100 天前的记录只要写在后面，
        #   就会被当成「最近」喂给反思，和用户要求的「按时效性」正好相反。
        #   这里按 days 过滤，多取 3 倍再截断，避免过滤后条数不够。
        _since = time.time() - max(1, int(days)) * 86400
        rows = [r for r in load(max(int(limit) * 3, int(limit)))
                if float(r.get("ts") or 0) >= _since][:int(limit)]
        if rows:
            lines.append("最近 %d 条：" % len(rows))
            for i, r in enumerate(rows, 1):
                t = str(r.get("title") or r.get("action_id") or "未命名动作")[:40]
                seg = ["%d) [%s/%s] %s" % (i, r.get("kind_zh") or r.get("kind"),
                                           r.get("stage_zh") or r.get("stage") or "-", t)]
                # ★ 用户要求「原因一定要详细记录」，反思端就必须**看得到细节**：
                #   原来 why_failed 只截 160 字，traceback 后几行（真正说明问题的
                #   那几行）全被切掉，模型只能从「失败了」重新猜。现在放宽到
                #   400/240，并补上 context（失败前在做什么）和堆栈末行。
                wf = str(r.get("why_failed") or "").replace("\n", " ")[:400]
                wr = str(r.get("why_rollback") or "").replace("\n", " ")[:240]
                ls = str(r.get("lesson") or "").replace("\n", " ")[:200]
                if wf:
                    seg.append("   为什么失败：%s" % wf)
                _cx = str(r.get("context") or "").replace("\n", " ")[:200]
                if _cx:
                    seg.append("   失败时在做什么：%s" % _cx)
                _tb = str(r.get("traceback") or "").strip()
                if _tb:
                    # 堆栈末行 = 异常类型 + 消息，信息密度最高
                    _last = [x.strip() for x in _tb.splitlines() if x.strip()]
                    if _last:
                        seg.append("   堆栈末行：%s" % _last[-1][:200])
                if wr:
                    seg.append("   为什么回滚：%s" % wr)
                if ls:
                    seg.append("   教训：%s" % ls)
                if r.get("too_vague"):
                    seg.append("   ⚠️ 这条原因写得过于笼统（不足 24 字），"
                               "下次遇到同类问题必须先补全具体错误再动手")
                lines.append("\n".join(seg))
        out = "\n".join(lines)
        if len(out) > max_chars + _extra:
            out = out[:max_chars + _extra] + (
                "\n…（归因表较长，已截断；完整内容在 "
                "data/self_iterate/fail_attribution.jsonl）")
        return out
    except Exception:                                     # noqa: BLE001
        return ""


def summary_text(n: int = 10) -> str:
    """给人看的纯文本摘要（前端 / 用户直接问「为什么失败」时用）。"""
    st = stats(30)
    if not st.get("total"):
        return "失败归因表是空的（近 30 天没有失败或回滚记录）。"
    lines = ["失败归因表：近 30 天 %d 条，其中回滚 %d 条"
             % (st["total"], st.get("rollback") or 0)]
    for k, v in list(st.get("by_kind_zh", {}).items()):
        lines.append("· %s：%d 次" % (k, v))
    rows = load(n)
    if rows:
        lines.append("\n最近 %d 条：" % len(rows))
        for i, r in enumerate(rows, 1):
            lines.append("%d) %s ｜%s｜%s" % (
                i, r.get("iso") or "", r.get("kind_zh") or r.get("kind") or "",
                str(r.get("title") or "")[:40]))
            if r.get("why_failed"):
                lines.append("   失败：%s" % str(r.get("why_failed"))[:200])
            if r.get("why_rollback"):
                lines.append("   回滚：%s" % str(r.get("why_rollback"))[:160])
    return "\n".join(lines)
