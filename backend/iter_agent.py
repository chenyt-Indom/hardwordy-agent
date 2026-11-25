"""迭代执行器：**LLM 直接动手改代码**（工具循环），不再是「模型出补丁、系统代劳」。

用户 2026-11-17 原话：「我是要迭代时由 LLM 直接上手操作，而不是辅助」。
配套要求（同一轮答复里明确的）：
  · 允许执行任意命令（pip install / git / 起进程等）；
  · 一次最多 80 步 / 约 20 分钟，复杂功能也放它自己做完；
  · **全部**迭代动作都走这条路（含自动反思产生的）；
  · 改坏了回滚，但**把它的改动和失败原因存下来**给人看；
  · 模型不写死 —— 跟随当前配置/对话所用的模型。

和旧流程（`self_iterate._plan_patch_for_action`）的本质区别
------------------------------------------------------------
旧：模型只被允许「输出 find/replace JSON」→ 系统应用 → 系统跑校验 →
    失败就回滚（模型全程看不见文件系统，改一次就结束）。
新：模型拿到 8 个工具，自己 read → edit → run_check → 再改 →
    自己决定何时 finish。系统只在**最后**做一次独立复核（语法 / 导入 /
    回归 / 功能断言），失败才回滚。

安全边界（这些是硬护栏，模型改不动）
------------------------------------
· 只能写 `backend/` `frontend/` 下的源码后缀（沿用 self_iterate._norm_rel）；
· 护栏文件（selfdev.py / regress.py / _feature_tests/）拒绝写，且**事后校验哈希**
  —— 就算它用 shell 绕过去改了，也会被发现并整条回滚；
· 动手前对整个源码树做快照，回滚时精确还原（含删掉它新建的文件）。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("iter_agent")

# ★ 2026-11-22 用户要求「加防护」。
#   事故背景：验证「迭代写入会不会打评分标记」时直接调了 _t_write，
#   而它的 _ROOT 永远指向真实项目根 —— 结果把 backend/map_tools.py
#   （73KB）覆盖成 9 字节的「# probe」。备份才救回来。
#   防护一：根目录可被环境变量接管。测试/沙箱设 MM_ITER_ROOT 指到临时
#   目录，就不会碰到真实源码；不设时行为与以前完全一致（项目根）。
_ROOT = Path(os.environ.get("MM_ITER_ROOT")
             or Path(__file__).resolve().parent.parent).resolve()

# 允许模型写的源码后缀（与 self_iterate 的白名单保持一致）
_EXTS = (".py", ".js", ".css", ".html", ".json", ".md", ".txt")

# 快照/扫描时跳过的目录（体积大、与迭代无关，或本身是缓存）
_SKIP_DIRS = {
    "__pycache__", "_pysite", "node_modules", ".git", "backups",
    "image_library", "models", "sessions", "data", "logs",
}

# 护栏：这些文件**不许改**（模型改不动；用 shell 绕过也会被事后校验抓到）
_GUARD_FILES = (
    "backend/selfdev.py",       # 回撤机制自身（改坏了就退不回来）
    "backend/regress.py",       # 行为回归的断言来源（改了等于自己判自己及格）
    "backend/iter_agent.py",    # 本模块（它不该改自己的护栏）
    # ★ 2026-11-22 补：品牌盖章文件必须纳入护栏。
    #   此前 iter_agent 的 _t_write / _t_edit 直接 write_text 落盘，
    #   能绕过 brand.py 的密码闸门改掉 hardwordy / 开发者署名 —— 等于
    #   把「必须输密码才能改品牌」这条保护开了个后门。
    "backend/brand.py",
)
_GUARD_DIRS = ("backend/_feature_tests/",)

# 明显会毁机器的命令：拒绝执行（其余命令放行 —— 用户明确要求「任意命令」）
_CMD_DENY = (
    r"\bformat\b", r"\bdiskpart\b", r"\bshutdown\b", r"\breboot\b",
    r"rm\s+-rf\s+/(\s|$)", r"rd\s+/s\s+/q\s+[a-z]:\\?\s*$",
    r"\bgit\s+push\b", r"\bdel\s+/f\s+/s\s+/q\s+[a-z]:\\?\s*$",
)


# ---------------------------------------------------------------- 小工具

def _now_tag():
    return time.strftime("%Y%m%d_%H%M%S")


def _settings():
    """读自迭代的设置（失败就给空 dict，绝不让设置读取挡住迭代）。"""
    try:
        from . import self_iterate as _SI
        return _SI.load_settings() or {}
    except Exception:                                       # noqa: BLE001
        logger.debug("读设置失败", exc_info=True)
        return {}


def _data(*parts):
    """执行日志 / 快照的存放目录。

    ⚠️ 2026-11-17 修：原来用 `config.data("iter_agent", ...)`，而
    `config.data_root()` 返回的其实是**项目根**（不是 data/）—— 实测一次动作
    就在项目根下生成 `iter_agent/` 里 146 个文件 / 5MB，几次就把源码目录撑脏，
    而且 `_scan_files()` 还会把它当项目内容扫进来。
    现在统一放到 `data/self_iterate/agent/`，和其它迭代数据待在一起。
    """
    try:
        from . import self_iterate as _SI
        base = Path(_SI._DATA) / "agent"
    except Exception:                                       # noqa: BLE001
        from . import config
        base = Path(config.data("self_iterate", "agent"))
    p = base.joinpath(*parts)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                                       # noqa: BLE001
        logger.debug("建目录失败：%s", p.parent, exc_info=True)
    return p


def _prune_snapshots(keep=6):
    """只留最近几次快照 —— 一次约 5MB，不清理迟早把磁盘堆满。"""
    try:
        root = _data("snapshots")
        if not root.is_dir():
            return 0
        subs = sorted([d for d in root.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)
        gone = 0
        for d in subs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
        return gone
    except Exception:                                       # noqa: BLE001
        logger.debug("清理旧快照失败", exc_info=True)
        return 0


def _norm(rel):
    """规范化路径（只允许 backend/ frontend/ 下的源码），不合法返回空串。"""
    try:
        from . import self_iterate as _SI
        return _SI._norm_rel(rel)
    except Exception:                                       # noqa: BLE001
        return ""


def _is_guard(rel):
    r = str(rel or "").replace("\\", "/").lstrip("./")
    if r in _GUARD_FILES:
        return True
    # ⚠️⚠️ 2026-11-24 实测缺陷：`_GUARD_DIRS` 把**整个** _feature_tests/ 都算护栏，
    #   连 `_pending_*.py`（待办/临时断言）也算 —— 而这类文件本来就会被正常
    #   创建、消费、清理掉。文件一没，`_sha()` 返回 ""≠快照 sha，于是
    #   `_final_verify` 判「护栏文件被修改（不允许）」，**整轮直接回滚**。
    #   实测 11-23 18:46 那次动作失败正是它：
    #     「护栏文件被修改（不允许）：backend/_feature_tests/_pending_盯梢任务哨兵…py」
    #   产品代码一行没坏，只是临时文件被清了。
    #   护栏的语义是「不许模型改写**验收断言**」，而下划线开头的文件
    #   regress 自己也按 `_` 前缀跳过（不参与回归），本就不是断言。
    #   所以：_feature_tests/ 下只把**非下划线开头**的 test_*.py 当护栏。
    if any(r.startswith(d) for d in _GUARD_DIRS):
        name = r.rsplit("/", 1)[-1]
        return not name.startswith("_")
    return False


def _mark_dirty(rel, note=""):
    """迭代改过的文件也要纳入评分闸门（2026-11-22 用户要求）。

    用户原话：「增加新功能以及加入推荐的功能或者优化改进时有没有也进行
              跑分评测然后反思报告？」。
    之前 _t_write / _t_edit 是**直接 write_text 落盘**，绕过了 selfdev_write，
    于是不打「待评分」标记 —— 一旦这轮迭代因为修不好 / 用户终止 / 超预算
    而没走到 selfdev_apply_restart，这次改动就完全没被评分记录过。
    现在每次写入成功都打标记，保证「只要动了代码，收尾必评分」。
    """
    try:
        from . import scoregate as _scg
        _scg.mark_dirty(str(rel or ""), str(note or "LLM 直接操作"))
    except Exception:                                       # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def _read_text(rel):
    try:
        return (_ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:                                       # noqa: BLE001
        return ""


def _sha(path):
    try:
        h = hashlib.sha256()
        h.update(Path(path).read_bytes())
        return h.hexdigest()[:16]
    except Exception:                                       # noqa: BLE001
        return ""


def _scan_files():
    """列出可迭代的源码文件（相对路径）。"""
    out = []
    for base in ("backend", "frontend"):
        d = _ROOT / base
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() not in _EXTS:
                continue
            if p.name.endswith(".pyc_tmp"):
                continue
            out.append(p.relative_to(_ROOT).as_posix())
    return sorted(out)


# ---------------------------------------------------------------- 快照 / 回滚

def _snapshot(tag):
    """动手前把整个源码树拷一份。返回 {"tag","dir","files":{rel:sha},"guards":{rel:sha}}。"""
    dst = _data("snapshots", tag)
    dst.mkdir(parents=True, exist_ok=True)
    files, guards = {}, {}
    for rel in _scan_files():
        src = _ROOT / rel
        out = dst / rel.replace("/", "__")
        try:
            shutil.copy2(src, out)
            files[rel] = _sha(src)
            if _is_guard(rel):
                guards[rel] = files[rel]
        except Exception:                                   # noqa: BLE001
            logger.debug("快照失败：%s", rel, exc_info=True)
    try:
        (dst / "_manifest.json").write_text(
            json.dumps({"tag": tag, "files": files, "guards": guards,
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        logger.debug("写快照清单失败", exc_info=True)
    _prune_snapshots(6)
    return {"tag": tag, "dir": dst, "files": files, "guards": guards}


def _changed_files(snap):
    """对比快照，返回 (修改过的, 新建的, 被删掉的)。"""
    mod, new, gone = [], [], []
    for rel, sha in (snap.get("files") or {}).items():
        p = _ROOT / rel
        if not p.is_file():
            gone.append(rel)
        elif _sha(p) != sha:
            mod.append(rel)
    known = set((snap.get("files") or {}).keys())
    for rel in _scan_files():
        if rel not in known:
            new.append(rel)
    return sorted(mod), sorted(new), sorted(gone)


def _restore(snap):
    """精确还原：改过的写回、删掉的补回、新建的删掉。返回还原了哪些。"""
    done = []
    for rel, sha in (snap.get("files") or {}).items():
        src = snap["dir"] / rel.replace("/", "__")
        if not src.is_file():
            continue
        p = _ROOT / rel
        try:
            if p.is_file() and _sha(p) == sha:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, p)
            done.append(rel)
        except Exception:                                   # noqa: BLE001
            logger.debug("还原失败：%s", rel, exc_info=True)
    mod, new, _gone = _changed_files(snap)
    for rel in new:
        try:
            (_ROOT / rel).unlink()
            done.append("-" + rel)
        except Exception:                                   # noqa: BLE001
            logger.debug("删除新建文件失败：%s", rel, exc_info=True)
    return done


def _compat_backup(snap, mod, new, tag):
    """再做一份 **self_iterate 格式** 的备份（`data/backups/<tag>/`）。

    为什么需要：`self_iterate.rollback()` 和「自检问题变多就回滚最后一个改动」
    都靠 `_restore(tag)`，它读的是 `_BACKUPS/<tag>/_manifest.json`
    和 `_BACKUPS/<tag>/<rel 里的 / 换成 __>`。这里按同一格式写一份，
    让「用户手动回滚」和「护栏自动回滚」对这个动作同样有效。
    """
    try:
        from . import self_iterate as _SI
        dst = Path(_SI._BACKUPS) / tag
        dst.mkdir(parents=True, exist_ok=True)
        for rel in mod:
            src = snap["dir"] / rel.replace("/", "__")
            if src.is_file():
                shutil.copy2(src, dst / rel.replace("/", "__"))
        (dst / "_manifest.json").write_text(json.dumps(
            {"files": list(mod), "created": list(new), "ts": time.time(),
             "tag": tag, "by": "iter_agent"}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return tag
    except Exception:                                       # noqa: BLE001
        logger.debug("写兼容备份失败", exc_info=True)
        return ""


def _hunks(old, new):
    """把改动整理成前端代码卡片能渲染的 find/replace 对。"""
    a, b = (old or "").splitlines(), (new or "").splitlines()
    out = []
    try:
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            out.append({"find": "\n".join(a[i1:i2])[:2400],
                        "replace": "\n".join(b[j1:j2])[:2400]})
    except Exception:                                       # noqa: BLE001
        logger.debug("算 diff 失败", exc_info=True)
    return out[:40]


def _diff_text(snap, rels):
    """给日志/评审用的 unified diff。"""
    chunks = []
    for rel in rels:
        old = ""
        src = snap["dir"] / rel.replace("/", "__")
        try:
            if src.is_file():
                old = src.read_text(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            old = ""
        new = _read_text(rel)
        if old == new:
            continue
        d = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                 fromfile="a/" + rel, tofile="b/" + rel,
                                 lineterm="", n=2)
        chunks.append("\n".join(list(d)[:400]))
    return "\n".join(chunks)


# ---------------------------------------------------------------- 工具实现

class _Ctx:
    """一次动作的执行上下文（工具共享）。"""

    def __init__(self, plan, action, ev, stop, steps, seconds, snap, base_fails, base_ok):
        self.plan = plan
        self.action = action
        self.ev = ev
        self.stop = stop
        self.steps = steps
        self.seconds = seconds
        self.snap = snap
        self.base_fails = base_fails
        self.base_ok = base_ok
        self.t0 = time.time()
        self.n_calls = 0
        self.log = []          # [{name, args, result}]
        self.finished = False
        self.finish_args = {}
        self.touched = []      # 动过的文件（写/编辑）
        self.last_check = ""   # 最近一次 run_check 的结果（收尾复核用）
        self.stopped = False   # 用户点了「终止」

    # --- 预算 ---
    def elapsed(self):
        return time.time() - self.t0

    def over(self):
        if self.elapsed() > self.seconds:
            return "时间预算已用完（已跑 %d 秒，上限 %d 秒）" % (
                int(self.elapsed()), int(self.seconds))
        if self.n_calls >= self.steps:
            return "步数预算已用完（上限 %d 步）" % int(self.steps)
        return ""

    def emit(self, text, kind="step"):
        if self.ev:
            try:
                self.ev(kind, text=text)
            except Exception:                               # noqa: BLE001
                logger.debug("事件回调失败", exc_info=True)

    def record(self, name, args, result):
        self.n_calls += 1
        self.log.append({
            "n": self.n_calls, "tool": name,
            "args": {k: (str(v)[:200]) for k, v in (args or {}).items()},
            "result": str(result)[:400],
            "seconds": round(self.elapsed(), 1),
        })


def _t_read(ctx, args):
    rel = _norm(args.get("path"))
    if not rel:
        return "拒绝：路径不在可改范围（只能读 backend/ frontend/ 下的源码）"
    p = _ROOT / rel
    if not p.is_file():
        # 允许读存在的非源码文件（配置文件等），方便它自己排查
        raw = str(args.get("path") or "").replace("\\", "/").lstrip("./")
        if not raw or ".." in raw:
            return "文件不存在：%s" % args.get("path")
        p2 = _ROOT / raw
        try:
            p2.resolve().relative_to(_ROOT.resolve())
        except Exception:                                   # noqa: BLE001
            return "拒绝：路径越界"
        if not p2.is_file():
            return "文件不存在：%s" % raw
        p, rel = p2, raw
    body = _read_text(rel)
    lines = body.split("\n")
    total = len(lines)
    start = int(args.get("start") or 1)
    end = int(args.get("end") or 0)
    if end <= 0 or end > total:
        end = total
    start = max(1, min(start, total))
    seg = lines[start - 1:end]
    head = "（%s 共 %d 行，下面是第 %d~%d 行）\n" % (rel, total, start, end)
    return head + "\n".join("%5d| %s" % (i, s) for i, s in enumerate(seg, start))


# 原文件小于这个字节数就不判缩水（小文件本来就短，缩水是正常的）
_SHRINK_MIN = 2000
# 新内容不到原文的 15% → 视为毁灭性覆盖（典型事故：73KB 被写成 9 字节）
_SHRINK_RATIO = 0.15


def _backup_before_write(rel):
    """写前备份（防护二）：覆盖已有文件前先留一份，出事能捞回来。

    事故当时是靠 selfdev 的旧备份救回 map_tools.py 的 —— 那是运气。
    这里让迭代自己的每次覆盖都有独立备份，不依赖别的模块。
    返回备份文件路径，没备份（新文件 / 失败）返回空串。
    """
    try:
        src = _ROOT / rel
        if not src.is_file():
            return ""
        # 走 config.data 与其他数据目录保持一致：设了 MM_DATA_DIR（Docker /
        # 便携包换目录）时，备份也要落到新目录，不能写回源码树。
        try:
            from . import config as _cfg
            data_dir = Path(_cfg.data("data"))
        except Exception:                                   # noqa: BLE001
            data_dir = Path(os.environ.get("MM_DATA_DIR") or (_ROOT / "data"))
        bd = data_dir / "iter_backups"
        bd.mkdir(parents=True, exist_ok=True)
        safe = str(rel).replace("/", "%2F").replace("\\", "%2F")
        dst = bd / ("%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), safe))
        shutil.copy2(str(src), str(dst))
        return str(dst)
    except Exception:                                       # noqa: BLE001
        logger.debug("迭代写前备份失败", exc_info=True)
        return ""


def _is_destructive(rel, new_text):
    """毁灭性覆盖判定（防护三）。返回 (是否危险, 原文字节, 新字节)。

    为什么需要：模型（或我）把「改一处」误写成「整份重写」时，最容易
    的表现就是用几十字节覆盖掉几万字节 —— 这种覆盖 99% 是事故不是本意。
    拦下来让人显式加 allow_shrink=true，比事后从备份里捞便宜得多。
    """
    try:
        p = _ROOT / rel
        new_len = len((new_text or "").encode("utf-8"))
        if not p.is_file():
            return (False, 0, new_len)
        old_len = p.stat().st_size
        if old_len >= _SHRINK_MIN and new_len < old_len * _SHRINK_RATIO:
            return (True, old_len, new_len)
        return (False, old_len, new_len)
    except Exception:                                       # noqa: BLE001
        logger.debug("毁灭性覆盖判定失败", exc_info=True)
        return (False, 0, len((new_text or "").encode("utf-8")))


def _t_write(ctx, args):
    rel = _norm(args.get("path"))
    if not rel:
        return "拒绝：只能写 backend/ frontend/ 下的源码文件"
    if _is_guard(rel):
        return "拒绝：%s 是护栏文件，禁止修改" % rel
    content = args.get("content")
    if not isinstance(content, str):
        return "拒绝：content 必须是字符串（整份文件内容）"
    if rel.endswith(".py"):
        try:
            compile(content, rel, "exec")
        except SyntaxError as e:
            return ("语法错误，没有写入：第 %s 行 %s" % (e.lineno, e.msg))
    bad, old_len, new_len = _is_destructive(rel, content)
    if bad and not args.get("allow_shrink"):
        return ("拒绝：这会用 %d 字节覆盖原文件的 %d 字节（不足 15%%），"
                "像是把文件清空了。确认要整份重写，请加 allow_shrink=true"
                % (new_len, old_len))
    p = _ROOT / rel
    bk = _backup_before_write(rel)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:                                  # noqa: BLE001
        return "写入失败：%s: %s" % (type(e).__name__, e)
    if rel not in ctx.touched:
        ctx.touched.append(rel)
    _mark_dirty(rel, "迭代写入")
    return "已写入 %s（%d 字符）%s" % (
        rel, len(content), ("；原文件已备份到 %s" % bk) if bk else "")


def _t_edit(ctx, args):
    rel = _norm(args.get("path"))
    if not rel:
        return "拒绝：只能改 backend/ frontend/ 下的源码文件"
    if _is_guard(rel):
        return "拒绝：%s 是护栏文件，禁止修改" % rel
    p = _ROOT / rel
    if not p.is_file():
        return "文件不存在：%s（新建请用 write_file）" % rel
    body = _read_text(rel)
    find = args.get("find")
    repl = args.get("replace")
    if not isinstance(find, str) or not find:
        return "拒绝：find 不能为空（要整份重写请用 write_file）"
    if not isinstance(repl, str):
        return "拒绝：replace 必须是字符串"
    cnt = body.count(find)
    if cnt == 0:
        return "没找到要替换的原文（find 必须与文件内容一字不差，含缩进）"
    if cnt > 1:
        return "find 在文件里出现了 %d 次，无法确定改哪一处，请写得更具体" % cnt
    new = body.replace(find, repl, 1)
    if rel.endswith(".py"):
        try:
            compile(new, rel, "exec")
        except SyntaxError as e:
            return ("改完会有语法错误，没有写入：第 %s 行 %s" % (e.lineno, e.msg))
    bk = _backup_before_write(rel)
    try:
        p.write_text(new, encoding="utf-8")
    except Exception as e:                                  # noqa: BLE001
        return "写入失败：%s: %s" % (type(e).__name__, e)
    if rel not in ctx.touched:
        ctx.touched.append(rel)
    _mark_dirty(rel, "迭代修改")
    return "已修改 %s（替换 1 处，净变化 %+d 字符）%s" % (
        rel, len(new) - len(body),
        ("；原文件已备份到 %s" % bk) if bk else "")


def _t_list(ctx, args):
    base = str(args.get("path") or "backend").replace("\\", "/").strip("/")
    d = _ROOT / base
    try:
        d.resolve().relative_to(_ROOT.resolve())
    except Exception:                                       # noqa: BLE001
        return "拒绝：路径越界"
    if not d.is_dir():
        return "目录不存在：%s" % base
    rows = []
    for p in sorted(d.iterdir()):
        if p.name in _SKIP_DIRS or p.name == "__pycache__":
            continue
        if p.is_dir():
            rows.append("  %s/" % p.name)
        else:
            try:
                n = sum(1 for _ in p.open("rb"))
            except Exception:                               # noqa: BLE001
                n = 0
            rows.append("  %s  (%d 行, %d KB)" % (p.name, n, max(1, p.stat().st_size // 1024)))
        if len(rows) >= 120:
            rows.append("  …（还有更多，已截断）")
            break
    return "%s/ 下共 %d 项：\n%s" % (base, len(rows), "\n".join(rows))


def _t_grep(ctx, args):
    pat = str(args.get("pattern") or "")
    if not pat:
        return "pattern 不能为空"
    base = str(args.get("path") or "backend").replace("\\", "/").strip("/")
    d = _ROOT / base
    try:
        d.resolve().relative_to(_ROOT.resolve())
    except Exception:                                       # noqa: BLE001
        return "拒绝：路径越界"
    limit = max(1, min(int(args.get("limit") or 40), 200))
    try:
        rx = re.compile(pat)
    except re.error:
        rx = re.compile(re.escape(pat))
    hits = []
    files = [d] if d.is_file() else sorted(d.rglob("*"))
    for p in files:
        if not p.is_file() or p.suffix.lower() not in _EXTS:
            continue
        if any(x in _SKIP_DIRS for x in p.parts):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                if rx.search(line):
                    hits.append("%s:%d: %s" % (p.relative_to(_ROOT).as_posix(), i, line.strip()[:180]))
                    if len(hits) >= limit:
                        break
        except Exception:                                   # noqa: BLE001
            continue
        if len(hits) >= limit:
            break
    return ("命中 %d 处：\n%s" % (len(hits), "\n".join(hits))) if hits else "没有命中"


def _kill_tree(pid):
    """杀掉整棵进程树。

    ★ 为什么必须单独做：Windows 上 `p.kill()` 只杀 cmd.exe，它派生的
      node.exe / python.exe 会变成孤儿继续跑、并且**继续持有管道的写端**。
      实测 11-20 04:18 那条 `cmd /c "node -e "..."` 就是因此把 worker 卡死。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            import signal as _sg
            os.killpg(os.getpgid(pid), _sg.SIGKILL)
    except Exception:                                       # noqa: BLE001
        logger.debug("杀进程树失败（忽略）", exc_info=True)


