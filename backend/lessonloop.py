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

# 改完之后要观察多久才敢判「真修好了」（小时）。
# 太短 = 刚改完就自封修好（实测：任何无关改动刷新 mtime 就会把旧教训判成 fixed）。
OBSERVE_HOURS = 6.0


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
    #
    # ⚠️⚠️ needle 必须是**修复逻辑本身**才有的字符串（2026-11-23 实测教训）：
    #   原来 "模型输出不可用" 指向 `parse_fail`、"字段名/键读错" 指向 `field_miss`，
    #   而这两个词就写在「记录这次失败」的函数里（jsonfix._note_parse_fail /
    #   _note_miss）—— 只要账本代码还在，探针就**恒命中**，等于没检查。
    #   现在改成指向修复能力：容错解析 / 截断补齐 / 容错取值。
    "改动锚点没命中": ("backend/lessonloop.py", "def patch_with_fallback"),
    "修 selfdev_patch 锚点没命中": ("backend/lessonloop.py", "def patch_with_fallback"),
    # 硬检查点：diff 为空直接判失败重来（不是单纯记账）
    "没产生实际改动": ("backend/iter_runner.py", "no_change"),
    # 容错解析链：strip_notice → balanced → _close_truncated → parse
    "模型输出不可用": ("backend/jsonfix.py", "def _close_truncated"),
    # 跑分前先取自检计数（static_first）
    "跑分退步（低于上一版）": ("backend/iter_runner.py", "static_first"),
    # 容错取值：键名写错/层级读错时不抛错
    "字段名/键读错": ("backend/jsonfix.py", "def safe_get"),
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


# --------------------------------------------------------------------------
# 行为探针（2026-11-23 修正）
#
# ⚠️ 原来 PROBES 的 needle 是 `anchor_miss` / `no_change` / `parse_fail` 这类
#    **失败事件名**，而它们本来就写在「记录这次失败」的代码里
#    （例：jsonfix 里 actlog.record("jsonfix", "parse_fail")），
#    所以 audit() 恒判 done —— 实测 6 条教训全「已修」、pending 恒空、
#    auto_fix 永远返回「没有未落地的教训」、lesson_auto.json 从未生成，
#    一条教训都没被真正修过。**假阳性比没检查更危险**。
#
# 现在用两条独立证据，任一足以支撑才判 fixed：
#   A. 行为证据：这条教训对应 fail kind 在「修复之后」**没再复发**；
#   B. 改动证据：该文件在教训记录之后**真的被改过**（actlog 成功改动 / mtime）。
#   两个都没有 → pending（只说不做）。
# --------------------------------------------------------------------------

def _fail_attr_path() -> str:
    return os.path.join(_data_dir(), "fail_attribution.jsonl")


def _read_jsonl(p: str) -> list:
    out = []
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
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
        logger.debug("读 %s 失败", p, exc_info=True)
    return out


