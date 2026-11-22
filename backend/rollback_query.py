# -*- coding: utf-8 -*-
"""回滚专用查证路径：`query_rollback(ts_hint)` —— 只读地回答「上次回滚成功了吗」。

用户原话（2026-11-18）：
    「问『上次回滚成功了吗』，首行直接给成功 / 失败 / 无记录。」

为什么不写进 backend/selfdev.py
-------------------------------
`backend/selfdev.py` 是**回撤机制自己**，被划成只读（见 selfdev.PROTECTED，
以及 tools.py 里 `_sd_fix.patch = _sd_patch_fixed` 的注释：selfdev.py 只读、
扩展放外面）。理由很硬：回撤能力一旦被改坏，用户就再也退不回去了。
所以这里照同样的办法做 —— 本模块 import 时把 `query_rollback` 挂到
`selfdev.query_rollback`，行为上等同「selfdev 多了一个函数」，
但一个字都没改 selfdev.py。

事实只有两处，其它都是猜
------------------------
  1. **journal**：data/selfdev_journal.jsonl
        · act="rollback"        单文件回滚，rel 是那个文件，from_file 用的哪个备份；
        · act="rollback_batch"  整批回撤，rel 是批次号，rels 是还原成功的文件，
                                failed 是失败个数，failed_files 是失败清单；
        · act="rollback_failed" 回滚没做成的记录（备份找不到 / 复制失败）；
        · 不针对具体文件的动作（toggle / restart / fullaccess 那类）rel 记成 '-'，
          同样算 journal 记录，只是「涉及文件」为空。
  2. **git**（backend/version.py）：
        · 回滚前自动建的「还原点」提交 —— hash 记在 data/version_undo.json；
        · 每次回滚都会提交一条「回滚到 xxx（…）」，这是回滚真落过地的硬证据。

用法
----
    from . import rollback_query as rq
    r = rq.query_rollback("上次")        # 最近一次
    r = rq.query_rollback("2026-11-18")  # 那天的
    r["status"]   # "success" / "failed" / "none"
    r["line"]     # 首行可直接给用户看的结论（含时间 + 涉及文件）
"""
from __future__ import annotations

import json
import logging

try:
    from . import selfdev
    from . import config
except ImportError:                     # 允许作为顶层模块被直接导入（如验收断言）
    import selfdev                      # type: ignore
    import config                       # type: ignore

logger = logging.getLogger(__name__)

# 回滚类 act（成功侧 / 失败侧）。act 里含 rollback / revert 的一律也算。
_OK_ACTS = ("rollback", "rollback_batch", "rollback_batch_partial", "revert",
            "undo", "restore")
_BAD_ACTS = ("rollback_failed", "rollback_error", "rollback_rejected",
             "rollback_backup_missing", "rollback_batch_failed")

# 用户提问里的口语词：算 ts_hint 时先剔掉；剔完为空 = 「不按时间筛，只看最近一次」
_TS_HINT_NOISE = ("上一次", "上一回", "上次", "最近一次", "最近", "刚才", "刚刚",
                  "那个时候", "那一次", "那次", "回滚", "撤销", "还原", "回退",
                  "成功了吗", "成功了", "成功", "失败", "结果", "记录", "查一下",
                  "查查", "看看", "是否", "有没有", "一下", "到底",
                  "的", "了", "吗", "呢", "呀", "吧", "啊",
                  "？", "?", "，", ",", "。", "、", "：", ":", " ", "\t", "\n")


# ---------------------------------------------------------------- 读 journal
def _journal_path() -> str:
    """journal 的落盘位置（优先问 selfdev，拿不到就按 config 兜底）。"""
    try:
        p = selfdev._journal_path()
        if p:
            return str(p)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        return str(config.data("data", "selfdev_journal.jsonl"))
    except Exception:
        return "data/selfdev_journal.jsonl"