def _t_cmd(ctx, args):
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "command 不能为空"
    low = cmd.lower()
    for pat in _CMD_DENY:
        if re.search(pat, low):
            return "拒绝执行：这条命令有毁坏机器的风险（%s）" % pat
    timeout = max(5, min(int(args.get("timeout") or 120), 600))
    t0 = time.time()
    # ★ 2026-11-20 修（用户报「迭代卡住不动了」）：
    #   原来用 `subprocess.run(cmd, shell=True, timeout=timeout)`。
    #   Windows 上它超时后还会再调一次 `communicate()` 去读剩余输出 ——
    #   此时 cmd.exe 已被 kill，但它派生的 node.exe 还活着并持有管道写端，
    #   这次 communicate() 会**永久阻塞**，「120 秒超时」形同虚设。
    #   实测：04:18:08 一条 `cmd /c "node -e "global.document={...}"`
    #   （嵌套双引号让 cmd 一直等输入）把 worker 线程卡了 11 分钟以上，
    #   任务永远停在「正在迭代」，事件流一条都不再增加。
    #   改法：Popen + stdin=DEVNULL（不再等输入）+ 超时杀整棵进程树 +
    #   **超时后绝不再 communicate()**，立刻返回。
    try:
        p = subprocess.Popen(
            cmd, shell=True, cwd=str(_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0))
    except Exception as e:                                  # noqa: BLE001
        return "命令执行失败：%s: %s" % (type(e).__name__, e)
    try:
        out_b, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p.pid)
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            p.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            pass
        return ("命令超时（%d 秒），已强制终止整棵进程树：%s\n"
                "提示：多半是命令在等输入或引号没闭合，换个写法再试。"
                % (timeout, cmd[:120]))
    out = (out_b or b"").decode("utf-8", "replace")
    return "退出码 %d（%.1f 秒）\n%s" % (
        p.returncode, time.time() - t0, out[-6000:] or "（无输出）")


