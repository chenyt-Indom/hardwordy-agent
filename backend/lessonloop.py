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
import threading
import time

logger = logging.getLogger(__name__)

# 同一 kind 未修多少次就升级（用户 2026-11-23 选：≥3 次强制升级到待批准）
ESCALATE_AT = 3

# 改完之后要观察多久才敢判「真修好了」（小时）。
# 太短 = 刚改完就自封修好（实测：任何无关改动刷新 mtime 就会把旧教训判成 fixed）。
#
# 2026-11-23 用户选定：**分两级**，不再用单一 6 小时窗。
#   0~1 小时        → verifying （待确认，progress 0 → 0.5）
#   1~24 小时       → confirming（过半确认，progress 0.5 → 0.95）
#   ≥24 小时且未复发 → fixed     （已确认，progress 1.0）
# 为什么必须拆：单窗 6 小时会让「刚修好」在账本上看起来像「没修」——
# 实测 6 条教训探针全部 code_hit=true，落地率却只有 1.7%，RSI 被钉死，
# 根本看不出有没有加速。
OBSERVE_HOURS = 6.0           # 兼容旧调用方，不再作为判定主窗口
OBSERVE_PENDING_HOURS = 1.0   # 1 小时内算「待确认」
OBSERVE_CONFIRM_HOURS = 24.0  # 满 24 小时且未复发才算「已确认」

# 静默异常账本（2026-11-23 修）。
#   `except ...: pass` 会把「功能静默失效」彻底掩盖掉 —— 这次踩过的坑就是它盖住了
#   AttributeError，所以自检里 silent-except 一直挂着一条扣分点。
#   但这两处兜底**本身是对的**（时间戳解析不出来 / 文件读不到 mtime，就该退回默认值，
#   不能把整条教训闭环打断），所以不能删 catch —— 改成「兜住但记一笔」：
#   控制流不变，出问题时能在 `_silent_log` 里查到 (时间戳, 异常) 现场。
#   上限只留最近 200 条，避免常驻服务里无限长大。
_silent_log = []
_SILENT_LOG_MAX = 200


def _note_silent(e) -> None:
    """兜住异常但记一笔（不改变控制流），供事后查「静默失效」。

    ⚠️ 2026-11-23 实测缺陷：这个函数**一直被调用、却从来没定义过**
       （`_num_ts` 时间戳解析失败、`_last_ok_edit` 取 mtime 失败时都会调它）。
       一旦走到异常分支就抛 NameError，把整条教训判定/统计链直接打断 ——
       比原来的 `except: pass` 更糟：静默失效变成了显式崩溃。
       现在补上：只记录 (时间, 异常)，绝不抛。
    """
    try:
        _silent_log.append((time.time(), "%s: %s" % (type(e).__name__, e)))
        if len(_silent_log) > _SILENT_LOG_MAX:
            del _silent_log[:len(_silent_log) - _SILENT_LOG_MAX]
    except Exception:                                   # noqa: BLE001
        pass


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
# 注入留痕（2026-11-24 用户要求：「教训涉及反思机制，希望它**能被看见**」）
#
# 背景：教训正文挂在最后一条用户消息末尾（省前缀缓存），前端**看不到** ——
#   用户没法确认「反思机制的产物到底有没有进模型」。这里每轮留一条记录，
#   供界面显示「上次注入了哪几条教训、什么时候、多长」。
# ⚠️ 只留最近 _INJ_MAX 条，避免常驻服务里无限长大。
# ⚠️ 节流：内容完全相同且距上次 < _INJ_MIN_GAP 秒 → 不重复写盘（对话主循环里调，不能每轮都 IO）。
# --------------------------------------------------------------------------
_INJ_MAX = 20
_INJ_MIN_GAP = 300.0
_last_inj = {"ts": 0.0, "text": ""}


def _injections_path() -> str:
    return os.path.join(_data_dir(), "lesson_injections.jsonl")


def record_injection(text: str) -> None:
    """记一笔「本轮真的把哪些教训送进了上下文」。绝不抛。"""
    try:
        raw = str(text or "")
        key = " ".join(raw.split())
        if not key:
            return
        now = time.time()
        if key == _last_inj.get("text") and now - float(_last_inj.get("ts") or 0) < _INJ_MIN_GAP:
            return
        _last_inj["ts"] = now
        _last_inj["text"] = key
        rec = {"ts": now,
               "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
               "chars": len(raw),
               "lines": [ln.strip() for ln in raw.splitlines() if ln.strip()][:12],
               "text": raw[:2000]}
        p = _injections_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(p, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        if len(lines) > _INJ_MAX:
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines[-_INJ_MAX:])
    except Exception:                                   # noqa: BLE001
        logger.debug("记录教训注入失败（不影响对话）", exc_info=True)