def _read_journal_all(limit: int = 5000) -> list:
    """读 journal **全量** —— 回滚可能发生在很久以前，recent_journal(30) 看不到。"""
    out = []
    try:
        with open(_journal_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception:
        return []
    return out[-limit:] if limit > 0 else out


# ---------------------------------------------------------------- 时间线索
def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _ts_hint_core(hint) -> str:
    """剔掉口语词，留下日期/时间线索。空串 = 没给具体时间。"""
    h = str(hint or "").strip()
    for w in _TS_HINT_NOISE:
        h = h.replace(w, "")
    return h.strip(" -_/.~～")


def _ts_match(ts, hint) -> bool:
    """ts 是否匹配用户给的时间线索（空 hint → 全匹配）。"""
    ts = str(ts or "")
    raw = str(hint or "").strip()
    if not raw:
        return True
    if raw in ts:                        # 原样子串："2026-11-18" / "10:22"
        return True
    core = _ts_hint_core(raw)
    if not core:                         # 只有口语词 → 不筛
        return True
    if core in ts:
        return True
    dh, dt = _digits(core), _digits(ts)
    return bool(dh) and dh in dt          # "20261118" / "1118" 这类纯日期


# ---------------------------------------------------------------- 解析记录
def _rec_is_rollback(rec: dict) -> bool:
    act = str(rec.get("act") or "").strip().lower()
    if not act:
        return False
    if act in _OK_ACTS or act in _BAD_ACTS:
        return True
    return ("rollback" in act) or ("revert" in act)


def _rec_failed(rec: dict) -> bool:
    """这条回滚记录算成功还是失败（宁严勿松：可疑一律按失败说清）。"""
    for k in ("ok", "success"):
        if isinstance(rec.get(k), bool):
            return not rec[k]
    for k in ("err", "error", "why"):
        if str(rec.get(k) or "").strip():
            return True
    try:
        if int(rec.get("failed") or 0) > 0:
            return True
    except (TypeError, ValueError):                         # noqa: BLE001 非数字属正常，下面还有别的判据
        logger.debug("failed 字段不是数字，跳过这条判据：%r", rec.get("failed"))
    act = str(rec.get("act") or "").strip().lower()
    return act in _BAD_ACTS or "fail" in act or "reject" in act


def _backups_of_batch(batch) -> list:
    """批次号 → 该批次涉及的源码文件（从备份清单还原）。"""
    out = []
    try:
        for b in selfdev.list_backups(600):
            if str(b.get("batch") or "") == str(batch or ""):
                rel = str(b.get("rel") or "")
                if rel and rel not in out:
                    out.append(rel)
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return out


def _rec_files(rec: dict) -> list:
    """这条回滚记录「涉及哪些文件」（多来源合并、去重保序）。"""
    out = []

    def _add(x):
        x = str(x or "").strip()
        if x and x != "-" and x not in out:
            out.append(x)

    rels = rec.get("rels")
    if isinstance(rels, list):
        for x in rels:
            _add(x)
    bad = rec.get("failed_files")
    if isinstance(bad, list):
        for x in bad:
            _add(x)                       # 失败的也算「涉及」
    fs = rec.get("files")
    if isinstance(fs, list):
        for x in fs:
            _add(x)
    rel = str(rec.get("rel") or "").strip()
    if rel and rel != "-":
        act = str(rec.get("act") or "").lower()
        if "batch" in act:
            for x in _backups_of_batch(rel):
                _add(x)
            if not out:
                _add(rel)                 # 备份已被清理 → 至少把批次号给他
        else:
            _add(rel)
    return out[:30]


# ---------------------------------------------------------------- 读 git
def _ver_mod():
    """拿到 version 模块（拿不到返回 None，绝不抛）。"""
    try:
        from . import version as _v
        return _v
    except ImportError:
        try:
            import version as _v           # type: ignore
            return _v
        except Exception:
            return None


def _git() -> dict:
    """git 回滚痕迹：还原点（undo 记录）+ 最近的「回滚到 …」提交。"""
    out = {"ok": False, "commits": [], "undo": {}, "error": ""}
    _ver = _ver_mod()
    if _ver is None:
        out["error"] = "version 模块没加载"
        return out
    try:
        u = _ver.get_undo()
        if u.get("ok"):
            out["undo"] = u.get("undo") or {}
    except Exception:
        logger.debug("silent-except", exc_info=True)
    try:
        ok, txt = _ver._run(["log", "-n", "40",
                             "--date=format:%Y-%m-%d %H:%M:%S",
                             "--pretty=format:%h\t%ad\t%s"])
    except Exception:
        return out
    if not ok:
        out["error"] = str(txt or "")[:200]
        return out
    seq = []
    for line in str(txt or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        subj = parts[2]
        if "回滚到" in subj or "还原点" in subj:
            seq.append({"hash": parts[0], "time": parts[1], "subject": subj[:120]})
    out["ok"] = True
    out["commits"] = seq[:5]
    return out


def _git_commit_files(h) -> list:
    """某个提交改了哪些文件（够回答「涉及文件」就行）。"""
    _ver = _ver_mod()
    if _ver is None:
        return []
    try:
        ok, txt = _ver._run(["show", "--name-only", "--pretty=format:", str(h)])
    except Exception:
        return []
    if not ok:
        return []
    return [ln.strip() for ln in txt.splitlines() if ln.strip()][:30]


# ---------------------------------------------------------------- 主入口
def query_rollback(ts_hint: str = "") -> dict:
    """查证「上次回滚成功了吗」—— 只读 journal + git，直接给事实。

    返回字段
    --------
        status : "success" / "failed" / "none"
        ts     : 那次回滚的时间（查不到为空）
        files  : 涉及的文件列表
        count  : 涉及文件数
        reason : 失败原因（仅 failed）
        source : "journal" / "git" / ""
        line   : **首行可直接给用户看的结论**（成功/失败/无记录 + 时间 + 涉及文件）
        detail : 补充说明（依据 / 还原点 / git 提交）
    """
    hint = str(ts_hint or "").strip()
    filtered = bool(_ts_hint_core(hint))
    out = {"ok": True, "status": "none", "ts": "", "files": [], "count": 0,
           "reason": "", "act": "", "source": "", "line": "", "detail": [],
           "ts_hint": hint, "journal_hits": 0, "git": {}, "error": ""}
    try:
        recs = [r for r in _read_journal_all() if _rec_is_rollback(r)]
    except Exception:
        logger.debug("silent-except", exc_info=True)
        recs = []
    hits = [r for r in recs if _ts_match(r.get("ts"), hint)]
    out["journal_hits"] = len(hits)
    git = _git()
    out["git"] = git

    rec = hits[-1] if hits else None
    if rec is not None:
        out.update({
            "status": "failed" if _rec_failed(rec) else "success",
            "ts": str(rec.get("ts") or ""),
            "files": _rec_files(rec),
            "reason": str(rec.get("err") or rec.get("error") or "").strip(),
            "act": str(rec.get("act") or ""),
            "source": "journal",
        })
    else:
        # journal 里没有 → 退到 git（「回滚到 xxx」提交是回滚真落过地的硬证据）
        cm = [c for c in (git.get("commits") or [])
              if _ts_match(c.get("time"), hint) and "回滚到" in c.get("subject", "")]
        if cm:
            out.update({"status": "success",
                        "ts": str(cm[0].get("time") or ""),
                        "files": _git_commit_files(cm[0].get("hash") or ""),
                        "act": "git", "source": "git"})
        elif git.get("error"):
            out["error"] = str(git.get("error"))[:200]
    out["count"] = len(out["files"])

    # ---- 首行：成功 / 失败 / 无记录 + 时间 + 涉及文件（用户只看这一行就能判断）----
    st = out["status"]
    ts = out["ts"] or "（时间未记录）"
    n, files = out["count"], out["files"]
    flist = ("：" + "、".join(files[:6]) + ("…" if n > 6 else "")) if files else ""
    if st == "success":
        if files:
            out["line"] = ("结论：上次回滚成功 —— 时间 %s，涉及 %d 个文件%s"
                           % (ts, n, flist))
        elif out["source"] == "git":
            out["line"] = ("结论：上次回滚成功 —— 时间 %s"
                           "（git 提交在，文件清单没取到）" % ts)
        else:
            out["line"] = "结论：上次回滚成功 —— 时间 %s（记录里没写文件名）" % ts
    elif st == "failed":
        out["line"] = "结论：上次回滚失败 —— 时间 %s，涉及 %d 个文件%s%s" % (
            ts, n, flist,
            ("；原因：" + out["reason"][:120]) if out["reason"] else "")
    else:
        out["line"] = ("结论：无回滚记录 —— journal 里查不到回滚动作，"
                       "git 里也没有「回滚到 …」的提交"
                       + (("（筛选条件：%s）" % hint) if filtered else ""))

    # ---- 补充说明（第二段起；用户想深挖才看）----
    if out["source"] == "journal":
        _rel = str(rec.get("rel") or "")
        out["detail"].append(
            "· 依据：journal 的 %s 记录（命中 %d 条，取最近一条）%s"
            % (out["act"] or "rollback", len(hits),
               "，rel='-'" if _rel == "-" else ("，rel=" + _rel)))
    elif out["source"] == "git":
        out["detail"].append("· 依据：git 提交记录（journal 里没有对应的回滚动作）")
    undo = git.get("undo") or {}
    if undo.get("restore_hash"):
        out["detail"].append(
            "· 还原点：%s（回滚前自动保存%s）—— 想撤掉这次回滚就回到它"
            % (str(undo.get("restore_hash"))[:10],
               ("，" + str(undo.get("ts") or "")) if undo.get("ts") else ""))
    if git.get("commits"):
        out["detail"].append("· git 最近的回滚痕迹：" + "；".join(
            "%s %s" % (c.get("hash"), c.get("subject"))
            for c in (git.get("commits") or [])[:3]))
    if not git.get("ok") and git.get("error"):
        out["detail"].append("· git 没查到（不影响结论）：%s"
                             % str(git.get("error"))[:120])
    return out


def status_line(ts_hint: str = "") -> str:
    """只要那一行结论（调用方拼首行用）。查证本身出错也返回一句，不抛异常。"""
    try:
        return str(query_rollback(ts_hint).get("line") or "")
    except Exception as e:                     # noqa: BLE001
        logger.warning("回滚查证失败：%s", e)
        return "结论：回滚记录查不了（查证出错：%s）" % str(e)[:80]


def report(ts_hint: str = "") -> str:
    """人话版：首行结论 + 补充说明（给工具/调试用）。"""
    r = query_rollback(ts_hint)
    lines = [r.get("line") or ""]
    lines += [d for d in (r.get("detail") or []) if d]
    return "\n".join(x for x in lines if x)


# 挂到 selfdev 上：调用方写 `selfdev.query_rollback(...)` 也一样能跑。
# （selfdev.py 是只读的护栏文件，只能这样扩展 —— 与 tools.py 里
#   `_sd_fix.patch = _sd_patch_fixed` 同一套做法。）
try:
    if not hasattr(selfdev, "query_rollback"):
        selfdev.query_rollback = query_rollback           # type: ignore[attr-defined]
except Exception:
    logger.debug("silent-except", exc_info=True)