def _t_check(ctx, args):
    """跑真实验证：syntax / import / regress / all。"""
    kind = str(args.get("kind") or "all").lower()
    rels = list(dict.fromkeys(ctx.touched + [
        r for r in (args.get("files") or []) if isinstance(r, str)]))
    out = []

    if kind in ("syntax", "all"):
        from . import self_iterate as _SI
        bad = []
        for rel in rels:
            ok, msg = _SI._verify(rel)
            if not ok:
                bad.append("%s：%s" % (rel, msg))
        out.append("【语法】%s" % ("全部通过（%d 个文件）" % len(rels) if not bad
                                  else "失败：\n" + "\n".join(bad[:6])))

    if kind in ("import", "all"):
        code = ("import sys;sys.path.insert(0,r'%s');"
                "import backend.main;print('IMPORT_OK')" % str(_ROOT))
        try:
            r = subprocess.run([sys.executable, "-c", code], cwd=str(_ROOT),
                               capture_output=True, timeout=180)
            o = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
            ok = "IMPORT_OK" in o and r.returncode == 0
            out.append("【导入】%s" % ("backend.main 可以正常导入"
                                      if ok else "失败：\n" + o[-1500:]))
        except Exception as e:                              # noqa: BLE001
            out.append("【导入】跑不起来：%s: %s" % (type(e).__name__, e))

    if kind in ("regress", "all"):
        try:
            from . import regress as _RG
            r = _RG.run_disk(scope="quick")
            fails = {str(x.get("name")) for x in (r.get("failed_items") or [])}
            new = fails - (ctx.base_fails or set())
            if new and ctx.base_ok:
                out.append("【回归】新增失败 %d 条：%s" % (len(new), "；".join(sorted(new)[:6])))
            elif new:
                out.append("【回归】%d 条失败（基线不可用，无法归因）：%s"
                           % (len(new), "；".join(sorted(new)[:4])))
            elif not r.get("ok"):
                out.append("【回归】%d 条既有失败（改动前就是红的，与你无关）"
                           % len(ctx.base_fails or ()))
            else:
                out.append("【回归】全部通过（%s 条 / %s 秒）"
                           % (r.get("passed"), r.get("seconds")))
        except Exception as e:                              # noqa: BLE001
            out.append("【回归】跑不起来：%s: %s" % (type(e).__name__, e))

    txt = "\n".join(out)
    ctx.last_check = txt
    return txt


def _t_finish(ctx, args):
    # ★ 用户要求「LLM 修完/写完代码之后要反复验证，直到一切完全正常」：
    #   模型一次 run_check 都没跑就想收工 → 先让它自己跑一遍看结果，
    #   有问题就地修。只提醒一次，避免模型死活不肯跑时卡在这里（系统复核仍会兜底）。
    if not ctx.last_check and not getattr(ctx, "finish_nagged", False):
        ctx.finish_nagged = True
        return ("还没跑过自检。请先调 run_check(kind=\"all\") 看真实结果，"
                "有问题就地修好、再跑一次确认全过，然后才 finish。")
    ctx.finished = True
    ctx.finish_args = dict(args or {})
    return "收到完成信号。系统接下来会独立复核（语法 / 导入 / 回归 / 功能断言）。"


_DISPATCH = {
    "read_file": _t_read,
    "write_file": _t_write,
    "edit_file": _t_edit,
    "list_dir": _t_list,
    "grep": _t_grep,
    "run_cmd": _t_cmd,
    "run_check": _t_check,
    "finish": _t_finish,
}