def _num_ts(v) -> float:
    """时间戳归一化：float / 'YYYY-MM-DD HH:MM:SS' 都认，认不出返回 0。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s[:19] if len(s) >= 19 else s, fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


def _last_ok_edit(rel: str) -> float:
    """该文件「最近一次被改动」的时间戳 = max(actlog 成功改动, 文件 mtime)。

    actlog 里 result=ok 且 files 命中 → 说明这一版真的落盘了；
    mtime 是兜底（有的改动走 selfdev_patch，不进 iter_runner 的账本）。
    """
    best = 0.0
    rows = _read_jsonl(os.path.join(_data_dir(), "actlog.jsonl"))
    for r in rows:
        if str(r.get("result") or "").lower() != "ok":
            continue
        files = r.get("files") or []
        if not isinstance(files, list):
            continue
        if any(str(f).replace("\\", "/").endswith(rel) for f in files):
            best = max(best, _num_ts(r.get("ts")))
    p = os.path.join(_PROJECT_ROOT, rel.replace("/", os.sep))
    try:
        best = max(best, os.path.getmtime(p))
    except OSError:
        pass
    return best


def _recur_after(kind_zh: str, since: float) -> int:
    """该 kind 在 since 之后复发了几次（0 = 修完之后没再犯）。"""
    k = str(kind_zh or "").strip()
    if not k:
        return 0
    n = 0
    for r in _read_jsonl(_fail_attr_path()):
        kz = str(r.get("kind_zh") or "").strip()
        kk = str(r.get("kind") or "").strip()
        if k not in (kz, kk):
            continue
        if _num_ts(r.get("ts") or r.get("iso")) > since:
            n += 1
    return n


def judge(it: dict) -> dict:
    """单条教训的真实判定（不靠模型自称，只看账本 + 文件）。

    返回 {kind, status, evidence, recur, edited_at, probe}
      fixed    —— **代码里有修复逻辑**（探针命中），且教训之后没再复发
      verifying—— 修复逻辑在、没复发，但刚改完还在观察窗内（不自封修好）
      pending  —— 修复逻辑不在代码里（只说不做/改歪了），或修复后仍复发
      unprobed —— 没有可判定的文件，不瞎判

    ⚠️ 判定主证据是 **probe 命中**，不是文件 mtime（2026-11-23 实测修正）：
       原逻辑 `if edited <= t0: pending(只说不做)` 把「修复早于教训记录」
       的条目一律判成没修 —— 实测 2 条 pending 里，探针 code_hit 全是 True
       （iter_runner 里 static_first / no_change 都在），落地率被压成 0/6，
       RSI 的「教训落地率」项恒为 0，auto_fix 还会反复去修已经修好的东西。
    """
    kind = str(it.get("kind") or "").strip()
    t0 = _num_ts(it.get("ts") or it.get("iso"))
    pr = PROBES.get(kind)
    if not pr:
        return {"kind": kind, "status": "unprobed", "recur": 0,
                "edited_at": 0.0, "probe": "", "evidence": "没有可判定的文件"}
    rel, needle = pr
    edited = _last_ok_edit(rel)
    hit = _probe_hit(rel, needle)
    recur = _recur_after(kind, t0)
    ev = {
        "file": rel, "needle": needle, "code_hit": hit,
        "edited_at": time.strftime("%m-%d %H:%M", time.localtime(edited)) if edited else "",
        "lesson_at": time.strftime("%m-%d %H:%M", time.localtime(t0)) if t0 else "",
        "recur": recur,
    }
    # ① 修复逻辑不在代码里 —— 这是硬事实，与 mtime 无关
    if not hit:
        if edited > t0:
            return {"kind": kind, "status": "pending", "recur": recur,
                    "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                    "evidence": "改过 %s，但**修复逻辑不在代码里**（改歪了）" % rel,
                    "detail": ev}
        return {"kind": kind, "status": "pending", "recur": recur,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "evidence": "修复逻辑不在代码里，该文件也没改过（只说不做）", "detail": ev}
    # ② 修复逻辑在，但教训之后又复发 —— 改得不够，继续 pending
    if recur > 0:
        return {"kind": kind, "status": "pending", "recur": recur,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "evidence": "修复逻辑已在代码里，但之后又复发 %d 次" % recur, "detail": ev}
    # ③ 修复逻辑在、没复发。观察窗从「改动时间 / 教训时间」的**晚者**算起。
    #    ⚠️ 原来写 `if edited > t0`：教训若比改动更新（同一 kind 被反复重记），
    #    该条件为假 → 跳过观察窗直接判 fixed。实测 12:32 记的教训 12:33 就
    #    判成「已修」，而「未复发」此时只代表「才过了一分钟」—— 假阳性，
    #    会把 RSI 的教训落地率抬虚高。取 max 后，刚记的教训一律先进观察窗。
    base = max(edited, t0)
    left = OBSERVE_HOURS * 3600.0 - (time.time() - base)
    if left > 0:
        return {"kind": kind, "status": "verifying", "recur": 0,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "evidence": "修复逻辑已在、暂未复发，观察中（还差 %.1f 小时）" % (left / 3600.0),
                "detail": ev}
    return {"kind": kind, "status": "fixed", "recur": 0,
            "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
            "evidence": "代码探针命中（%s 含 %r）且教训之后未复发"
                        % (rel, needle),
            "detail": ev}


# RSI 缓存：hint() 每轮构建系统提示都会走到这里，
# 不能每次都把 actlog.jsonl 全解析一遍（几千行）。
_RSI_CACHE: dict = {"ts": 0.0, "val": None}


def rsi_index(limit: int = 400, cache_sec: float = 60.0) -> dict:
    """客观 RSI 指数（0~100）—— 只用可复现账本，**不含 LLM 自评**。

    为什么必须换掉自评：94 次评估的跨评估斜率 0.001、标准差 1.74（10 分制），
    噪声比任何真实改进都大 —— 拿它当目标函数，斜率天然是 0，看不出有没有进步。

    四项（各自 0~1，加权平均后 ×100）：
      1) 动作成功率   actlog: ok / (ok+fail)          —— 改得动不动
      2) 幽灵率       1 - phantom / 有标记的动作      —— 声称改了其实没改
      3) 教训落地率   fixed / (fixed+pending)         —— 学到的东西有没有落地
      4) 静态质量     1 - min(1,(high*3+mid)/10)      —— 代码本身有没有变差
    """
    _now = time.time()
    if (cache_sec > 0 and _RSI_CACHE["val"] is not None
            and _now - _RSI_CACHE["ts"] < cache_sec):
        return _RSI_CACHE["val"]
    rows = _read_jsonl(os.path.join(_data_dir(), "actlog.jsonl"))[-max(20, limit):]
    ok = sum(1 for r in rows if str(r.get("result") or "").lower() == "ok")
    bad = sum(1 for r in rows if str(r.get("result") or "").lower() in ("fail", "failed", "error"))
    marked = [r for r in rows if r.get("phantom") is not None]
    ph = sum(1 for r in marked if r.get("phantom") is True)
    a = audit()
    fx = len(a.get("fixed") or a.get("done") or [])
    pd_ = len(a.get("pending") or []) + len(a.get("verifying") or [])
    hi = mid = 0
    try:
        au = json.load(open(os.path.join(_data_dir(), "audits.json"), encoding="utf-8"))
        it0 = (au.get("items") or [{}])[0]
        c = it0.get("counts") or {}
        hi, mid = int(c.get("high") or 0), int(c.get("mid") or 0)
    except Exception:                                   # noqa: BLE001
        logger.debug("读 audits 失败", exc_info=True)
    parts = []
    if ok + bad:
        parts.append(("动作成功率", ok / (ok + bad), 0.40,
                      "%d/%d" % (ok, ok + bad)))
    if marked:
        parts.append(("无幽灵率", 1 - ph / len(marked), 0.15,
                      "幽灵 %d/%d" % (ph, len(marked))))
    if fx + pd_:
        parts.append(("教训落地率", fx / (fx + pd_), 0.25, "%d/%d" % (fx, fx + pd_)))
    parts.append(("静态质量", max(0.0, 1 - min(1.0, (hi * 3 + mid) / 10.0)), 0.20,
                  "high %d / mid %d" % (hi, mid)))
    tw = sum(p[2] for p in parts) or 1.0
    score = round(100 * sum(p[1] * p[2] for p in parts) / tw, 1)
    res = {"ok": True, "score": score,
           "parts": [{"name": n, "value": round(v, 3), "weight": w, "detail": d}
                     for n, v, w, d in parts],
           "at": time.strftime("%H:%M:%S", time.localtime(_now))}
    _RSI_CACHE["ts"], _RSI_CACHE["val"] = _now, res
    return res


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
        j = judge(it)
        j["count"] = int(it.get("count") or 1)
        j["fix"] = str(it.get("fix") or "")[:200]
        j["will_fix"] = bool(it.get("will_fix"))
        rows.append(j)
    # 同 kind 合并（只保留最严重的一条）
    merged: dict[str, dict] = {}
    for r in rows:
        k = r["kind"]
        old = merged.get(k)
        if old is None:
            merged[k] = r
            continue
        # fixed 优先于 verifying 优先于 pending 优先于 unprobed
        rank = {"fixed": 0, "verifying": 1, "pending": 2, "unprobed": 3}
        if rank[r["status"]] < rank[old["status"]]:
            merged[k] = r
        merged[k]["count"] = max(int(old.get("count") or 1),
                                 int(r.get("count") or 1))
    out = list(merged.values())
    out.sort(key=lambda x: ({"pending": 0, "verifying": 1, "unprobed": 2,
                             "fixed": 3}[x["status"]],
                            -int(x.get("count") or 1)))
    return {
        "ok": True,
        "total": len(out),
        # fixed 是标准名；done 保留给旧调用方（scheduler/main 用 a["done"]）
        "fixed": [r for r in out if r["status"] == "fixed"],
        "done": [r for r in out if r["status"] == "fixed"],
        "pending": [r for r in out if r["status"] == "pending"],
        "verifying": [r for r in out if r["status"] == "verifying"],
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
    lines = ["\n【历史教训（未修完，别重复踩）】"]
    # RSI 当前最弱项：让主循环知道「整体最该修哪一项」。
    # 没有这行，模型只看到零散教训，不知道该先动哪个。
    # ⚠️ 放在最前面，避免被 max_chars 截断。
    try:
        _w = rsi_weakest()
        if _w.get("ok"):
            lines.append("- 【RSI 最弱项】%s（%.0f%%，权重 %.2f）：%s"
                         % (_w["name"], (_w.get("value") or 0) * 100,
                            _w.get("weight") or 0, _w.get("advice")))
    except Exception:                                   # noqa: BLE001
        logger.debug("RSI 最弱项注入失败", exc_info=True)
    lines += ["下面这些是前几轮反思里发现、但**代码里还没落地**的问题。"
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
    pend = [r for r in (a.get("pending") or [])
            if r.get("probe") and r.get("status") == "pending"]
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


# --------------------------------------------------------------------------
# RSI 主指标：客观指数（rsi_index）+ 历史序列 + 趋势形态
#
# 为什么必须落盘历史：rsi_index() 只给**当前瞬时值**，单点没法判断
# 「斜率是否逐渐变陡」—— 那正是 RSI 与普通指标的区别。
# 只有按时间采样、再对序列算**分段斜率**，才能区分四种形态：
#   ① 平台（原地跑）② 直线上升 ③ 斜率递增（RSI）④ 锯齿下滑
# --------------------------------------------------------------------------

def _rsi_hist_path() -> str:
    return os.path.join(_data_dir(), "rsi_history.jsonl")


def rsi_sample(force: bool = False, min_gap: float = 60.0) -> dict:
    """采样一次 RSI 写入历史。同一分钟内重复调用只记一条（防刷爆账本）。"""
    cur = rsi_index()
    p = _rsi_hist_path()
    rows = _read_jsonl(p)
    now = time.time()
    if not force and rows:
        last = _num_ts(rows[-1].get("ts"))
        if now - last < min_gap:
            return {"ok": True, "skipped": True, "score": cur.get("score"),
                    "n": len(rows), "why": "距上次采样不足 %.0f 秒" % min_gap}
    rec = {"ts": now,
           "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
           "score": cur.get("score"),
           "parts": {x["name"]: x["value"] for x in (cur.get("parts") or [])}}
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": "写 rsi_history 失败：%s" % e}
    return {"ok": True, "score": rec["score"], "n": len(rows) + 1,
            "iso": rec["iso"]}


def rsi_trend(limit: int = 120, window: int = 5) -> dict:
    """RSI 历史序列的趋势形态（判断有没有 RSI 那种斜率递增）。

    判定用**三段斜率**而不是只看整体斜率：
      前 1/3、中 1/3、后 1/3 各算一次斜率，
      s1 < s2 < s3 且 s1 > 0 → 斜率递增（RSI 形态）；
      整体 > 0 但三段不递增 → 直线上升；
      |整体| ≤ 0.05 → 平台（原地跑）；
      整体 < -0.05 → 下滑。
    """
    rows = _read_jsonl(_rsi_hist_path())[-max(10, limit):]
    pts = [{"ts": r.get("iso") or "", "score": float(r.get("score") or 0)}
           for r in rows if isinstance(r.get("score"), (int, float))]
    ys = [p["score"] for p in pts]
    if len(ys) < 4:
        return {"ok": True, "n": len(ys), "points": pts, "shape": "样本不足",
                "slope": None, "segs": [],
                "advice": "继续采样（每次评估收尾会自动记一条）"}
    sl = _slope(ys) or 0.0
    sd = _stdev(ys) or 0.0
    k = max(1, len(ys) // 3)
    segs = []
    for i, name in enumerate(("前段", "中段", "后段")):
        a0 = i * k if i < 2 else 2 * k
        a1 = (i + 1) * k if i < 2 else len(ys)
        seg = ys[a0:a1]
        segs.append({"name": name, "n": len(seg),
                     "slope": round(_slope(seg) or 0.0, 3),
                     "mean": round(sum(seg) / max(1, len(seg)), 1)})
    s1, s2, s3 = [x["slope"] for x in segs]
    if s1 < s2 < s3 and s1 > 0:
        shape = "③ 斜率递增（RSI）"
    elif sl > 0.05:
        shape = "② 直线上升"
    elif sl < -0.05:
        shape = "④ 锯齿下滑"
    else:
        shape = "① 平台（原地跑）"
    return {"ok": True, "n": len(ys), "points": pts[-40:],
            "slope": round(sl, 3), "stdev": round(sd, 2),
            "segs": segs, "shape": shape,
            "first": ys[0], "last": ys[-1],
            "delta": round(ys[-1] - ys[0], 1), "window": window}


def rsi_weakest() -> dict:
    """当前**最该修**的一项：权重最高、得分最低。

    RSI 的实质不是「有个分数」，而是**拿分数决定下一步改什么**。
    没有这一步，指数只是仪表盘，不会自我改进。
    """
    cur = rsi_index()
    parts = cur.get("parts") or []
    if not parts:
        return {"ok": False, "error": "无可用指标"}
    # 拖后腿程度 = 权重 × (1 - 得分)
    worst = max(parts, key=lambda p: p["weight"] * (1 - p["value"]))
    advice = {
        "动作成功率": "挑最近失败最多的动作类型，看它的失败原因，先修掉重复出现的那个",
        "无幽灵率": "检查「声称改了但 diff 为空」的动作，把校验卡在提交前",
        "教训落地率": "把 pending 教训按 fix 逐条落到代码，并用探针复核",
        "静态质量": "先清 high 级静态问题，再谈别的",
    }.get(worst["name"], "先处理这一项")
    return {"ok": True, "name": worst["name"], "value": worst["value"],
            "weight": worst["weight"], "detail": worst.get("detail"),
            "drag": round(worst["weight"] * (1 - worst["value"]), 3),
            "advice": advice, "score": cur.get("score")}


def rsi_line() -> str:
    """一行 RSI 结论（写进评估报告 / 对话提示）。"""
    cur = rsi_index()
    w = rsi_weakest()
    t = rsi_trend()
    s = "RSI %.1f 分" % (cur.get("score") or 0)
    if t.get("n", 0) >= 4:
        s += "（%d 次采样，斜率 %+.2f/次，形态：%s）" % (
            t["n"], t.get("slope") or 0.0, t.get("shape"))
    else:
        s += "（历史采样 %d 条，还不够判趋势）" % t.get("n", 0)
    if w.get("ok"):
        s += "；最该修：%s（%.0f%%，拖 %s）" % (
            w["name"], (w.get("value") or 0) * 100, w.get("drag"))
    return s


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