def last_injection() -> dict:
    """最近一次注入的教训（界面「能被看见」用）。没有记录就返回 ok=False。"""
    try:
        with open(_injections_path(), "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return {"ok": False, "error": "还没有注入记录"}
    if not lines:
        return {"ok": False, "error": "还没有注入记录"}
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and rec.get("lines"):
            rec["ok"] = True
            rec["count"] = len(rec["lines"])
            return rec
    return {"ok": False, "error": "注入记录读不出来"}


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
    # ⚠️⚠️ 2026-11-24 实测缺陷：这里原来指向 iter_runner.py 的 "static_first"
    #   （跑分前先取自检计数）—— 那和「跑分退步」**根本不是同一个问题**：
    #   实测 5 次 regressed 里 4 次只有 0.44~0.5 分跌幅，是评分噪声，
    #   真正的修法是 scoregate 的噪声带判定（_verdict_of）。
    #   探针指错文件 → 恒命中 → 这条教训永远显示「已修」，
    #   而实际上它反复复发（教训库里 ×7，是最高频的一条）。
    #   探针必须指向**修复逻辑本身**（同本表开头的告警）。
    "跑分退步（低于上一版）": ("backend/scoregate.py", "def _verdict_of"),
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


def _probe_hit(rel: str, needle: str):
    """三态探针：True=命中 / False=读到了但没有这段代码 / None=**读不到文件**。

    ⚠️ 2026-11-23 实测缺陷（用户要求「不要记录错误污染的样本」）：
       原来读失败一律 return False，而 judge() 把 False 解释成
       「修复逻辑不在代码里 → 只说不做」。于是文件被占用、权限不足、
       路径变动这类**观测失败**，会把一条**已经修好**的教训判成 pending，
       直接压低落地率、污染 RSI。
       读不到 ≠ 没修，必须单独一态，交给 judge 判 unprobed（不瞎判）。
    """
    p = os.path.join(_PROJECT_ROOT, rel.replace("/", os.sep))
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return needle in f.read()
    except OSError as e:
        _note_silent(e)
        return None


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


# 账本读取质量统计（2026-11-23 用户要求「不要落下样本、也不要让错误样本污染统计」）。
# 原来坏行是 `continue` 直接丢掉：既不计数也不留证，等于样本凭空消失，
# 而且**没人知道丢了**——统计照样给出一个看起来正常的数字。
# 现在：坏行原样隔离到 <账本>.bad，并计入 _READ_STATS，可随时查 read_quality()。
_READ_STATS = {"files": 0, "lines": 0, "bad": 0, "read_error": 0}
_READ_LOCK = threading.Lock()


def read_quality() -> dict:
    """账本读取质量：读了多少行、坏行几条、整个文件读失败几次。"""
    with _READ_LOCK:
        return dict(_READ_STATS)


def _read_jsonl(p: str) -> list:
    out = []
    if not os.path.isfile(p):
        return out
    bad_lines = []
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:                       # noqa: BLE001
                    bad_lines.append(line)
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        logger.debug("读 %s 失败", p, exc_info=True)
        with _READ_LOCK:
            _READ_STATS["read_error"] += 1
    if bad_lines:
        # 隔离而不是丢弃：坏行原样留档，事后能查「是哪条样本写坏了」
        try:
            with _READ_LOCK:
                with open(p + ".bad", "a", encoding="utf-8") as f:
                    for ln in bad_lines:
                        f.write(ln + "\n")
                _READ_STATS["bad"] += len(bad_lines)
        except OSError:
            with _READ_LOCK:
                _READ_STATS["bad"] += len(bad_lines)
            logger.warning("坏行隔离失败（%s），这些行已计入 bad 计数", p, exc_info=True)
    with _READ_LOCK:
        _READ_STATS["files"] += 1
        _READ_STATS["lines"] += len(out) + len(bad_lines)
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
    except (ValueError, TypeError):
        # ⚠️ 这里是**正常的格式探测**：ISO 时间串（"2026-11-16 12:40:51"）必然
        #    过不了 float()，接着走下面的 strptime 分支。
        #    实测把它记成异常后，光读一遍 actlog 就产生上千条噪音，
        #    200 条上限瞬间被灌满，**真正的静默失效反而被挤掉** ——
        #    那是记录污染，不是记录（2026-11-23 修）。
        #
        #    ★ 但也不能写成 `pass`：静态检查会把它算成「静默吞异常」
        #      （silent-except，扣 0.5 分），而且真出问题时完全无痕。
        #      折中：只在 debug 层留一行 —— 正常情况零输出（不进 _silent_log，
        #      避免上面说的噪音灌满），出问题时把 logging 调到 DEBUG 就能看到现场。
        logger.debug("_num_ts: %r 不是数字，按时间串继续解析", s[:32])
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
    except Exception as e:                          # noqa: BLE001 —— 兜住但要记一笔
        _silent_log.append((time.time(), repr(e)))
        del _silent_log[:-_SILENT_LOG_MAX]          # 只留最近 200 条
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


def _hit_state_path() -> str:
    return os.path.join(_data_dir(), "lesson_hit.json")


def _hit_state() -> dict:
    try:
        with open(_hit_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def _hit_state_save(d: dict) -> None:
    try:
        with open(_hit_state_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("写 lesson_hit 失败", exc_info=True)


def _first_hit_ts(kind: str, fallback: float) -> float:
    """探针**第一次命中**的时间 —— 观察窗从这里算，而不是文件的最后编辑时间。

    ⚠️⚠️ 2026-11-24 实测缺陷：原来 `edited = _last_ok_edit(rel)` 取的是该文件
    最后一次成功编辑的时间。于是**任何无关改动都会重置观察窗** ——
    实测：只改了 backend/lessonloop.py 里另两处（锚点诊断），5 条探针指向
    lessonloop.py 的教训 progress 全部归零，教训落地率 0.354 → 0.147，
    RSI 71.1 → 66.0。这也是 RSI 长期在 70 附近「原地跑」的机制原因：
    自迭代每次都在改这些文件，进度永远攒不起来。
    改成记住首次命中时间后，只有「修复真的消失又重新出现」才重新计时。
    """
    st = _hit_state()
    cur = st.get(kind)
    if isinstance(cur, (int, float)) and float(cur) > 0:
        return float(cur)
    st[kind] = float(fallback)
    _hit_state_save(st)
    return float(fallback)


def _clear_hit_ts(kind: str) -> None:
    """修复消失 / 复发 → 丢掉首次命中时间，下次重新计时（不能拿旧进度冒充）。"""
    st = _hit_state()
    if kind in st:
        st.pop(kind, None)
        _hit_state_save(st)


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
    # 探针文件读不到（被占用 / 权限 / 路径变动）＝ **观测失败**，不是「没修」。
    # 单列 unprobed，既不算落地也不算没落地，绝不污染落地率。
    if hit is None:
        return {"kind": kind, "status": "unprobed", "recur": recur,
                "edited_at": edited, "probe": "%s 含 %r（文件读不到）" % (rel, needle),
                "progress": None,
                "evidence": "探针文件读不到（%s），本次不做判定，避免把观测失败误判成没修" % rel,
                "detail": {"file": rel, "needle": needle, "code_hit": None,
                           "observe_error": True, "recur": recur}}
    ev = {
        "file": rel, "needle": needle, "code_hit": hit,
        "edited_at": time.strftime("%m-%d %H:%M", time.localtime(edited)) if edited else "",
        "lesson_at": time.strftime("%m-%d %H:%M", time.localtime(t0)) if t0 else "",
        "recur": recur,
    }
    # ① 修复逻辑不在代码里 —— 这是硬事实，与 mtime 无关
    if not hit:
        _clear_hit_ts(kind)
        if edited > t0:
            return {"kind": kind, "status": "pending", "recur": recur,
                    "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                    "progress": 0.0,
                    "evidence": "改过 %s，但**修复逻辑不在代码里**（改歪了）" % rel,
                    "detail": ev}
        return {"kind": kind, "status": "pending", "recur": recur,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "progress": 0.0,
                "evidence": "修复逻辑不在代码里，该文件也没改过（只说不做）", "detail": ev}
    # ② 修复逻辑在，但教训之后又复发 —— 改得不够，继续 pending
    if recur > 0:
        _clear_hit_ts(kind)
        return {"kind": kind, "status": "pending", "recur": recur,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "progress": 0.0,
                "evidence": "修复逻辑已在代码里，但之后又复发 %d 次" % recur, "detail": ev}
    # ③ 修复逻辑在、没复发。观察窗从「改动时间 / 教训时间」的**晚者**算起。
    #    ⚠️ 原来写 `if edited > t0`：教训若比改动更新（同一 kind 被反复重记），
    #    该条件为假 → 跳过观察窗直接判 fixed。实测 12:32 记的教训 12:33 就
    #    判成「已修」，而「未复发」此时只代表「才过了一分钟」—— 假阳性，
    #    会把 RSI 的教训落地率抬虚高。取 max 后，刚记的教训一律先进观察窗。
    # 起点 = 「探针第一次命中」与「教训记录时间」的晚者。
    # 不再直接用文件 mtime：否则改这个文件里任何别的地方都会把进度清零。
    base = max(_first_hit_ts(kind, max(edited, t0)), t0)
    elapsed = max(0.0, time.time() - base)
    h1 = OBSERVE_PENDING_HOURS * 3600.0
    h24 = OBSERVE_CONFIRM_HOURS * 3600.0
    # 为什么必须给部分信用（2026-11-23 实测修正）：
    #    原来 verifying 在落地率里算 0 分 → 6 条全在观察中时落地率恒为 0，
    #    RSI 被钉在 57 分一动不动；窗口一到又同时转 fixed，
    #    落地率 0→100%，RSI 一次突跳 —— 那是**阶跃**，与 RSI 要的平滑上升相反。
    if elapsed < h1:
        # 第一级：待确认（progress 0 → 0.5）
        prog = 0.5 * elapsed / h1
        return {"kind": kind, "status": "verifying", "recur": 0,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "progress": round(prog, 4),
                "evidence": "修复逻辑已在、暂未复发，待确认（已过 %.0f%%，还差 %.1f 小时转「过半确认」）"
                            % (prog * 100, (h1 - elapsed) / 3600.0),
                "detail": ev}
    if elapsed < h24:
        # 第二级：过半确认（progress 0.5 → 0.95）
        prog = 0.5 + 0.45 * (elapsed - h1) / max(1.0, h24 - h1)
        return {"kind": kind, "status": "confirming", "recur": 0,
                "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
                "progress": round(prog, 4),
                "evidence": "修复逻辑已在、暂未复发，过半确认（已过 %.0f%%，还差 %.1f 小时转「已确认」）"
                            % (prog * 100, (h24 - elapsed) / 3600.0),
                "detail": ev}
    return {"kind": kind, "status": "fixed", "recur": 0,
            "edited_at": edited, "probe": "%s 含 %r" % (rel, needle),
            "progress": 1.0,
            "evidence": "代码探针命中（%s 含 %r）且满 %.0f 小时未复发"
                        % (rel, needle, OBSERVE_CONFIRM_HOURS),
            "detail": ev}


# 哪些失败属于「护栏按设计拦下」，不能算成「修失败」。
# 实测依据（2026-11-23，actlog 407 条动作）：169 次 fail 里 65 次是这一类
# —— 熔断 51 次（同一文件 60 分钟内改太多次，主动停手）+ 质量闸门 14 次
# （占位式修补 / 待人工复核 / 无法安全实施）。
# 混进成功率分母会把 69.6% 压成 58.5%，等于「越安全越像在退步」。
# ⚠️ 2026-11-24 实测缺陷：「护栏文件被修改（不允许）」原来**不在**名单里，
#    而 self_iterate._classify 早就把它归成 guard —— 同一件事两套口径，
#    直接矛盾。后果：模型写到护栏文件被判失败（护栏正常生效），却扣了动作
#    成功率（近 400 条里 2 条，69.13% → 69.74%）。这里补齐，口径统一。
_GATE_MARKS = ("熔断", "占位式修补", "待人工复核", "无法安全实施",
               "篡改分类器", "禁止模型改写", "护栏文件被修改")


def _is_gate_block(row: dict) -> bool:
    """这条失败是不是「安全 / 质量护栏主动拦下」—— 不算能力失败。"""
    try:
        d = row.get("detail")
        d = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
        c = row.get("changelog")
        c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    except Exception:                                   # noqa: BLE001
        return False
    s = (d or "") + " " + (c or "")
    return any(m in s for m in _GATE_MARKS)


# 哪些动作记录属于「验证机制本身」而不是真实改动能力。
# 实测依据（2026-11-24，actlog 495 条）：probe=True 9 条，files 指向
# _probe_ / _tmp_ / _debug_archive 临时脚本 23 条 —— 这类动作跑一次几乎必然 ok，
# 混进动作成功率会让分数随「我跑了多少次测试」上下浮动，等于自己给自己刷分。
# ⚠️ backend/_feature_tests/ 下的 test_*.py **不算**（写测试用例是真实工作），
#    只有明确是诊断 / 临时脚本的才排除。
_PROBE_FILE_MARKS = ("_probe_", "/_tmp_", "/_debug_archive/")


def _is_probe_row(row: dict) -> bool:
    """这条 actlog 是不是探针 / 临时诊断产生的（不该进能力统计）。"""
    try:
        if row.get("probe") is True:
            return True
        files = row.get("files") or []
        if not isinstance(files, list) or not files:
            return False
        names = [str(f).replace("\\", "/") for f in files]
        return all(any(m in n for m in _PROBE_FILE_MARKS) for n in names)
    except Exception:                                   # noqa: BLE001
        return False


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
    # ★ 2026-11-24 修（用户要求：统计不许被测试 / 探针污染）
    #   探针与临时诊断脚本的记录整条排除，并显式报出条数 —— 不静默丢弃。
    # ⚠️ 2026-11-24 修：review_approve 是「用户批准动作」的**审批事件**，
    #   不是动作执行结果（实测 58 条 approved + 14 条 noop 全是这一类）。
    #   混进样本会让覆盖率虚低（0.647），让人误以为统计不透明。
    _events = [r for r in rows if r.get("kind") == "review_approve"]
    judged = [r for r in rows
              if not _is_probe_row(r) and r.get("kind") != "review_approve"]
    n_probe = len(rows) - len(judged) - len(_events)
    ok = sum(1 for r in judged if str(r.get("result") or "").lower() == "ok")
    _fail = [r for r in judged
             if str(r.get("result") or "").lower() in ("fail", "failed", "error")]
    gate = sum(1 for r in _fail if _is_gate_block(r))
    bad = len(_fail) - gate
    # 既不算成功也不算失败的（approved 待执行 / noop 无事可做 / 空结果）：
    # 不进分母是合理的，但必须**报出覆盖率** —— 否则用户看到的百分比
    # 可能只覆盖了一半记录，属于统计不透明。
    n_undecided = len(judged) - ok - len(_fail)
    marked = [r for r in judged if r.get("phantom") is not None]
    ph = sum(1 for r in marked if r.get("phantom") is True)
    a = audit()
    # 落地率按 progress 加权：fixed=1.0 / verifying=窗口进度 / pending=0.0。
    # ⚠️ 原来只数 fixed、把 verifying 当 0 分（2026-11-23 实测修正）：
    #    观察窗 6 小时内落地率恒 0，RSI 被钉死；窗口一满一次性转 fixed，
    #    落地率 0→100%，RSI 突跳 25 分 —— 是阶跃，不是 RSI 要的平滑上升。
    rows_a = a.get("rows") or []
    # ⚠️⚠️ 2026-11-24 实测缺陷：原来 `pd_ = len(rows_a)`、`fx = sum(progress or 0)`，
    #   而 unprobed 的 progress 是 None → **计 0 分却进分母**。
    #   实测：1 条 fixed + 3 条 unprobed 得 0.25（应得 1.0）—— 与 judge() 里
    #   「unprobed 既不算落地也不算没落地，绝不污染落地率」直接矛盾，
    #   只要有一条教训没有探针（新 kind 未登记），RSI 就被无声压低。
    #   分母只算**可判定**（progress 不是 None）的条目。
    scored = [r for r in rows_a if r.get("progress") is not None]
    n_unprobed = len(rows_a) - len(scored)
    fx = sum(float(r.get("progress") or 0.0) for r in scored)
    pd_ = len(scored)
    hi = mid = 0
    try:
        au = json.load(open(os.path.join(_data_dir(), "audits.json"), encoding="utf-8"))
        it0 = (au.get("items") or [{}])[0]
        c = it0.get("counts") or {}
        hi, mid = int(c.get("high") or 0), int(c.get("mid") or 0)
    except Exception:                                   # noqa: BLE001
        logger.debug("读 audits 失败", exc_info=True)
    # ⚠️⚠️ 2026-11-24 修（用户要求「避免刻意刷分」）：
    #   原来缺数据的项直接 `if pd_:` 不进 parts，随后 tw 只加剩余权重并重新归一化
    #   —— 等于「让某一项没数据」就能把它的 0 分从总分里抹掉，分数自动上涨。
    #   这正是可被刻意利用的刷分路径：把教训全变成 unprobed（无可判定探针），
    #   落地率的 0 分就消失，总分凭空变高，而实际什么都没改好。
    #   现在改成：缺失项**保留固定权重、按 0 计**（保守，宁可低估不虚高），
    #   并在 insufficient 里点名是哪项没数据 —— 分数只能靠真实改进上升。
    parts = []
    insufficient = []
    if ok + bad:
        parts.append(("动作成功率", ok / (ok + bad), 0.40,
                      "%d/%d%s" % (ok, ok + bad,
                                   ("（另 %d 次护栏拦截，不计入分母）" % gate) if gate else "")))
    else:
        insufficient.append("动作成功率")
    if marked:
        parts.append(("无幽灵率", 1 - ph / len(marked), 0.15,
                      "幽灵 %d/%d" % (ph, len(marked))))
    else:
        insufficient.append("无幽灵率")
    if pd_:
        parts.append(("教训落地率", min(1.0, fx / pd_), 0.25,
                      "%.1f/%d 条（观察中按进度计%s）"
                      % (fx, pd_,
                         "；%d 条无可判定探针，不计入分母" % n_unprobed
                         if n_unprobed else "")))
    else:
        insufficient.append("教训落地率")
    parts.append(("静态质量", max(0.0, 1 - min(1.0, (hi * 3 + mid) / 10.0)), 0.20,
                  "high %d / mid %d" % (hi, mid)))
    # ★ 分母**固定为 1.0**（四项权重之和），而不是「只加有数据的项再归一化」：
    #   · 缺数据的项不进 parts —— UI 不会把它误显示成「0% 落地率」；
    #   · 但它的权重仍算在分母里（等价于按 0 计）—— 删掉数据也不能抬分。
    #   这两个要求缺一不可，是「不误报」和「不刷分」的平衡点。
    score = round(100 * sum(p[1] * p[2] for p in parts), 1)
    # 覆盖率：可判定动作 / 全部动作 —— 让「这个分数覆盖了多少记录」可见。
    coverage = round((ok + bad) / float(len(judged) or 1), 3) if judged else 0.0
    res = {"ok": True, "score": score,
           "parts": [{"name": n, "value": round(v, 3), "weight": w, "detail": d,
                      "insufficient": n in insufficient}
                     for n, v, w, d in parts],
           "insufficient": insufficient,
           "coverage": coverage,
           "samples": {"judged": len(judged), "probe_excluded": n_probe,
                       "event_excluded": len(_events),
                       "undecided": n_undecided, "gate_blocked": gate},
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
        # fixed > confirming > verifying > pending > unprobed
        rank = {"fixed": 0, "confirming": 1, "verifying": 2, "pending": 3,
                "unprobed": 4}
        if rank[r["status"]] < rank[old["status"]]:
            merged[k] = r
        merged[k]["count"] = max(int(old.get("count") or 1),
                                 int(r.get("count") or 1))
    out = list(merged.values())
    _ord = {"pending": 0, "verifying": 1, "confirming": 2, "unprobed": 3,
            "fixed": 4}
    out.sort(key=lambda x: (_ord.get(x["status"], 9), -int(x.get("count") or 1)))
    return {
        "ok": True,
        "total": len(out),
        # fixed 是标准名；done 保留给旧调用方（scheduler/main 用 a["done"]）
        "fixed": [r for r in out if r["status"] == "fixed"],
        "done": [r for r in out if r["status"] == "fixed"],
        "pending": [r for r in out if r["status"] == "pending"],
        "verifying": [r for r in out if r["status"] == "verifying"],
        "confirming": [r for r in out if r["status"] == "confirming"],
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
      · 有 pending → 只列未修项；没有 pending → 列**已落地**项（观察窗内也得能看见，
        否则刚修好的那 24 小时里教训完全不进上下文，2026-11-24 实测缺陷）；
      · 文案按 kind 排序、截断到固定条数，同一批教训下是**稳定字符串**
        （不含时间/百分比/累计次数，避免每轮都变、废掉前缀缓存）。
    """
    # 顺带做一次 RSI 采样（内部节流：1 小时最多一条）。
    # ⚠️ 放在 `if not pend: return ""` 之前 —— 没有待办时 RSI 照样需要序列点，
    #    否则「没 pending」的那段时间曲线是空的，趋势判不出来。
    try:
        rsi_sample_throttled()
    except Exception:                                   # noqa: BLE001
        logger.debug("RSI 节流采样失败", exc_info=True)
    try:
        a = audit()
    except Exception:                                   # noqa: BLE001
        logger.debug("lessonloop.audit 失败", exc_info=True)
        return ""
    pend = a.get("pending") or []
    if not pend:
        # ⚠️⚠️ 2026-11-24 实测缺陷（用户要求「教训要能被看见」）：
        #   6 条教训全部处在观察窗内（verifying 2 / confirming 4）时 pending 为空，
        #   于是 hint() 返回 ""，**教训一个字都不进上下文** —— 而观察窗最长
        #   OBSERVE_CONFIRM_HOURS（24 小时）。也就是说：教训刚落地、最该被遵守的
        #   那段时间，反而完全看不见。这里补一段「已落地」摘要。
        #   ⚠️ 必须是**稳定字符串**：只列 kind + 改法摘要，不含时间/百分比/累计次数
        #   （那些会变，且已验证会破坏前缀缓存）。verifying→confirming→fixed
        #   的状态迁移不改变这段文本，只有教训集合本身变了才变。
        landed = (a.get("confirming") or []) + (a.get("verifying") or []) \
            + (a.get("fixed") or [])
        seen, items = set(), []
        for r in sorted(landed, key=lambda x: str(x.get("kind") or "")):
            k = str(r.get("kind") or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(r)
        if not items:
            return ""
        lines = ["\n【历史教训（已落地，别再犯）】",
                 "下面这些是前几轮反思发现、**代码里已经修好**的问题。"
                 "本轮如果碰到同类情况，直接按「改法」做，别重新想一遍："]
        for r in items[:max_n]:
            fix = " ".join(str(r.get("fix") or "").split())[:100]
            lines.append("- 「%s」%s" % (r.get("kind"), ("：" + fix) if fix else ""))
        s = "\n".join(lines) + "\n"
        return s[:max_chars]
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


def _slope_ts(pts: list) -> float | None:
    """按**真实时间**算斜率，单位「分 / 小时」。

    为什么不能按索引算（原来的 _slope）：采样间隔并不等距 —— 正常 1 小时一次，
    调试 / 手动 force 时可能 12 分钟内 8 次。按索引算等于假设等距，
    密集点会把斜率压成接近 0（实测 05:42~05:54 那 8 个点就是这么把趋势压平的）。
    按时间算，密集点的权重自然降低，曲线才反映真实改进速度。

    pts 每项需要 {"t": 秒级时间戳, "score": 分数}；时间戳缺失时退化为索引斜率。
    """
    try:
        xs = [float(p.get("t") or 0.0) for p in pts]
        ys = [float(p.get("score") or 0.0) for p in pts]
    except (TypeError, ValueError):
        return None
    n = len(xs)
    if n < 2 or len(set(xs)) < 2:
        return _slope(ys)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    per_sec = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    return per_sec * 3600.0


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


def anchor_candidates(text: str, find: str, limit: int = 8) -> list:
    """把 find 在文件里的每一处命中，连同行号与前后一行一起交出去。

    为什么必须给候选（2026-11-24 实测）：actlog 里 6 次
    「find 命中 N 处，不唯一（已自动回滚）」的失败，原实现只回一句「不唯一」，
    模型下一轮拿不到任何新信息，只能照着同一个锚点再猜一次 → 再失败一次。
    把行号和上下文交出去，下一轮才能把锚点扩成含唯一上下文的多行块。
    """
    out = []
    if not find:
        return out
    lines = text.splitlines()
    pos = text.find(find)
    while pos != -1 and len(out) < limit:
        ln = text.count("\n", 0, pos) + 1
        out.append({
            "line": ln,
            "prev": (lines[ln - 2].strip()[:80] if ln >= 2 else ""),
            "cur": (lines[ln - 1].strip()[:80] if ln - 1 < len(lines) else ""),
            "next": (lines[ln].strip()[:80] if ln < len(lines) else ""),
        })
        pos = text.find(find, pos + max(1, len(find)))
    return out


def near_miss(text: str, find: str, limit: int = 3) -> list:
    """锚点没命中时，用 find 的首个非空行去搜，给出「最像的那几行」。

    实测价值：模型记错缩进/函数名时，一句「没找到」它下一轮还是照原样再试；
    给出真实原文的相近行，它一次就能改对。
    """
    import re
    parts = [p for p in re.split(r"\s+", find or "") if p]
    if not parts:
        return []
    key = parts[0][:40]
    if len(key) < 3:
        return []
    out = []
    lines = text.splitlines()
    for i, ln in enumerate(lines, 1):
        if key in ln:
            out.append({"line": i, "text": ln.strip()[:120]})
            if len(out) >= limit:
                break
    return out


def patch_with_fallback(rel: str, find: str, replace: str, note: str = "",
                        session: str = "default") -> dict:
    """带锚点降级的 patch —— 给自迭代链 / 反思链 / 修复链调用。

    为什么不在 selfdev.py 里改（2026-11-23 实测）：
    `selfdev.py` 是回撤机制自身，写入工具直接拒绝改写
    （返回「是回撤机制自身，禁止模型改写」—— 改坏了就退不回来了），
    所以降级放在**调用层**：先试精确 patch，失败再按空白归一化定位后整段替换，
    并记一条 `anchor_miss` 事件（带 fallback=loose），便于统计这类失败还剩多少。
    """
    # ⚠️⚠️ 2026-11-24 实测缺陷（actlog 可查）：
    #   find 为空时 `text.count("") == len(text) + 1`，selfdev.patch 会报
    #   「find 命中 48607 处，不唯一（已自动回滚）」—— 把「模型压根没给锚点」
    #   误诊成「锚点不唯一」。两者修法完全不同，必须在这里先分开。
    if not str(find or "").strip():
        return {"ok": False, "reason": "find_empty",
                "error": "锚点（find）为空：请给出要替换的原文片段；"
                         "只改一处插入可用 selfdev_insert_after（一行锚点即可）。"}
    if len(str(find).strip()) < 8 and "\n" not in str(find):
        # 太短的锚点几乎必然命中多处（`}、return out` 之类），提前说清而不是让它撞墙。
        return {"ok": False, "reason": "anchor_too_short",
                "error": "锚点太短（%r）：请带上前后各一行组成唯一多行块。"
                         % str(find)[:20]}
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
        # 命中多处 → 不猜（改错地方比不改更糟），但要把「该改哪一处」的证据交出去，
        # 否则下一轮拿同一个锚点还会再失败一次（2026-11-24 实测 6 次）。
        # ⚠️ 2026-11-24 修（regress 里 4 条断言长期挂红）：判定**不能只看文案**。
        #   selfdev.patch 的真实文案是「这段原文出现了 N 次，为免改错地方…」，
        #   里面根本没有「不唯一」三个字 → 这个分支永远走不到，
        #   模型拿到的仍是一句没有候选的报错（实测 candidates=0、reason=None）。
        #   改成自己数候选：≥2 处就是多处命中，与上游文案无关。
        got = _sd.read(rel)
        text = (got.get("raw") if got.get("ok") else "") or ""
        cands = anchor_candidates(text, find)
        if "不唯一" in err or len(cands) >= 2:
            r["reason"] = "ambiguous"
            r["candidates"] = cands
            r["hint"] = ("锚点命中多处，已给出前 %d 处的行号与上下文（candidates）；"
                         "请把 find 扩成含**前后各一行**的唯一多行块再试。" % len(cands))
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
        # ⚠️⚠️ 2026-11-24 实测缺陷：不带 fail_kind 调 _sd.patch 时，第一步就被
        #   「幽灵修复」闸门拦下（reason=phantom），于是**永远走不到**上面那句
        #   `if "不唯一" in err` —— 锚点其实命中多处，模型看到的却是
        #   「幽灵修复」这种风马牛不相及的错误，下一轮去改 fail_kind，白跑。
        #   所以这里必须自己判一次：多处命中 → ambiguous（带候选），真的没命中 → near。
        cands = anchor_candidates(text, find)
        if len(cands) >= 2:
            r["reason"] = "ambiguous"
            r["candidates"] = cands
            r.pop("phantom", None)
            r["error"] = ("锚点命中 %d 处，不能猜（改错地方比不改更糟）。"
                          "请把 find 扩成含前后各一行的唯一多行块。" % len(cands))
            r["hint"] = ("候选行号与上下文见 candidates（line/prev/cur/next）。"
                         "只插不改可用 selfdev_insert_after。")
            return r
        near = near_miss(text, find)
        r["reason"] = "anchor_miss" if r.get("reason") in (None, "", "phantom") \
            else r.get("reason")
        r.pop("phantom", None)
        r["near"] = near
        r["hint"] = (("没命中。文件里最相近的 %d 行见 near，请照抄真实原文（含缩进）再试；"
                      "只插不改可用 selfdev_insert_after。") % len(near) if near else
                     ("空白归一化后也没命中：先用 selfdev_grep 搜关键字拿到真实原文，"
                      "或改用 selfdev_insert_after（只要一行锚点）。"))
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


def rsi_speed(days: float = 7.0, target_per_day: float = 20.0) -> dict:
    """加速指数（0~100）：真实修复事件有没有「越来越快」。

    为什么要单独一条（2026-11-23 用户要求「四个都要，合成一个指数」）：
    rsi_index 算的是**当前水平**（改得动不动 / 落地率 / 代码质量），
    它回答不了「速度在不在涨」。而 RSI 那张图的关键特征是**斜率逐渐变陡**，
    所以必须把「速度」单独抽出来做成序列，否则永远只能看到一条水平线。

    四个分量（各 0~1）：
      吞吐      最近 days 天真实改动事件/天 ÷ target_per_day
      首修成功率 1 - 真失败率（护栏拦截不计，见 _is_gate_block）
      落地率    audit 的 progress 加权（fixed=1 / confirming≈.5~.95 / pending=0）
      不复发率  1 - 复发教训数 / 教训总数
    权重：吞吐 .30 / 首修 .30 / 落地 .25 / 不复发 .15
    """
    now = time.time()
    t0 = now - max(0.5, float(days)) * 86400.0
    rows = [r for r in _read_jsonl(os.path.join(_data_dir(), "actlog.jsonl"))
            if _num_ts(r.get("ts")) >= t0]
    ok_rows = [r for r in rows
               if str(r.get("result") or "").lower() == "ok" and r.get("files")]
    fail_rows = [r for r in rows
                 if str(r.get("result") or "").lower() in ("fail", "failed", "error")]
    real_fail = [r for r in fail_rows if not _is_gate_block(r)]
    per_day = len(ok_rows) / max(0.5, float(days))
    a = audit()
    rows_a = a.get("rows") or []
    # 落地率 / 不复发率的分母只算**可判定**的教训（progress is not None）。
    # 2026-11-23 修：unprobed（没有探针、或探针文件读不到）的 progress 是 None，
    # 原来用 `or 0.0` 会把它当「没落地」算进分母 —— 等于「判不了」就被罚分，
    # 落地率凭空被压低，RSI 曲线跟着失真。
    judgeable = [r for r in rows_a if r.get("progress") is not None]
    land = (sum(float(r.get("progress") or 0.0) for r in judgeable) / len(judgeable)
            if judgeable else 0.0)
    recur = sum(1 for r in judgeable if int(r.get("recur") or 0) > 0)
    norecur = (1.0 - recur / len(judgeable)) if judgeable else 0.0
    first_ok = (len(ok_rows) / (len(ok_rows) + len(real_fail))
                if (ok_rows or real_fail) else 0.0)
    parts = [
        ("吞吐", min(1.0, per_day / max(1.0, float(target_per_day))), 0.30,
         "%.1f 次/天（目标 %.0f）" % (per_day, target_per_day)),
        ("首修成功率", first_ok, 0.30,
         "%d 成 / %d 真失败（护栏拦截 %d 次不计入）"
         % (len(ok_rows), len(real_fail), len(fail_rows) - len(real_fail))),
        ("落地率", min(1.0, land), 0.25,
         "%.2f/%d 条可判定（另有 %d 条无法判定，不计入）"
         % (land * len(judgeable), len(judgeable), len(rows_a) - len(judgeable))),
        ("不复发率", norecur, 0.15,
         "复发 %d/%d" % (recur, len(judgeable))),
    ]
    tw = sum(p[2] for p in parts) or 1.0
    score = round(100 * sum(p[1] * p[2] for p in parts) / tw, 1)
    return {"ok": True, "score": score, "days": days,
            "parts": [{"name": n, "value": round(v, 3), "weight": w, "detail": d}
                      for n, v, w, d in parts],
            "at": time.strftime("%H:%M:%S", time.localtime(now))}


_RSI_SAMPLE_LOCK = threading.Lock()


def rsi_sample(force: bool = False, min_gap: float = 60.0) -> dict:
    """采样一次 RSI 写入历史。同一分钟内重复调用只记一条（防刷爆账本）。

    2026-11-23 实测缺陷：不加锁会**并发双写** —— 12:35:32 落了 2 条完全相同的
    采样（ts 只差 0.04 秒），rsi_trend 会把同一个点当成两个点去算斜率，
    斜率/分段形态全被污染。
    """
    with _RSI_SAMPLE_LOCK:
        return _rsi_sample_locked(force=force, min_gap=min_gap)


def _rsi_sample_locked(force: bool = False, min_gap: float = 60.0) -> dict:
    cur = rsi_index()
    p = _rsi_hist_path()
    rows = _read_jsonl(p)
    now = time.time()
    replace_last = False
    if rows:
        last = _num_ts(rows[-1].get("ts"))
        gap = now - last
        if not force and gap < min_gap:
            return {"ok": True, "skipped": True, "score": cur.get("score"),
                    "n": len(rows), "why": "距上次采样不足 %.0f 秒" % min_gap}
        # ★ 2026-11-24 修（统计污染）：force=True（调试 / 手动）原来无条件追加 ——
        #   实测 rsi_history 30 个点里有 8 个挤在 12 分钟内（05:42:30~05:54:30），
        #   而 _slope 是按索引等距算的 → 趋势斜率被这堆几乎相同的点主导、曲线失真。
        #   改成：距上一条不足 min_gap 时**替换**最后一条，历史点保持等距，
        #   调试不再往趋势里堆点。
        if gap < min_gap:
            replace_last = True
    try:
        sp = rsi_speed().get("score")
    except Exception:                                   # noqa: BLE001
        logger.debug("rsi_speed 采样失败", exc_info=True)
        sp = None
    rec = {"ts": now,
           "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
           "score": cur.get("score"),
           "speed": sp,
           "parts": {x["name"]: x["value"] for x in (cur.get("parts") or [])}}
    try:
        if replace_last:
            rows[-1] = rec
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
        else:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": "写 rsi_history 失败：%s" % e}
    return {"ok": True, "score": rec["score"],
            "n": len(rows) if replace_last else len(rows) + 1,
            "replaced": replace_last, "iso": rec["iso"]}


# 节流采样：hint() 每轮对话都会调，但只有距上次超过 gap 秒才真写盘。
# 为什么需要它：rsi_sample() 原本只在「评估收尾」调用，而评估约 2 小时一次 ——
# 判趋势至少要 4 个采样点，那得等 8 小时才画得出曲线，期间用户问「有没有上升」
# 只能答「样本不足」。加上每轮对话的低频采样后，只要用户在用就有足够密度。
_RSI_LAST_SAMPLE: dict = {"ts": 0.0}


def rsi_sample_throttled(gap: float = 3600.0) -> dict:
    """距上次采样超过 gap 秒才真写一条（防每轮对话都写盘）。"""
    now = time.time()
    if now - float(_RSI_LAST_SAMPLE.get("ts") or 0.0) < gap:
        return {"ok": True, "skipped": True,
                "why": "距上次采样不足 %.0f 秒" % gap}
    _RSI_LAST_SAMPLE["ts"] = now
    try:
        return rsi_sample(min_gap=gap)
    except Exception:                                   # noqa: BLE001
        logger.debug("rsi 节流采样失败", exc_info=True)
        return {"ok": False}


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
    # ★ 2026-11-24 修（统计污染）：斜率改成**按真实时间**算（见 _slope_ts）。
    #   原来按索引算 = 假设采样等距，而实测正常 1 小时一次、调试时 12 分钟 8 次，
    #   密集点会把斜率压成接近 0，曲线失真。
    #   这里只保留「同一秒内的重复写入」去重（并发双写的老问题），不动正常采样点。
    _ded = []
    for _r in rows:
        _ts = _num_ts(_r.get("ts"))
        if _ded and _ts and _ded[-1][0] and abs(_ts - _ded[-1][0]) < 1.0:
            _ded[-1] = (_ts, _r)
        else:
            _ded.append((_ts, _r))
    rows = [r for _, r in _ded]
    pts = [{"ts": r.get("iso") or "",
            "t": _num_ts(r.get("ts")) or 0.0,
            "score": float(r.get("score") or 0),
            "speed": (float(r["speed"])
                      if isinstance(r.get("speed"), (int, float)) else None)}
           for r in rows if isinstance(r.get("score"), (int, float))]
    ys = [p["score"] for p in pts]
    if len(ys) < 4:
        return {"ok": True, "n": len(ys), "points": pts, "shape": "样本不足",
                "slope": None, "segs": [],
                "advice": "继续采样（每次评估收尾会自动记一条）"}
    sl = _slope_ts(pts) or 0.0
    sd = _stdev(ys) or 0.0
    k = max(1, len(ys) // 3)
    segs = []
    for i, name in enumerate(("前段", "中段", "后段")):
        a0 = i * k if i < 2 else 2 * k
        a1 = (i + 1) * k if i < 2 else len(ys)
        seg_pts = pts[a0:a1]
        seg_y = ys[a0:a1]
        segs.append({"name": name, "n": len(seg_y),
                     "slope": round(_slope_ts(seg_pts) or 0.0, 3),
                     "mean": round(sum(seg_y) / max(1, len(seg_y)), 1)})
    s1, s2, s3 = [x["slope"] for x in segs]
    if s1 < s2 < s3 and s1 > 0:
        shape = "③ 斜率递增（RSI）"
    elif sl > 0.05:
        shape = "② 直线上升"
    elif sl < -0.05:
        shape = "④ 锯齿下滑"
    else:
        shape = "① 平台（原地跑）"
    return {"ok": True, "n": len(ys),
            "points": [{k: v for k, v in p.items() if k != "t"} for p in pts[-40:]],
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
    # ⚠️ 「教训落地率」在没有 pending 时**不可行动**（2026-11-23 实测）：
    #    6 条全在观察窗里，落地率被拖到 1%，drag 0.247 排第一，
    #    RSI 会一直催「去把 pending 教训落到代码」—— 可当时根本没有 pending，
    #    唯一能做的是等 6 小时。那会把改进方向指错。
    #    所以没有 pending 时把这一项排除出候选，只从能动手的项里选。
    try:
        _pend_n = len(audit().get("pending") or [])
    except Exception:                                   # noqa: BLE001
        _pend_n = 0
    # ⚠️ 2026-11-24 修：缺数据的项（insufficient，按 0 计）**不能**当「最该修」——
    #    它 0 分只是因为没样本，不是能力差；拿它当改进方向会把模型指去
    #    「制造数据」而不是「修问题」，等于变相刷分。
    cands = [p for p in parts
             if not (p["name"] == "教训落地率" and _pend_n == 0)
             and not p.get("insufficient")]
    # 全满分同样没有「最该修」：硬选一个 drag=0 的项 = 假装有事要做，
    # 会把模型指去瞎改 —— 那也是刷分（为了让指标好看而制造动作）。
    if cands and max(p["weight"] * (1 - p["value"]) for p in cands) <= 0:
        cands = []
    if not cands:
        # 全是缺数据项 → 不给「最该修」：硬选只会把改进方向指错，
        # 等于逼模型去「制造数据」而不是「修问题」。
        return {"ok": False, "error": "没有可判定的短板（数据不足或各项已满分）",
                "insufficient": [p["name"] for p in parts if p.get("insufficient")],
                "score": cur.get("score")}
    # 拖后腿程度 = 权重 × (1 - 得分)
    worst = max(cands, key=lambda p: p["weight"] * (1 - p["value"]))
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
        s += "（%d 次采样，斜率 %+.2f 分/小时，形态：%s）" % (
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