def _step_brief(name, args, res):
    """把一次工具调用压成一句人话，给 record_stage('agent_step') 用。

    只挑「用户真正关心」的动作（读/写/改/跑命令/自检），其余（list_dir/grep/finish）
    返回空串不记 —— 否则日志会被定位类调用刷屏。
    """
    name = str(name or "")
    args = args if isinstance(args, dict) else {}
    res = str(res or "")
    ok = not res.startswith(("拒绝", "没找到", "文件不存在", "工具异常", "命令执行失败",
                             "命令超时", "语法错误", "改完会有语法错误", "写入失败"))
    if name == "read_file":
        return "读 %s" % (args.get("path") or "?")
    if name == "write_file":
        return "%s %s" % ("写入" if ok else "写入失败", args.get("path") or "?")
    if name == "edit_file":
        return "%s %s" % ("修改" if ok else "修改失败", args.get("path") or "?")
    if name == "run_cmd":
        cmd = str(args.get("command") or "").strip().replace("\n", " ")
        return "跑命令：%s" % (cmd[:100] or "?")
    if name == "run_check":
        return "自检 %s：%s" % (args.get("kind") or "all",
                                "通过" if ok else "有问题")
    return ""


class _Cancelled(Exception):
    """用户点了终止：用它跳出整个工具循环，不再等模型再跑一轮 finish。

    ★ 2026-11-25 修（用户报「点终止还停不下来，要等好久」）：原来是返回一句
    「请立刻调用 finish 收尾」交给模型 —— 那等于**再跑一次完整模型调用**，
    本地模型一次几分钟，实测终止后还要等 297 秒才结束。
    """


def _exec(ctx, name, args):
    """工具总入口：统一记录 + 预算拦截 + 异常兜底。"""
    name = str(name or "")
    args = args if isinstance(args, dict) else {}
    # 用户点了「终止」→ 立刻停手（见 _Cancelled 的说明）。
    try:
        if ctx.stop and ctx.stop() and name != "finish":
            ctx.stopped = True
            raise _Cancelled("用户终止")
    except _Cancelled:
        raise
    except Exception:                                       # noqa: BLE001
        logger.debug("终止检查失败", exc_info=True)
    over = ctx.over()
    if over and name != "finish":
        res = "⏰ %s —— 不要再调工具了，立刻调用 finish 收尾。" % over
        ctx.record(name, args, res)
        return res
    fn = _DISPATCH.get(name)
    if not fn:
        return "没有这个工具：%s（可用：%s）" % (name, "、".join(_DISPATCH))
    if name in ("write_file", "edit_file"):
        ctx.emit("%s %s" % (name, args.get("path") or "?"))
    elif name == "run_cmd":
        ctx.emit("跑命令：%s" % str(args.get("command") or "")[:80])
    elif name == "run_check":
        ctx.emit("自检：%s" % (args.get("kind") or "all"))
    try:
        res = fn(ctx, args)
    except Exception as e:                                  # noqa: BLE001
        res = "工具异常：%s: %s" % (type(e).__name__, e)
        logger.debug("工具异常：%s", name, exc_info=True)
    ctx.record(name, args, res)
    # 每步工具调用都记一句人话，让用户实时看到 LLM 在动哪个文件 / 跑什么。
    # ⚠️ 2026-11-17 修 high bug：这里原来调用 `record_stage(ctx.plan, ...)`，
    #    但**全项目从来没有 record_stage 这个函数** —— 每次调用都抛 NameError，
    #    又被外层 `except Exception` 吞掉，于是「每步在改哪个文件」一次都没显示过，
    #    静态自检也只在把 except 当成正常逻辑时才没报。
    #    改成真实存在的两条路径：
    #      ① 写进 plan（前端轮询 run_status / 动作卡片可读）；
    #      ② 推进全局阶段提示 set_stage（self_iterate 里真实存在，延迟 import 防循环依赖）。
    try:
        _step_summary = _step_brief(name, args, res)
        if _step_summary:
            ctx.plan["_agent_step"] = _step_summary
            ctx.plan["_agent_step_n"] = int(ctx.plan.get("_agent_step_n") or 0) + 1
            # ② 落进 actlog 的阶段流水（时间线卡片按 plan_id 读它）。
            #    ⚠️ record_stage 真正住在 actlog 里，签名是 (plan_id, stage, detail)；
            #    原来既没 import，又把 plan **字典**当成了 plan_id，双错。
            try:
                from . import actlog as _actlog
                _pid = str((ctx.plan or {}).get("id") or (ctx.plan or {}).get("plan_id")
                           or (ctx.plan or {}).get("pid") or "")
                _actlog.record_stage(_pid, "agent_step", _step_summary)
            except Exception:                               # noqa: BLE001
                logger.debug("actlog.record_stage 不可用，跳过阶段流水", exc_info=True)
            try:
                from .self_iterate import set_stage as _set_stage
                _set_stage("exec", _step_summary)
            except Exception:                               # noqa: BLE001
                logger.debug("set_stage 不可用，跳过阶段提示", exc_info=True)
    except Exception:                                       # noqa: BLE001
        logger.debug("记录 agent_step 失败", exc_info=True)
    return res


# ---------------------------------------------------------------- 工具 schema

def _schemas():
    def f(name, desc, props, required=()):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": list(required)}}}

    return [
        f("read_file", "读文件内容（带行号）。大文件请用 start/end 分段读。",
          {"path": {"type": "string", "description": "相对路径，如 backend/self_iterate.py"},
           "start": {"type": "integer", "description": "起始行（从 1 开始）"},
           "end": {"type": "integer", "description": "结束行，0=读到末尾"}},
          ["path"]),
        f("write_file", "整份写入/新建文件。会先做 Python 语法检查，语法错不落盘。",
          {"path": {"type": "string"},
           "content": {"type": "string", "description": "完整文件内容"}},
          ["path", "content"]),
        f("edit_file", "精确替换：find 必须与原文一字不差且全文只出现一次。",
          {"path": {"type": "string"}, "find": {"type": "string"},
           "replace": {"type": "string"}},
          ["path", "find", "replace"]),
        f("list_dir", "列目录（文件名 + 行数），先看清有哪些文件。",
          {"path": {"type": "string", "description": "如 backend / frontend"}}),
        f("grep", "按正则搜代码，返回 文件:行号: 内容。",
          {"pattern": {"type": "string"}, "path": {"type": "string"},
           "limit": {"type": "integer"}},
          ["pattern"]),
        f("run_cmd", "在本机执行任意命令（pip / git / python / node 等），返回退出码与输出。",
          {"command": {"type": "string"}, "timeout": {"type": "integer"}},
          ["command"]),
        f("run_check", "跑真实验证：syntax(语法) / import(能启动) / regress(行为回归) / all。"
                       "改完**必须**至少跑一次 all 再 finish。",
          {"kind": {"type": "string", "enum": ["syntax", "import", "regress", "all"]},
           "files": {"type": "array", "items": {"type": "string"}}}),
        f("finish", "宣布做完了。调用前必须已经跑过 run_check(all) 并且结果正常。",
          {"summary": {"type": "string", "description": "做了什么，一句话"},
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "改了/新建了哪些文件"}},
          ["summary"]),
    ]


# ---------------------------------------------------------------- 提示词

_SYS = """你是一个**直接动手**的工程师：在用户这个项目里实现一条已批准的改动。
你**不是**给建议、也不是输出补丁 —— 你有工具，自己读代码、自己改、自己验证。

工作方式（必须照做）：
1. 先 `list_dir` / `grep` 定位，再 `read_file` 读**真实代码**。不许凭印象写。
2. 用 `edit_file`（精确替换）或 `write_file`（整份写）落地。改动要**完整**：
   半截实现比不实现更糟；新增功能要连前端、路由、数据结构一起接上。
3. 改完**必须** `run_check(kind="all")`。有失败就照真实报错继续改，再跑，直到通过。
4. 全部通过后调用 `finish(summary, files)` 收尾。**没跑过 run_check(all) 不许 finish。**

硬约束（违反会被系统回滚，白干）：
· 只能写 backend/ 与 frontend/ 下的源码；**不许**碰 data/ models/ sessions/ image_library/。
· **不许**修改 backend/selfdev.py、backend/regress.py、backend/_feature_tests/ 下的断言文件
  —— 那是护栏，改了等于自己判自己及格（系统会比对哈希，发现即整条回滚）。
· 不要动与需求无关的函数。这个项目 backend/main.py 有 1.5 万行、backend/tools.py 有 7 千行，
  它们常常只是"顺带被列进候选"；除非需求明确要求，一个字都别改。
  实测教训：为了"存套路"顺手改了 main.py 的 _clip_reply，6 条回归断言全红、整条回滚。
· 需求与代码不符、确实做不到 → 直接 `finish(summary="做不到：原因")`，不要硬凑。
"""

_TASK = """## 要做的改动
标题：{title}
说明：{detail}
用户备注（★ 与上面的说明冲突时，**一律以这条备注为准**）：{note}
计划里点名的文件（**仅供参考**）：{files}
风险等级：{risk}

⚠️ 上面这些**只是计划阶段的猜测，不是限制**。你是真正动手的人：
   - 该改哪些文件、怎么改，由你读代码后自己判断；
   - 如果计划点错了文件、或漏了必须一起改的文件（前端/路由/数据结构），
     照你判断的改，并在 finish 的 summary 里说清为什么偏离计划。

## 项目结构（源码文件 + 行数，先看这里决定读哪个）
{inventory}

## 预算
最多 {steps} 步、约 {minutes} 分钟。到点会被强制收尾。
{prev}
现在开始：先定位、再读、再改，改完跑 run_check(all)，通过后 finish。
"""

# 重试时插进任务描述的一段。用户要求「迭代受阻 LLM 要自己解决直到成功」——
# 光重跑一遍没用（模型不知道上次错在哪），必须把系统复核的真实原因给它。
_PREV_FAIL = """
## ⚠️ 上一次尝试失败了（第 {n} 次重试，代码已回滚到改动前）

系统复核给出的原因：
{why}

请**先针对这个原因修正**，再继续做完整件事：
· 如果是「语法校验失败」→ 把那个文件读出来，改到能编译为止；
· 如果是「backend.main 无法导入」→ 通常是引用了不存在的函数/变量/导入，先 grep 确认再改；
· 如果是「行为回归新增失败」→ 你改坏了别处的行为，用 grep 找到被影响的地方，改成不影响原有行为；
· 如果是「LLM 评审拒绝」→ 说明改动偏离需求或误伤了无关代码，缩小改动范围；
· 如果是「没有改动任何文件」→ 你只说了没做，这次必须真的 write_file/edit_file。
不要换个地方乱改，也不要放弃 —— 除非确实做不到（那就 finish 说明原因）。
"""

