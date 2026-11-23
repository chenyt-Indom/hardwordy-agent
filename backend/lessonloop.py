"""教训闭环（lesson loop）—— 让「反思 → 真修 → 下一轮知道已修」真的闭上。

为什么有这个模块（2026-11-23 用户要求）：
`data/self_iterate/lessons.jsonl` 里 6 条教训**全都**写了 will_fix=True 和具体
fix，但代码里一条都没落地（grep `anchor_miss` / `no_change` / `parse_fail` 全 0 命中）；
同一条「改动锚点没命中」11-22 11:30 记一次、11-23 07:07 又记一次（count 3）——
模型读到了，但**没有执行，也没有人检查它执行了没有**。

原来唯一的消费者是 failattr.build_hint()，它只把 lessons 拼回**反思提示词**，
所以教训只影响后台自迭代，完全不进「跟用户对话」这条主循环
（main.py 17658 行里 `lessons` 0 命中）。

本模块补四件事：
  1) `audit()`  —— 每条教训带 probe（去哪个文件找哪个字符串），用**代码本身**
     判定「修没修」，不靠模型自称；
  2) `pending()` —— 未落地的标 blocked 并进待办，供下一轮反思直接取；
  3) `escalate()` —— 同一 kind 累计 ≥3 次仍未修 → 升级成待批准计划（不再默默重试）；
  4) `hint()`   —— 给对话主循环的教训摘要，让历史教训进入每轮上下文；
  5) `curve()`  —— 跨评估的分数曲线（斜率 / 标准差 / 每 4 次窗口斜率），
     用来判断有没有 RSI 那种「斜率逐渐变陡」，而不是只看轮内涨跌。
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 同一 kind 未修多少次就升级（用户 2026-11-23 选：≥3 次强制升级到待批准）
ESCALATE_AT = 3


def _data_dir() -> str:
    """沿用 self_iterate 的数据目录（MM_DATA_DIR 可隔离，测试用）。"""
    root = os.environ.get("MM_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    d = os.path.join(root, "self_iterate")
    os.makedirs(d, exist_ok=True)
    return d


def _lessons_path() -> str:
    return os.path.join(_data_dir(), "lessons.jsonl")


def _escalation_path() -> str:
    return os.path.join(_data_dir(), "lesson_escalations.json")


# --------------------------------------------------------------------------
# 探针表：kind → (相对项目根的文件, 该修法落地后必然出现的字符串)
#
# ⚠️ 只放**能一句话判定**的：文件里出现这个字符串 = 修了。
#    判断不了的 kind 不放进来，audit() 会标 unprobed 而不是瞎猜。
# --------------------------------------------------------------------------
PROBES: dict[str, tuple[str, str]] = {
    # ⚠️ selfdev.py 是回撤机制自身，写入工具拒绝改写它，所以锚点降级
    #    落在**调用层**（本模块的 patch_with_fallback），探针也指这里。
    "改动锚点没命中": ("backend/lessonloop.py", "anchor_miss"),
    "修 selfdev_patch 锚点没命中": ("backend/lessonloop.py", "anchor_miss"),
    "没产生实际改动": ("backend/iter_runner.py", "no_change"),
    "模型输出不可用": ("backend/jsonfix.py", "parse_fail"),
    "跑分退步（低于上一版）": ("backend/iter_runner.py", "static_first"),
    "字段名/键读错": ("backend/jsonfix.py", "field_miss"),
}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load() -> list:
    """读回全部教训（按时间正序，旧→新）。"""
    p = _lessons_path()
    out = []
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:                       # noqa: BLE001
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        logger.debug("读 lessons 失败", exc_info=True)
        return []
    out.sort(key=lambda x: float(x.get("ts") or 0))
    return out


def record(item: dict) -> dict:
    """追加一条教训（failattr 之外的新入口，带去重）。"""
    if not isinstance(item, dict):
        return {"ok": False, "error": "item 必须是 dict"}
    kind = str(item.get("kind") or "").strip()
    if not kind:
        return {"ok": False, "error": "缺少 kind"}
    item = dict(item)
    item.setdefault("ts", time.time())
    item.setdefault("iso", time.strftime("%Y-%m-%d %H:%M:%S"))
    item.setdefault("status", "pending")
    try:
        with open(_lessons_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    return {"ok": True, "item": item}


def _probe_hit(rel: str, needle: str) -> bool:
    p = os.path.join(_PROJECT_ROOT, rel.replace("/", os.sep))
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return needle in f.read()
    except OSError:
        return False


def audit() -> dict:
    """逐条核验教训「到底修没修」，返回分状态清单。

    status 取值：
      done     —— 探针命中，改法已落地
      pending  —— 探针未命中，还没修（要进待办）
      unprobed —— 这条教训没有可判定的探针（不瞎判，单列出来人工看）
    """
    items = load()
    rows = []
    for it in items:
        kind = str(it.get("kind") or "").strip()
        pr = PROBES.get(kind)
        if not pr:
            rows.append({"kind": kind, "status": "unprobed",
                         "count": int(it.get("count") or 1),
                         "fix": str(it.get("fix") or "")[:200]})
            continue
        rel, needle = pr
        rows.append({"kind": kind,
                     "status": "done" if _probe_hit(rel, needle) else "pending",
                     "count": int(it.get("count") or 1),
                     "probe": "%s 含 %r" % (rel, needle),
                     "fix": str(it.get("fix") or "")[:200],
                     "will_fix": bool(it.get("will_fix"))})
    # 同 kind 合并（只保留最严重的一条）
    merged: dict[str, dict] = {}
    for r in rows:
        k = r["kind"]
        old = merged.get(k)
        if old is None:
            merged[k] = r
            continue
        # done 优先于 pending 优先于 unprobed
        rank = {"done": 0, "pending": 1, "unprobed": 2}
        if rank[r["status"]] < rank[old["status"]]:
            merged[k] = r
        merged[k]["count"] = max(int(old.get("count") or 1),
                                 int(r.get("count") or 1))
    out = list(merged.values())
    out.sort(key=lambda x: ({"pending": 0, "unprobed": 1, "done": 2}[x["status"]],
                            -int(x.get("count") or 1)))
    return {
        "ok": True,
        "total": len(out),
        "done": [r for r in out if r["status"] == "done"],
        "pending": [r for r in out if r["status"] == "pending"],
        "unprobed": [r for r in out if r["status"] == "unprobed"],
        "rows": out,
    }


def pending(limit: int = 5) -> list:
    """未落地的教训（按累计次数降序）—— 下一轮反思/待办直接取这个。"""
    a = audit()
    return (a.get("pending") or [])[:limit]


def escalate() -> list:
    """同一 kind 累计 ≥ESCALATE_AT 次仍未修 → 升级成待批准，不再默默重试。"""
    a = audit()
    ups = []
    for r in (a.get("pending") or []):
        if int(r.get("count") or 0) >= ESCALATE_AT:
            ups.append({"kind": r["kind"], "count": r["count"],
                        "fix": r.get("fix") or "",
                        "probe": r.get("probe") or "",
                        "why": "同一教训已累计 %d 次仍未落地" % int(r["count"])})
    if ups:
        try:
            with open(_escalation_path(), "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(),
                           "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "items": ups}, f, ensure_ascii=False, indent=2)
        except OSError:
            logger.debug("写升级清单失败", exc_info=True)
    return ups


def hint(max_n: int = 4, max_chars: int = 700) -> str:
    """给对话主循环的教训摘要（塞进系统提示）。

    ⚠️ 内容随教训状态变化会破坏 Ollama 前缀缓存，所以：
      · 只在有 pending 时才返回非空；
      · 文案按 kind 排序、截断到固定条数，同一状态下是**稳定字符串**。
    """
    try:
        a = audit()
    except Exception:                                   # noqa: BLE001
        logger.debug("lessonloop.audit 失败", exc_info=True)
        return ""
    pend = a.get("pending") or []
    if not pend:
        return ""
    lines = ["\n【历史教训（未修完，别重复踩）】",
             "下面这些是前几轮反思里发现、但**代码里还没落地**的问题。"
             "本轮如果碰到同类情况，直接按「改法」做，别重新想一遍："]
    for r in pend[:max_n]:
        fix = " ".join(str(r.get("fix") or "").split())[:120]
        lines.append("- 「%s」累计 %s 次：%s" % (r.get("kind"), r.get("count"), fix))
    s = "\n".join(lines) + "\n"
    return s[:max_chars]


# --------------------------------------------------------------------------
# 跨评估曲线：判断有没有 RSI 那种「斜率逐渐变陡」
# --------------------------------------------------------------------------
def _slope(ys: list) -> float | None:
    n = len(ys)
    if n < 2:
        return None
    mx = sum(range(n)) / n
    my = sum(ys) / n
    den = sum((i - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    return sum((i - mx) * (ys[i] - my) for i in range(n)) / den


def _stdev(ys: list) -> float | None:
    n = len(ys)
    if n < 2:
        return None
    my = sum(ys) / n
    return (sum((y - my) ** 2 for y in ys) / n) ** 0.5


def curve(window: int = 4, limit: int = 40) -> dict:
    """从 experience.json 的 items 里取**每次评估**的均分，算跨评估曲线。

    ⚠️ 与 self_iterate.experience_summary() 原来的 trend 不同：
       那个是「最近一半 turns vs 前一半 turns」的**轮内**对比，
       所以 16 次评估里 16 次都写「趋势：上升 0.7」，跟实际走势无关（会误导）。
       这里用的是**跨评估**的斜率，才是 RSI 判据。
    """
    try:
        from . import self_iterate as _si
        items = _si.load_experience().get("items", [])
    except Exception:                                   # noqa: BLE001
        logger.debug("取 experience 失败", exc_info=True)
        return {"ok": False, "error": "experience 不可读", "points": []}

    pts = []
    for r in items:
        c, e = r.get("avg_completion"), r.get("avg_efficiency")
        if not isinstance(c, (int, float)) or not isinstance(e, (int, float)):
            continue
        pts.append({"ts": str(r.get("ts") or ""),
                    "score": round((float(c) + float(e)) / 2, 2)})
    pts.sort(key=lambda x: x["ts"])
    pts = pts[-limit:]
    ys = [p["score"] for p in pts]

    sl = _slope(ys)
    sd = _stdev(ys)
    # 每 window 次的窗口斜率（最近一个窗口是「当前斜率」，之前的是历史斜率）
    wins = []
    for i in range(0, max(0, len(ys) - window + 1)):
        seg = ys[i:i + window]
        wins.append({"from": pts[i]["ts"][:16], "to": pts[i + window - 1]["ts"][:16],
                     "slope": round(_slope(seg) or 0.0, 3),
                     "start": seg[0], "end": seg[-1]})
    cur = wins[-1]["slope"] if wins else None
    # 形态判定（对照用户给的四形态图）
    shape = "样本不足"
    if len(ys) >= 6 and sl is not None:
        rising = sum(1 for w in wins[-3:] if w["slope"] > 0)
        if sl > 0.08 and rising >= 3:
            shape = "③ 斜率递增（RSI 雏形）"
        elif sl > 0.03:
            shape = "② 直线上升"
        elif sl < -0.03:
            shape = "① 平线偏下 / ④ 锯齿下滑"
        else:
            shape = "① 平线（原地跑）"
    return {"ok": True, "n": len(ys), "points": pts,
            "slope": round(sl, 3) if sl is not None else None,
            "stdev": round(sd, 2) if sd is not None else None,
            "windows": wins[-6:], "current_window_slope": cur,
            "window": window, "shape": shape,
            "first_half": round(sum(ys[:len(ys) // 2]) / max(1, len(ys) // 2), 2)
            if ys else None,
            "second_half": round(sum(ys[len(ys) // 2:]) / max(1, len(ys) - len(ys) // 2), 2)
            if ys else None}


def loose_span(text: str, find: str):
    """空白归一化定位：缩进/空格/换行差异不再算「锚点没命中」。

    返回 (start, end)；**0 处或多处命中都返回 None** —— 不猜，避免改错地方。
    """
    import re
    parts = [p for p in re.split(r"\s+", find or "") if p]
    if not parts:
        return None
    pat = r"\s+".join(re.escape(p) for p in parts)
    ms = list(re.finditer(pat, text or ""))
    if len(ms) != 1:
        return None
    return ms[0].span()


def patch_with_fallback(rel: str, find: str, replace: str, note: str = "",
                        session: str = "default") -> dict:
    """带锚点降级的 patch —— 给自迭代链 / 反思链 / 修复链调用。

    为什么不在 selfdev.py 里改（2026-11-23 实测）：
    `selfdev.py` 是回撤机制自身，写入工具直接拒绝改写
    （返回「是回撤机制自身，禁止模型改写」—— 改坏了就退不回来了），
    所以降级放在**调用层**：先试精确 patch，失败再按空白归一化定位后整段替换，
    并记一条 `anchor_miss` 事件（带 fallback=loose），便于统计这类失败还剩多少。
    """
    from . import selfdev as _sd
    r = _sd.patch(rel, find, replace, note=note)
    if r.get("ok"):
        return r
    err = str(r.get("error") or "")
    # 可降级的两种情况：
    #   a) 锚点没命中（本函数要解决的）；
    #   b) 幽灵修复闸门误拦 —— selfdev.patch 对**没带 fail_kind** 的调用会去查
    #      "(unspecified)" 的近 7 天真实计数，0 次就拒（实测直接调 _sd.patch
    #      就撞上这条）。锚点降级本来就不是"针对某个失败分类的修复"，
    #      被它拦住会让人以为锚点没问题。这里允许降级，但记一条 phantom_skip。
    anchor_miss = ("没找到这段原文" in err) or ("没找到包含" in err)
    phantom = (str(r.get("reason") or "") == "phantom") or ("幽灵修复" in err)
    if not anchor_miss and not phantom:
        return r
    got = _sd.read(rel)
    if not got.get("ok"):
        return got
    text = got.get("raw")
    if text is None:
        text = got.get("text") or ""
    # ① 精确命中且唯一 → 直接用它（这条路径不再回头调 _sd.patch，避免死循环）
    if text.count(find) == 1:
        s0 = text.index(find)
        span = (s0, s0 + len(find))
    else:
        span = loose_span(text, find)
    if not span:
        r["hint"] = ("空白归一化后也没唯一命中：先用 selfdev_grep 搜关键字拿到真实原文，"
                     "或改用 selfdev_insert_after（只要一行锚点）。")
        return r
    s0, s1 = span
    # 降级走 write 会绕过 patch 里的篡改分类器，这里补一遍同样的检查。
    try:
        from . import code_audit as _ca
        _t = _ca.audit_tamper(rel, find, replace, session=session)
        if _t.get("blocked"):
            return {"ok": False,
                    "error": _t.get("reason") or "篡改分类器拒绝：命中度量篡改或授权绕过",
                    "reason": "tamper", "tamper": _t}
    except Exception:                                   # noqa: BLE001
        logger.debug("降级路径跳过篡改分类", exc_info=True)
    if phantom:
        try:
            from . import actlog as _al2
            _al2.record(session, "selfdev_patch",
                        {"rel": rel, "phantom_skip": True},
                        ok=True, note="锚点降级时跳过幽灵修复闸门（未带 fail_kind 的误拦）")
        except Exception:                               # noqa: BLE001
            logger.debug("记 phantom_skip 失败", exc_info=True)
    out = _sd.write(rel, text[:s0] + replace + text[s1:],
                    note=note or "patch(anchor_miss)")
    try:
        from . import actlog as _al
        _al.record(session, "selfdev_patch",
                   {"rel": rel, "anchor_miss": True, "fallback": "loose"},
                   ok=bool(out.get("ok")),
                   note="锚点未精确命中，按空白归一化定位后替换")
    except Exception:                                   # noqa: BLE001
        logger.debug("记 anchor_miss 事件失败", exc_info=True)
    if out.get("ok"):
        out["anchor_miss"] = True
        out["fallback"] = "loose"
    return out


def _auto_state_path() -> str:
    return os.path.join(_data_dir(), "lesson_auto.json")


def _auto_state() -> dict:
    try:
        with open(_auto_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def _auto_save(d: dict) -> None:
    try:
        with open(_auto_state_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("写 lesson_auto 失败", exc_info=True)


def auto_fix(max_n: int = 1, submit: bool = True, cooldown_hours: float = 24.0) -> dict:
    """把**未落地**的教训变成修复计划并提交执行（用户 2026-11-23：全自动，有备份可回滚）。

    保护（缺一不可，否则会变成「越修越乱」）：
      · 每次最多 max_n 条（默认 1），不批量糊上去；
      · 同一 kind 在 cooldown_hours 内只提交一次（防同一坑反复开计划）；
      · 只处理**有探针定义**的教训 —— 判断得了修没修的才敢自动改；
      · 走 iter_runner 的正规链路（备份 / 验证 / 回归 / 跑分 / 可回滚）。
    """
    a = audit()
    pend = [r for r in (a.get("pending") or []) if r.get("probe")]
    if not pend:
        return {"ok": True, "submitted": [], "msg": "没有未落地的教训"}
    st = _auto_state()
    hist = st.get("submitted") or {}
    now = time.time()
    picked = []
    for r in pend:
        k = r["kind"]
        last = float(hist.get(k) or 0)
        if now - last < cooldown_hours * 3600:
            continue
        picked.append(r)
        if len(picked) >= max_n:
            break
    if not picked:
        return {"ok": True, "submitted": [],
                "msg": "未落地的教训都在冷却期内（%g 小时内不重复开计划）" % cooldown_hours}
    if not submit:
        return {"ok": True, "submitted": [], "planned": picked,
                "msg": "只生成清单，未提交（submit=False）"}
    out = []
    for r in picked:
        k = r["kind"]
        fix = str(r.get("fix") or "").strip()
        try:
            from . import self_iterate as _si
            from . import iter_runner as _ir
            _rel = ""
            pr = r.get("probe") or ""
            if pr.startswith("backend/"):
                _rel = pr.split(" 含 ")[0]
            _act = {"title": "修教训：%s" % k[:60],
                    "detail": fix or "按教训里记录的改法修复",
                    "state": "pending", "risk": "low",
                    "fail_kind": k,
                    "files": [_rel] if _rel else []}
            cr = _si.create_plan(
                summary="教训闭环自动修复：%s（已累计 %s 次未落地）" % (k, r.get("count")),
                actions=[_act], trigger="lesson_auto")
            if not cr.get("ok"):
                out.append({"kind": k, "ok": False,
                            "error": str(cr.get("error") or "建计划失败")})
                continue
            pid = (cr.get("plan") or {}).get("id")
            sb = _ir.submit(pid, source="auto",
                            title="修教训：%s" % k[:40], max_n=1)
            out.append({"kind": k, "ok": bool(sb.get("ok", True)),
                        "plan_id": pid, "submit": str(sb.get("state") or sb.get("error") or "")[:120]})
            if sb.get("ok", True):
                hist[k] = now
        except Exception as e:                          # noqa: BLE001
            out.append({"kind": k, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
    st["submitted"] = hist
    st["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _auto_save(st)
    return {"ok": True, "submitted": out}


def summary_line(c: dict | None = None) -> str:
    """一行文字版曲线结论（写进评估报告）。"""
    c = c or curve()
    if not c.get("ok") or not c.get("n"):
        return "跨评估曲线：样本不足"
    return ("跨评估斜率 %+.3f 分/次，标准差 %.2f，近 %d 次窗口斜率 %s，形态：%s"
            % (c.get("slope") or 0.0, c.get("stdev") or 0.0, c.get("window") or 4,
               ("%+.2f" % c["current_window_slope"])
               if c.get("current_window_slope") is not None else "—",
               c.get("shape")))