# ★★ 2026-11-17 用户要求（核心）：
#   「遇到阻碍或哪里有问题时，不要直接失败回滚，LLM 需要和平时一样排除困难
#     完成迭代」「LLM 必须发挥作用来排除错误以及迭代时遇到的困难和阻碍」
#   所以复核没过时**不回滚**，把系统复核的真实原因 + 自己改出来的 diff 一起
#   喂回去，让它在**同一份改动上**修好。只有修复轮用尽仍不行，才回滚。
#
#   与 _PREV_FAIL 的区别很关键：
#     _PREV_FAIL —— 外层重试用，**代码已回滚**，换思路从头再来；
#     _REPAIR    —— 内层就地修，**代码保持原样**，看着真实报错修。
#   后者才是人排查 bug 的方式，也不会把前面几十步的工作白扔掉。
_REPAIR = """
## 原始任务（★ 别丢：修复要照着它做，不是照着报错瞎改）
标题：{task_title}
说明：{task_detail}
点名文件：{task_files}

## 系统复核没通过（第 {n} 轮就地修复）

你刚才的改动**没有被回滚，文件现在就是改过之后的状态**。系统另外跑了一遍
独立复核，给出的问题在这里：

{why}

你这一轮改动出来的 diff（供你对照，看看是哪里出的问题）：

```diff
{diff}
```

请**就地把它修好** —— 不要重头再来，也不要放弃：
- 语法错（中文冒号/全角括号/引号不配对/缩进）→ 把那几行读出来，改到能编译；
- backend.main 无法导入 → 说明引用了不存在的名字，先 grep 确认再改；
- 行为回归新增失败 → 你改坏了别处的行为，找到被影响的地方，改成不影响原有行为；
- 功能验收断言没过 → 按断言要求的行为把它补全；
- LLM 评审拒绝 → 改动偏离需求或误伤了无关代码，缩小改动范围；
- 「没有改动任何文件」→ 你只说了没做，这次必须真的 write_file / edit_file。

★ 两条硬约束（2026-11-25 实测：3 次失败都是「连续 3 轮只说不做」）：
1. **第一件事必须是工具调用**：不要先输出解释/计划/复述任务，直接调用
   write_file / edit_file 改上面「点名文件」里的那一个，改完再说话。
2. **只许改点名文件**：上面列了哪些文件就改哪些。要动别的文件必须先说明理由，
   否则评审会判「需求与代码对不上」并整轮回滚（实测发生过 2 次）。

修完**必须**跑 run_check(kind="all")，确认语法 / 导入 / 回归全过，再 finish。
你还有约 {steps} 步、{minutes} 分钟可用。
"""


def _inventory(limit=90):
    rows = []
    for rel in _scan_files():
        try:
            n = sum(1 for _ in (_ROOT / rel).open("rb"))
        except Exception:                                   # noqa: BLE001
            n = 0
        if n < 5:
            continue
        rows.append((n, rel))
    rows.sort(reverse=True)
    txt = "\n".join("- %s（%d 行）" % (rel, n) for n, rel in rows[:limit])
    return txt or "（扫不到源码文件）"


# ---------------------------------------------------------------- 模型调用

def _pick_model():
    """跟随当前配置的模型（不写死）。

    优先级：
      1. 设置里显式给了 `iter_agent_model`（用户想固定就用它）；
      2. 用户在前端选的**代码模型**（`code_model_choice`，非 auto）——
         自我迭代本来就是写代码，用户选了就照他选的；
      3. 其余：有 DeepSeek Key 走 `code_model_ds`（默认 deepseek-flash），
         没 Key 走本机 `default_model`（即当前对话用的本地模型）。
    """
    cfg = {}
    try:
        from . import config
        cfg = config.load_config() or {}
    except Exception:                                       # noqa: BLE001
        cfg = {}
    st = _settings()
    force = str(st.get("iter_agent_model") or "").strip()
    try:
        from . import keyvault
        key = keyvault.get_key() or ""
    except Exception:                                       # noqa: BLE001
        key = ""
    if force.startswith("local:"):
        return "local", force.split(":", 1)[1], key
    if force:
        return "cloud", force, key
    try:
        from . import tools as _T
        kind, model = _T.code_model_pick(cfg, {"ds_key": key})
    except Exception:                                       # noqa: BLE001
        kind, model = ("cloud", "deepseek-flash") if key else ("local", "")
    if kind == "local" and not model:
        model = str(cfg.get("default_model") or "")
    if kind == "cloud" and not key:
        kind, model = "local", str(cfg.get("default_model") or "")
    return kind, model, key


def _run_cloud(ctx, sys_prompt, task_prompt, steps, model, key, on_note=None):
    """云端：走 deepseek_client 自带的工具循环（它会自己回填 tool_calls）。"""
    from .deepseek_client import chat_stream_with_tools, DeepSeekError

    def _executor(name, args):
        return _exec(ctx, name, args)

    def _guard(used, final_text):
        # 「光说不做」兜底：没调过任何改文件的工具就想收工 → 退回一轮
        if ctx.finished:
            return None
        if not ({"write_file", "edit_file"} & set(used or ())):
            return ("（系统提示）你还没有真正改动任何文件。"
                    "请按需求动手改代码，改完跑 run_check(all)，再 finish。")
        if "run_check" not in set(used or ()):
            return ("（系统提示）你改了代码但没跑 run_check。"
                    "请先跑 run_check(kind=\"all\")，确认语法/导入/回归都正常再 finish。")
        return None

    # ★ 2026-11-17 用户要求：「一样需要流式输出思考过程」。
    #   原来这里写死 thinking=False，模型的思维链被完全丢掉 —— 界面上只能看到
    #   「工具 read_file 完成」这种结果，看不到它在想什么。
    #   但 thinking 增量是**碎片**（几个字一条），逐条 emit 会把事件队列刷爆
    #   （前端每 700ms 拉一次增量，一轮上万条会卡死）。所以攒够 160 字
    #   或隔了 0.35 秒才发一次，既流畅又不会淹掉队列。
    _buf = {"t": "", "ts": time.time()}

    def _flush(force=False):
        t = _buf["t"]
        if not t:
            return
        if not force and len(t) < 160 and (time.time() - _buf["ts"]) < 0.35:
            return
        ctx.emit(t, "think")
        _buf["t"] = ""
        _buf["ts"] = time.time()

    msgs = [{"role": "user", "content": task_prompt}]
    # ★ 2026-11-25：生成器先拿到变量里 —— 终止时要 close() 它才能立刻掐断上游；
    #   原来写成 `for ... in chat_stream_with_tools(...)`，终止分支里根本没有
    #   可关闭的对象（引用了不存在的 _gen，会 NameError 被 except 吞掉）。
    _gen = chat_stream_with_tools(
        msgs, model, key, _schemas(), _executor,
        max_rounds=max(4, steps), temperature=0.25, max_tokens=4096,
        thinking=True, system_prompt=sys_prompt, guard=_guard)
    for kind, txt in _gen:
        # 云端也是逐块吐的，每块都看一眼终止，别等整段生成完。
        if ctx.stop and ctx.stop():
            ctx.stopped = True
            try:
                _gen.close()
            except Exception:                           # noqa: BLE001
                pass
            raise _Cancelled("用户终止（生成中）")
        if kind == "thinking" or kind == "content":
            _buf["t"] += str(txt)
            _flush()
        elif kind == "tool_start":
            _flush(True)
            ctx.emit("准备调用 %s" % str(txt), "step")
        elif kind == "tool_result":
            _flush(True)
            name, ok, text = txt
            ctx.emit("工具 %s %s" % (name, "完成" if ok else "失败"), "step")
        elif kind == "note":
            _flush(True)
            if on_note:
                on_note(str(txt))
    _flush(True)


def _run_local(ctx, sys_prompt, task_prompt, steps, model, on_note=None):
    """本机 Ollama：自己实现工具循环（流式，边想边推给前端）。

    ★ 用户要求「流式输出思考过程」。原来这里是 `stream=False` 一轮一返，
      只有整轮结束才把 thinking 一次性 emit 出去，界面上看起来像卡住。
      现在改成 `stream=True`，逐块读 NDJSON：
        · 每个 chunk 里的 `message.thinking` → 攒进缓冲，按长度/时间节流
          通过 on_note 推给前端（和云端 _run_cloud 的节奏保持一致）；
        · `message.content` 同样处理（有些模型不吐 thinking 只吐 content）；
        · `message.tool_calls` 在流末尾才完整，攒齐后统一执行。
      拿不到流式（老版本 Ollama / 报错）时自动退回非流式，功能不丢。
    """
    from . import main as _main

    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": task_prompt}]

    # 思考片段的节流缓冲（与 _run_cloud 里同一套策略：攒够 160 字或隔 0.35 秒发一次）
    _buf = {"t": "", "ts": time.time()}

    def _flush(force=False):
        t = _buf["t"]
        if not t:
            return
        if not force and len(t) < 160 and (time.time() - _buf["ts"]) < 0.35:
            return
        if on_note:
            try:
                on_note(t)
            except Exception:                               # noqa: BLE001
                logger.debug("on_note 回调失败", exc_info=True)
        else:
            ctx.emit(t, "think")
        _buf["t"] = ""
        _buf["ts"] = time.time()

    def _push(text):
        if not text:
            return
        _buf["t"] += str(text)
        _flush()

    def _chat_once(stream):
        """跑一轮对话。stream=True 时逐块消费并把思考推出去，返回 (msg, calls)。"""
        resp = _main.client.chat(msgs, model=model, stream=stream,
                                 params={"temperature": 0.25, "max_tokens": 4096,
                                         "think": True})
        if not stream:
            data = resp.json() if hasattr(resp, "json") else resp
            msg = (data or {}).get("message") or {}
            _push(str(msg.get("thinking") or ""))
            _push(str(msg.get("content") or ""))
            _flush(True)
            return msg, (msg.get("tool_calls") or [])

        # --- 流式：逐行读 NDJSON ---
        think_acc, content_acc, calls = [], [], []
        try:
            for line in resp.iter_lines():
                # ★ 2026-11-25：模型还在生成时也能立刻停 —— 每块都问一次。
                #   原来只在「一次工具调用结束」时才检查，一次生成要几分钟，
                #   用户点了终止还得干等这一整段。
                if ctx.stop and ctx.stop():
                    ctx.stopped = True
                    try:
                        resp.close()
                    except Exception:                   # noqa: BLE001
                        pass
                    raise _Cancelled("用户终止（生成中）")
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:                           # noqa: BLE001
                    continue
                m = (obj or {}).get("message") or {}
                th = m.get("thinking")
                if th:
                    think_acc.append(str(th))
                    _push(str(th))
                ct = m.get("content")
                if ct:
                    content_acc.append(str(ct))
                    _push(str(ct))
                tc = m.get("tool_calls")
                if tc:
                    calls.extend(tc)
                if (obj or {}).get("done"):
                    break
        except _Cancelled:
            # 终止不是「流式失败」：绝不能退回非流式再跑一遍生成
            raise
        except Exception:                                   # noqa: BLE001
            logger.debug("流式读取中断，退回非流式", exc_info=True)
            _flush(True)
            return _chat_once(False)
        _flush(True)
        msg = {"thinking": "".join(think_acc), "content": "".join(content_acc)}
        return msg, calls

    for _ in range(max(4, steps)):
        # ★ 2026-11-25：每一轮开始前先看有没有被终止，别再多跑一轮生成。
        if ctx.stop and ctx.stop():
            ctx.stopped = True
            raise _Cancelled("用户终止（轮间）")
        over = ctx.over()
        if over and not ctx.finished:
            msgs.append({"role": "user", "content": "⏰ %s，请立刻调用 finish 收尾。" % over})
        try:
            msg, calls = _chat_once(True)
        except _Cancelled:
            # ★ 2026-11-25 实测抓到：这里原来 `except Exception` 会把终止异常
            #   当成「流式失败」→ 退回非流式**再完整生成一遍**，
            #   用户点了终止反而要多等一整段。终止必须原样往上抛。
            raise
        except Exception as e:                              # noqa: BLE001
            # 流式整体失败（比如服务端不支持 stream）→ 退回非流式再试一次
            logger.debug("流式对话失败，退回非流式：%s", e, exc_info=True)
            msg, calls = _chat_once(False)
        calls = msg.get("tool_calls") or []
        if not calls:
            # 没有工具调用 = 它想收工。没 finish 就催一次。
            if ctx.finished:
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or ""})
            msgs.append({"role": "user", "content": (
                "（系统提示）你还没有调用 finish。请继续：要么把活干完并 "
                "run_check(all)，要么 finish 说明为什么做不到。")})
            continue
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})
        for c in calls:
            fn = (c or {}).get("function") or {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:                           # noqa: BLE001
                    args = {}
            res = _exec(ctx, name, args if isinstance(args, dict) else {})
            msgs.append({"role": "tool", "content": str(res)[:12000]})
            if ctx.finished:
                break
        if ctx.finished:
            break


# ---------------------------------------------------------------- 可达性检查

_RE_ROUTE = re.compile(
    r"""@app\.(?:get|post|put|delete|patch|websocket)\(\s*["']([^"']+)["']""")


def _is_scratch(rel):
    """临时脚本：文件名以单下划线开头（`__init__.py` 这类双下划线不算）。"""
    name = str(rel).replace("\\", "/").rsplit("/", 1)[-1]
    if name.startswith("__"):
        return False
    return name.startswith("_")


def _new_api_routes(snap, changed):
    """本轮**新增**的后端路由（对比快照里的旧内容）。返回 [(rel, path)]。"""
    out = []
    for rel in changed:
        if not rel.endswith(".py"):
            continue
        new_txt = _read_text(rel)
        if "no-frontend" in new_txt:      # 文件里声明了「这是外部接口，不需要前端」
            continue
        src = snap["dir"] / rel.replace("/", "__")
        old_txt = ""
        try:
            if src.is_file():
                old_txt = src.read_text(encoding="utf-8", errors="replace")
        except Exception:                                    # noqa: BLE001
            old_txt = ""
        old_set = set(_RE_ROUTE.findall(old_txt))
        for path in _RE_ROUTE.findall(new_txt):
            if path not in old_set and path.startswith("/api/"):
                out.append((rel, path))
    return out


def _frontend_text():
    """把所有前端文件的正文拼起来，用于判断「新接口有没有调用方」。"""
    buf = []
    d = _ROOT / "frontend"
    if d.is_dir():
        for p in d.rglob("*"):
            if p.suffix.lower() in (".js", ".html", ".css", ".mjs"):
                try:
                    buf.append(p.read_text(encoding="utf-8", errors="replace"))
                except Exception:                            # noqa: BLE001
                    pass
    return "\n".join(buf)


def _fe_calls(path, fe):
    """前端到底有没有真的调用这个接口。

    ★ 2026-11-18 自查发现原来的判据是「路径前三段出现在前端就算有调用方」：
      `/api/canvas/command` 的键是 `/api/canvas`，于是同一模块下**再加**一个
      `/api/canvas/other` 也会被当成「已接上」—— 检查形同虚设。
      改成认引号里的**完整路径**（`"/api/canvas/command"`），
      外加一种明确的拼接写法：前端引号里正好是 `"/api/canvas/"`。
    """
    for q in ('"', "'", "`"):
        if q + path + q in fe:
            return True
    parent = path.rsplit("/", 1)[0] + "/" if "/" in path[1:] else path
    for q in ('"', "'", "`"):
        if q + parent + q in fe:
            return True
    return False


# ---------------------------------------------------------------- 独立复核

def _final_verify(ctx, snap):
    """系统自己的复核（不信模型自述）。返回 (ok, 原因, 细节)。

    ★ 聚合式复核：一次把**所有**问题查出来一起返回，而不是遇到第一个错就停。
      理由（2026-11-16 用户问「LLM 修完之后有没有反复验证再修复」）：
      短路式的话，模型改了 3 个文件、其中 2 个有语法错，每轮只报第一个；
      修好第一个 → 下一轮才发现第二个 → 再下一轮才发现导入起不来 →
      再下一轮才发现回归挂了。修复轮预算根本不够，最后被判失败整条回滚，
      前面几十步全白扔。聚合后一轮就能看到全部问题，一次修完。
    """
    mod, new, gone = _changed_files(snap)
    changed = mod + new
    if not changed and not gone:
        return False, "模型没有改动任何文件（它可能只是说了说）", {}

    # ★ 2026-11-18 用户问「自迭代的检验机制是不是有问题」——
    #   实测 f5_a1 改了 _tmp_plan.py / _tmp_probe.py / _tmp_wire.py 三个临时脚本，
    #   语法/导入/回归当然全过，于是被判「成功」，但产品里没有任何变化。
    if changed and all(_is_scratch(r) for r in changed):
        return False, ("只改动了临时脚本（%s），等于没做任何真实改动"
                       % "、".join(changed[:4])), {}

    # ① 护栏文件有没有被动过（包括用 shell 绕过去改的）
    #    硬违规：不进「修复轮」，直接回滚 —— 这种错不能靠改代码绕过去
    bad = [r for r, sha in (snap.get("guards") or {}).items()
           if _sha(_ROOT / r) != sha]
    if bad:
        return False, "护栏文件被修改（不允许）：%s" % "、".join(bad[:3]), {}

    probs = []      # 收集全部问题（不短路）
    detail = {}

    # ② 语法 —— 检查**所有**改动文件，逐个收集
    from . import self_iterate as _SI
    syn_bad = []
    for rel in changed:
        ok, msg = _SI._verify(rel)
        if not ok:
            syn_bad.append("  · %s：%s" % (rel, str(msg).strip()[:400]))
    if syn_bad:
        probs.append("【语法】%d 个文件编译不过：\n%s"
                     % (len(syn_bad), "\n".join(syn_bad[:8])))

    # ③ 能启动（backend.main 导入）
    #    语法不过时导入必然失败，重复报同一个根因只会干扰模型判断，所以跳过
    imp_bad = ""
    if not syn_bad:
        code = ("import sys;sys.path.insert(0,r'%s');import backend.main;print('IMPORT_OK')"
                % str(_ROOT))
        try:
            r = subprocess.run([sys.executable, "-c", code], cwd=str(_ROOT),
                               capture_output=True, timeout=240)
            o = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
            if "IMPORT_OK" not in o or r.returncode != 0:
                imp_bad = o[-1500:]
                probs.append("【导入】backend.main 起不来（程序会打不开）：\n" + imp_bad)
        except Exception as e:                              # noqa: BLE001
            imp_bad = "%s: %s" % (type(e).__name__, e)
            probs.append("【导入】检查跑不起来：%s" % imp_bad)

    # ④ 行为回归（只算「本次新增的失败」）—— 列出**全部**新增失败，不只前 4 条
    if not syn_bad and not imp_bad and _settings().get("regress_after_change", True):
        try:
            from . import regress as _RG
            # ★ 用户要求「弄完之后验证**所有功能**是否完全正常」——
            #   quick 只有 38 条快用例；full 会追加「全量自检无 high」+
            #   「全模块可导入」两项（约 10 秒）。这里用 full。
            rr = _RG.run_disk(scope="full")
            fails = {str(x.get("name")) for x in (rr.get("failed_items") or [])}
            new_fails = fails - (ctx.base_fails or set())
            detail["regress"] = {"passed": rr.get("passed"), "seconds": rr.get("seconds"),
                                 "new_fails": sorted(new_fails)}
            if new_fails and ctx.base_ok:
                probs.append("【回归】新增失败 %d 条（改动前这些是过的，说明你改坏了）：\n%s"
                             % (len(new_fails),
                                "\n".join("  · " + x for x in sorted(new_fails)[:12])))
        except Exception as e:                              # noqa: BLE001
            probs.append("【回归】跑不起来，无法确认改动安全：%s: %s"
                         % (type(e).__name__, e))

    # ⑤ 新功能自带验收断言
    act = ctx.action
    if act.get("origin") == "feature" and act.get("assert_file"):
        try:
            from . import selfevolve
            selfevolve.unpark_test(act.get("title"), path=(act.get("assert_file") or None))
            vr = selfevolve.verify_one(act.get("title"), path=(act.get("assert_file") or None))
            detail["assert"] = {"ok": bool(vr.get("ok")), "why": str(vr.get("why") or "")[:300]}
            if not vr.get("ok"):
                probs.append("【功能验收】断言没过：%s" % str(vr.get("why") or "")[:600])
        except Exception as e:                              # noqa: BLE001
            detail["assert"] = {"ok": False, "why": str(e)[:200]}

    # ⑥ 新接口有没有调用方
    #   ★ 2026-11-18 用户报「点了迭代说成功，功能却点不到」：
    #   f6 那轮新建 backend/canvas.py + POST /api/canvas/command，前端一处调用都没有，
    #   但语法/导入/回归全过 → 被判成功（日志里 assert=None，说明功能断言压根没跑）。
    #   用户侧看到的就是「这个功能不存在」。所以「有没有入口」必须算验收项。
    if not syn_bad:
        routes = _new_api_routes(snap, changed)
        if routes:
            fe = _frontend_text()
            orphan = [(r, p) for r, p in routes if not _fe_calls(p, fe)]
            if orphan:
                probs.append(
                    "【可达性】新增了 %d 个后端接口，但前端一处调用都没有"
                    "（用户在界面上点不到，等于没做）：\n%s\n"
                    "  要么在前端接上调用，要么在该接口所在文件里写注释 "
                    "`no-frontend` 声明它是给外部/脚本用的。"
                    % (len(orphan),
                       "\n".join("  · %s → %s" % (r, p) for r, p in orphan[:6])))
                detail["orphan_routes"] = [p for _r, p in orphan[:20]]

    if probs:
        return False, "\n\n".join(probs)[:4000], detail
    return True, "", detail


def _save_log(tag, payload):
    """把这一轮的完整过程落盘（用户要求「把改动和失败原因存下来给人看」）。"""
    try:
        p = _data("logs", "%s.json" % tag)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        md = _data("logs", "%s.md" % tag)
        lines = ["# 迭代执行记录 %s" % tag, "",
                 "- 结果：%s" % ("成功" if payload.get("ok") else "失败"),
                 "- 动作：%s（%s）" % (payload.get("title"), payload.get("action_id")),
                 "- 模型：%s" % payload.get("model"),
                 "- 步数 / 用时：%s 步 / %s 秒" % (payload.get("steps"), payload.get("seconds")),
                 "- 失败原因：%s" % (payload.get("error") or "（无）"),
                 "- 改动文件：%s" % ("、".join(payload.get("changed") or []) or "（无）"),
                 "", "## 工具调用"]
        for it in (payload.get("log") or [])[:200]:
            lines.append("%d. `%s` %s → %s" % (it.get("n"), it.get("tool"),
                                               json.dumps(it.get("args"), ensure_ascii=False)[:160],
                                               str(it.get("result"))[:200]))
        if payload.get("diff"):
            lines += ["", "## 改动 diff", "```diff", payload["diff"][:20000], "```"]
        md.write_text("\n".join(lines), encoding="utf-8")
        return str(p)
    except Exception:                                       # noqa: BLE001
        logger.debug("写执行日志失败", exc_info=True)
        return ""


# ---------------------------------------------------------------- 对外入口

def _run_once(plan, action, ev=None, stop=None, prev_fail="", attempt=0):
    """跑**一次**尝试。失败重试由外层 `run_action` 负责（用户要求
    「迭代受阻 LLM 要自己解决直到成功，不到万不得已不能失败」）。

    返回 dict：
      ok      是否成功（系统独立复核通过）
      error   失败原因（已回滚时带「已回滚」）
      changed 改动/新建的文件
      hunks   {rel: [{find, replace}]} 供前端代码卡片渲染
      log     过程日志路径
      steps / seconds / model
    """
    act = action or {}
    aid = str(act.get("id") or "?")
    st = _settings()
    if not st.get("iter_agent_enabled", True):
        return {"ok": False, "error": "「LLM 直接操作」已关闭（设置里打开 iter_agent_enabled）",
                "skipped": True}
    steps = int(st.get("iter_agent_steps") or 80)
    # ★ 2026-11-17：迭代是最费钱的一条路，单独打标签，记账里能一眼分开。
    try:
        from . import usage_ledger as _ul
        _ul.set_tag("iter")
    except Exception:                                       # noqa: BLE001
        pass
    seconds = int(st.get("iter_agent_seconds") or 1200)
    kind, model, key = _pick_model()
    if kind == "cloud" and not key:
        return {"ok": False, "error": "没有可用的 DeepSeek Key，无法调用云端模型"}

    # tag 带上尝试序号：每次重试都换一个快照目录 / 日志文件，
    # 否则第 2 次的快照会把第 1 次的覆盖掉，出问题就查不回去了。
    tag = "agent_%s_a%d_%s" % (aid, int(attempt), _now_tag())

    def _emit(text, k="step"):
        if ev:
            try:
                ev(k, text="%s %s" % (aid, text))
            except Exception:                               # noqa: BLE001
                logger.debug("事件失败", exc_info=True)
    if attempt:
        _emit("第 %d 次重试（上一轮失败已回滚，这次带着失败原因重做）" % (attempt + 1))
    _emit("准备快照（LLM 直接操作模式，最多 %d 步 / %d 分钟，模型 %s）"
          % (steps, max(1, seconds // 60), model))

    snap = _snapshot(tag)

    # 基线回归：先记下「本来就红的」断言，免得连坐
    base_fails, base_ok = set(), False
    if st.get("regress_after_change", True):
        try:
            from . import regress as _RG
            # 基线必须与最终复核**同一范围**（full）—— 否则 full 里那两项
            # 「全量自检 / 全模块导入」会被当成「本次新增失败」，把每条动作判死。
            b0 = _RG.run_disk(scope="full")
            bf = list(b0.get("failed_items") or [])
            if not any(str(x.get("name")) == "regress" for x in bf):
                base_fails = {str(x.get("name")) for x in bf}
                base_ok = True
        except Exception:                                   # noqa: BLE001
            logger.debug("基线回归不可用", exc_info=True)

    ctx = _Ctx(plan, act, ev, stop, steps, seconds, snap, base_fails, base_ok)
    _prev = ""
    if prev_fail:
        try:
            _prev = _PREV_FAIL.format(n=int(attempt) + 1, why=str(prev_fail)[:1200])
        except Exception:                                   # noqa: BLE001
            _prev = "\n## 上一次尝试失败原因\n%s\n" % str(prev_fail)[:1200]
    task_prompt = _TASK.format(
        title=act.get("title") or "",
        detail=(act.get("detail") or "")[:2000],
        note=act.get("note") or "（无）",
        files="、".join([f for f in (act.get("files") or []) if isinstance(f, str)]) or "（未指定，自己定位）",
        risk=act.get("risk") or "未标注",
        inventory=_inventory(),
        prev=_prev,
        steps=steps, minutes=max(1, seconds // 60))

    # ---------------------------------------------------------------
    # ★★ 2026-11-17 用户要求（核心，务必看清为什么这么写）：
    #   「如果遇到阻碍或哪里有问题时，不要直接失败回滚，LLM 需要和平时一样
    #     排除困难完成迭代」「LLM 必须发挥作用来排除错误以及迭代时遇到的
    #     困难和阻碍」「一般不到很严重的问题时绝对不能迭代失败」
    #
    #   原来的写法：模型跑完 → 复核没过 → **立刻整条回滚** → 外层从头再来。
    #   问题：模型永远看不到自己的报错，几十步的工作全白扔，语法错这种
    #   一分钟就能修好的事也会被判「失败」——这就是用户说的「遇到东西就失败回滚」。
    #
    #   现在：复核没过**不回滚**，把「系统复核的真实原因 + 自己改出来的 diff」
    #   一起喂回去，让它在**同一份改动上**修好，修完再复核。
    #   只有修复轮用尽仍然不行，才回滚（回滚是最后手段，不是第一反应）。
    #
    #   两层重试的分工（不要混淆）：
    #     · 内层「就地修复」（这里）：代码保持原样，看着真实报错修 —— 人的排查方式；
    #     · 外层「重试」（run_action）：整条回滚、换一个思路重来 —— 用来跳出死胡同。
    # ---------------------------------------------------------------
    try:
        repairs = int(st.get("iter_agent_repairs", 2))
    except Exception:                                       # noqa: BLE001
        repairs = 2
    repairs = max(0, min(5, repairs))

    err = ""
    ok, why, detail, diff = False, "", {}, ""
    changed, hunks = [], {}
    mod, new = [], []
    cancelled = False
    rounds = 0
    for rnd in range(repairs + 1):
        rounds = rnd + 1
        if rnd:
            # 修复轮：文件保持上一轮改过的样子（**不回滚**），把问题喂回去
            ctx.finished = False
            ctx.finish_args = {}
            ctx.last_check = ""
            # 修复轮额外给 15 步预算 —— 否则第一轮把步数用光后，连修都修不了
            try:
                ctx.steps = int(ctx.steps) + 15
            except Exception:                               # noqa: BLE001
                pass
            ctx.emit("第 %d 轮就地修复（不回滚，让它对着真实报错改）…" % rnd, "step")
            try:
                prompt = _REPAIR.format(
                    n=rnd, why=str(why or "")[:3000], diff=(diff or "")[:3000],
                    steps=max(6, int(ctx.steps) - ctx.n_calls),
                    minutes=max(1, int(max(0, ctx.seconds - ctx.elapsed())) // 60),
                    # ★ 2026-11-23：修复轮必须带着原始任务，否则模型只剩「你只说了没做」
                    #   这一句，根本不知道该做什么（实测 a5 就是这样空手失败的）。
                    task_title=str(act.get("title") or ""),
                    task_detail=str(act.get("detail") or "")[:2000],
                    task_files=("、".join([f for f in (act.get("files") or [])
                                          if isinstance(f, str)]) or "（未指定）"))
            except Exception:                               # noqa: BLE001
                prompt = ("原始任务：%s\n说明：%s\n\n系统复核没通过：%s\n"
                          "请照着原始任务就地修好，然后跑 "
                          "run_check(kind=\"all\") 再 finish。"
                          % (str(act.get("title") or "")[:200],
                             str(act.get("detail") or "")[:1500],
                             str(why)[:3000]))
        else:
            prompt = task_prompt

        err = ""
        try:
            _left = max(6, int(ctx.steps) - ctx.n_calls)
            if kind == "cloud":
                # on_note 原来定义了却没传进来 —— 客户端吐的「输出被截断，正在续写」
                # 这类过程提示全被丢掉，界面上看起来像卡住。现在接上。
                _run_cloud(ctx, _SYS, prompt, _left, model, key,
                           on_note=lambda s: ctx.emit(s, "step"))
            else:
                _run_local(ctx, _SYS, prompt, _left, model,
                           on_note=lambda s: ctx.emit(s, "think"))
        except _Cancelled:
            # ★ 2026-11-25：用户终止 → 立刻结束，不走「模型调用失败」分支，
            #   也不进外层重试（否则终止后还会自己再跑一轮）。
            cancelled = True
            err = ""
            break
        except Exception as e:                              # noqa: BLE001
            err = "模型调用失败：%s: %s" % (type(e).__name__, e)
            logger.warning("iter_agent 调用失败", exc_info=True)
            break

        # 用户点了终止
        try:
            cancelled = bool(stop and stop())
        except Exception:                                   # noqa: BLE001
            cancelled = False

        # 每轮都要重算：修复轮可能又改了别的文件、也可能把自己建的文件删了
        mod, new, gone = _changed_files(snap)
        changed = mod + new
        hunks = {}
        for rel in changed:
            old = ""
            src = snap["dir"] / rel.replace("/", "__")
            try:
                if src.is_file():
                    old = src.read_text(encoding="utf-8", errors="replace")
            except Exception:                               # noqa: BLE001
                old = ""
            hunks[rel] = _hunks(old, _read_text(rel))
        diff = _diff_text(snap, changed)

        ok, why, detail = False, err, {}
        # ★ 2026-11-25：已终止就别再跑复核（回归/自检几十秒到几分钟），
        #   直接进回滚收尾 —— 用户点终止要的是「马上停」。
        if cancelled:
            why = why or "用户终止"
        elif not err:
            ok, why, detail = _final_verify(ctx, snap)

        # ⑥ LLM 评审**真实 diff**。
        #   旧流程里的评审门禁（self_iterate._llm_review_action）在「生成改动之前」
        #   就跑，那时 action["diff"] 是空的 —— 它实际只能看标题和说明，
        #   「会不会误伤无关函数」根本判断不了。现在模型已经改完，拿真 diff 去评才有意义。
        #   fail-open：评审自身出错一律放行（它只是多一道把关，不能反过来卡死迭代）。
        if ok:
            try:
                from . import self_iterate as _SI2
                _a2 = dict(act)
                _a2["diff"] = diff[:4000]
                _a2["files"] = changed
                _rv = _SI2._llm_review_action(plan or {}, _a2)
                if not _rv.get("ok"):
                    ok = False
                    why = ("LLM 评审拒绝（针对真实 diff）：%s"
                           % str(_rv.get("reason") or "")[:200])
            except Exception:                               # noqa: BLE001
                logger.debug("评审失败（放行）", exc_info=True)

        if ok or cancelled:
            break
        # 护栏文件被改是**硬违规**，不能靠「修」绕过去 —— 直接回滚，不进修复轮
        if "护栏文件被修改" in str(why or ""):
            break
        if rnd < repairs:
            ctx.emit("复核没过（%s），进入第 %d 轮就地修复"
                     % (str(why or "")[:80], rnd + 1), "step")

    # 成功时留一份 self_iterate 格式的备份（供手动回滚 / 退化回滚使用）
    _btag = _compat_backup(snap, mod, new, tag) if ok else ""

    # 修复轮用尽仍不行 → 回滚（**最后手段**；用户要求回滚时把改动和原因存下来）
    rolled = []
    if not ok:
        if not why:
            why = "未通过系统复核"
        rolled = _restore(snap)
        why = why + "（已自动回滚：%s）" % ("、".join(rolled[:4]) or "无改动可回滚")
        if rounds > 1:
            why = why + "｜已就地修复 %d 轮仍未过" % (rounds - 1)

    payload = {
        "tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action_id": aid, "title": act.get("title"),
        "model": "%s:%s" % (kind, model), "ok": bool(ok), "error": why,
        "steps": ctx.n_calls, "seconds": round(ctx.elapsed(), 1),
        # ★ 2026-11-22 补：外层重试次数由 run_action 汇总，这里先留位，
        #   否则日志里 attempts 永远是 None，看不出「试了几次才成」。
        "attempts": None,
        "changed": changed, "rolled_back": rolled, "verify": detail,
        "summary": (ctx.finish_args or {}).get("summary") or "",
        "finished": ctx.finished, "cancelled": cancelled,
        "repairs": max(0, rounds - 1), "rounds": rounds,
        "log": ctx.log, "diff": diff,
    }
    log_path = _save_log(tag, payload)
    return {"ok": bool(ok), "error": why, "changed": changed, "hunks": hunks,
            "log": log_path, "steps": ctx.n_calls,
            "seconds": round(ctx.elapsed(), 1),
            "model": "%s:%s" % (kind, model), "backup_tag": _btag,
            "summary": payload["summary"], "rolled_back": rolled,
            "repairs": max(0, rounds - 1),
            "cancelled": cancelled, "diff": diff}


# ------------------------------------------------------------ 对外入口（带重试）

def run_action(plan, action, ev=None, stop=None):
    """让 LLM 直接动手做完这一条动作；**失败会自动重试**。

    用户要求（2026-11-17）：
      「如果说迭代受阻 LLM 需要解决这个问题直到成功迭代且完全正常，
        一般不到万不得已不能迭代失败」

    所以不再是「跑一次、失败就回滚收工」，而是**两层**：

      ① 内层「就地修复」（_run_once 里，iter_agent_repairs，默认 2 轮）：
         复核没过**不回滚**，把真实报错 + 自己的 diff 喂回去，
         让它在**同一份改动上**修好 —— 这是人排查 bug 的方式，
         语法错这种一分钟能修的事，不会再被判成「失败」。
      ② 外层「回滚重来」（这里，iter_agent_retries，默认 3 次，共 4 次尝试）：
         修复轮用尽仍不行，才整条回滚、带着失败原因换一个思路重做。
      ③ 用户点终止 / 总耗时超预算（iter_agent_total_seconds，默认 2400 秒）
         → 立刻停，不硬撑。

    为什么要有上限：模型在同一处反复失败，多半是需求本身做不到（或触及
    护栏文件）。无限重试只会烧 token、把代码反复改坏。所以设上限，但把
    **每一次**的失败原因都落盘（tries），便于人接手判断。
    """
    act = action or {}
    aid = str(act.get("id") or "?")
    st = _settings()
    if not st.get("iter_agent_enabled", True):
        return {"ok": False,
                "error": "「LLM 直接操作」已关闭（设置里打开 iter_agent_enabled）",
                "skipped": True}

    # ★ 用户要求「一般不到很严重的问题时绝对不能迭代失败，LLM 必须主动
    #   排除错误」。所以默认从 2 提到 3（最多共 4 次尝试）：每次失败都把
    #   系统复核的真实原因喂回去，让它针对性重做。上限仍在，避免在
    #   「需求本身做不到」时无限烧 token、把代码反复改坏。
    try:
        retries = int(st.get("iter_agent_retries", 3))
    except Exception:                                       # noqa: BLE001
        retries = 3
    retries = max(0, min(6, retries))
    try:
        total_budget = int(st.get("iter_agent_total_seconds") or 2400)
    except Exception:                                       # noqa: BLE001
        total_budget = 2400

    def _note(text):
        if ev:
            try:
                ev("step", text=text)
            except Exception:                               # noqa: BLE001
                logger.debug("事件失败", exc_info=True)

    t0 = time.time()
    prev, last, tries = "", {}, []
    for n in range(retries + 1):
        if n:
            try:
                if stop and stop():
                    _note("%s 已终止，不再重试" % aid)
                    break
            except Exception:                               # noqa: BLE001
                pass
            if total_budget > 0 and (time.time() - t0) > total_budget:
                _note("%s 总耗时超过 %d 秒，不再重试" % (aid, total_budget))
                break
            _note("%s 第 %d 次重试（上一轮：%s）" % (aid, n, prev[:100]))
        last = _run_once(plan, action, ev=ev, stop=stop,
                         prev_fail=prev, attempt=n) or {}
        tries.append({"attempt": n + 1, "ok": bool(last.get("ok")),
                      "error": str(last.get("error") or "")[:300]})
        if last.get("ok") or last.get("skipped") or last.get("cancelled"):
            break
        prev = str(last.get("error") or "")
        # 失败原因里带着「过程记录：路径」，重试时模型不该看到这句噪音
        if "｜过程记录：" in prev:
            prev = prev.split("｜过程记录：", 1)[0]

    if tries:
        last["tries"] = tries
        last["attempts"] = len(tries)
        # ★ 2026-11-22 补：日志文件是 _run_once 里写的，那时还不知道外层试了几次。
        #   这里回写，否则「试了 4 次才成」这件事在日志里完全看不到。
        _lp = str(last.get("log") or "")
        if _lp:
            try:
                with open(_lp, "r", encoding="utf-8") as _fh:
                    _lg = json.load(_fh)
                _lg["attempts"] = len(tries)
                _lg["tries"] = tries
                with open(_lp, "w", encoding="utf-8") as _fh:
                    json.dump(_lg, _fh, ensure_ascii=False, indent=1)
            except Exception:                               # noqa: BLE001
                logger.debug("回写重试次数失败", exc_info=True)
        if len(tries) > 1 and not last.get("ok"):
            _note("%s 连续 %d 次都没做成，已停在最后一次（每次都回滚了，代码是干净的）"
                  % (aid, len(tries)))
    return last
