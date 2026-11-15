# -*- coding: utf-8 -*-
"""版本快照与回滚（git 封装）。

背景（2026-11-10 用户提出）：
    自开发改了上百次代码，但一次都没进版本控制 —— 出问题只能靠散落的
    `.bak` 单文件备份手工拼，改坏一次就难回头。用户要求：

    1. 每次重启生效前**自动打一次快照**（只本地 commit，不 push）；
    2. 前端能看**版本列表**，知道每次改了哪些文件；
    3. 支持**整轮回滚**和**单文件回滚**两种粒度；
    4. 回滚前**必须自动建还原点**，手滑按错了也能回到回滚前。

设计要点（重要，改动前先读）：
    · 回滚**不用** `git reset --hard`。那样 HEAD 会后退、还原点变成游离
      commit，用户在列表里再也看不见它。
      这里改成：先把目标版本的文件**检出到工作区**，再 commit 一个
      「回滚到 xxx」的新提交。历史永远线性前进，还原点自然躺在历史里，
      「回到回滚前」= 再回滚到还原点那一条，同样安全。
    · 快照只提交源码（`data/` `sessions/` `image_library/` 等在 .gitignore
      里已排除），不会把聊天记录、图片库、模型权重带进版本库。
    · 所有 git 调用都带 `-c core.quotepath=false`，否则中文文件名会被转义
      成 \\346\\234\\254 这种，前端显示不了。
"""
from __future__ import annotations

import os
import subprocess
from .procwin import NOWIN as _NOWIN
import time
import json
import threading
import contextvars
import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 基础

def project_root() -> str:
    """项目根目录（和自开发工具用同一个根，避免两边不一致）。"""
    try:
        from . import selfdev
        return selfdev.project_root()
    except Exception:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(args, timeout=120, tries=3):
    """跑一条 git 命令，返回 (ok, 输出)。失败不抛异常，交给调用方判断。

    ⚠️ Windows 上连续快速起子进程偶尔会以 0xC0000142（DLL 初始化失败）退出，
    表现为「返回码非 0 且没有任何输出」——这不是 git 真的出错，等一下重跑即可。
    这里统一做 3 次重试，避免快照/回滚被这种偶发故障打断（2026-11-10 实测）。
    """
    cmd = ["git", "-c", "core.quotepath=false"] + [str(a) for a in args]
    text = ""
    r = None
    for attempt in range(max(1, tries)):
        try:
            r = subprocess.run(cmd, cwd=project_root(), capture_output=True,
            creationflags=_NOWIN,
                               timeout=timeout)
        except FileNotFoundError:
            return False, "本机没有安装 git，版本功能不可用"
        except subprocess.TimeoutExpired:
            return False, "git 命令超时（%s）" % " ".join(str(a) for a in args[:3])
        except Exception as e:  # noqa: BLE001
            return False, "git 命令失败：%s" % e
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        text = (out + ("\n" + err if err.strip() else "")).strip()
        if r.returncode == 0:
            return True, text
        if text.strip() and 0 < r.returncode < 128:
            return False, text          # 真正的业务失败（如 nothing to commit）
        time.sleep(0.4 * (attempt + 1))  # 进程没能正常起来 → 重试
    if not text.strip():
        text = "git 进程异常退出（返回码 %s），已重试 %d 次" % (
            getattr(r, "returncode", "?"), tries)
    return False, text


def available() -> dict:
    """版本功能是否可用（有 git、是仓库、有身份配置）。"""
    ok, out = _run(["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        return {"ok": False, "error": "当前目录不是 git 仓库：%s" % out[:200]}
    ok_n, name = _run(["config", "user.name"])
    if not (ok_n and name.strip()):  # 偶发进程起不来（0xC0000142）：重试一次
        time.sleep(0.4)
        ok_n, name = _run(["config", "user.name"])
    ok_e, email = _run(["config", "user.email"])
    if not (ok_e and email.strip()):
        time.sleep(0.4)
        ok_e, email = _run(["config", "user.email"])
    if not (ok_n and name.strip()) or not (ok_e and email.strip()):
        return {"ok": False,
                "error": "git 还没配 user.name / user.email，无法提交快照"}
    return {"ok": True, "root": project_root(),
            "user": name.strip(), "email": email.strip()}


def current() -> dict:
    """当前 HEAD 与是否有未提交改动。"""
    ok, head = _run(["rev-parse", "--short", "HEAD"])
    if not ok:
        return {"ok": False, "error": head}
    ok2, branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    ok3, dirty = _run(["status", "--porcelain"])
    files = [l for l in dirty.splitlines() if l.strip()] if ok3 else []
    return {"ok": True, "hash": head.strip(),
            "branch": branch.strip() if ok2 else "",
            "dirty": len(files), "dirty_files": files[:50]}


# ---------------------------------------------------------------- 快照

def snapshot(message, allow_empty=False, paths=None) -> dict:
    """把当前工作区提交成一个快照。没有改动时直接返回 skipped。

    paths 为空 = 提交工作区**全部**改动（重启前兜底、回滚后提交走这条）；
    paths 给了列表 = **只提交这些文件** —— 任务级提交必须走这条。
    为什么：`git add -A` 会把**另一个任务**（另一个群 / 单聊）正在改的文件
    一起提交走，于是"群任务的改动"混进了"单聊任务的版本"，
    回滚一个任务会把另一个任务也带走（2026-11-15 实测确认）。
    """
    _clear_stale_lock()
    av = available()
    if not av.get("ok"):
        return {"ok": False, "error": av.get("error")}
    only = [str(p) for p in (paths or []) if p]
    if only:
        ok, out = _run(["add", "--"] + only)
    else:
        ok, out = _run(["add", "-A"])
    if not ok:
        return {"ok": False, "error": "git add 失败：%s" % out[:300]}
    # 有东西待提交吗
    ok2, staged = _run(["diff", "--cached", "--name-only"])
    names = [l for l in staged.splitlines() if l.strip()] if ok2 else []
    if not names and not allow_empty:
        cur = current()
        return {"ok": True, "skipped": True, "reason": "没有新的改动，不需要快照",
                "hash": cur.get("hash"), "files": 0,
                "msg": "工作区没有改动，跳过"}
    msg = (message or "自迭代快照").strip()
    if not msg:
        msg = "自迭代快照"
    ok3, out3 = _run(["commit", "-m", msg])
    if not ok3:  # 偶发 index.lock 抢占：等一下重试一次
        time.sleep(0.5)
        ok3, out3 = _run(["commit", "-m", msg])
    if not ok3:
        return {"ok": False, "error": "git commit 失败：%s" % out3[:400]}
    ok4, h = _run(["rev-parse", "--short", "HEAD"])
    return {"ok": True, "skipped": False, "hash": h.strip() if ok4 else "",
            "files": len(names), "msg": msg,
            "names": names[:60]}


# ---------------------------------------------------------------- 列表 / 详情
# ---------------------------------------------------------------- 任务级提交
# 用户要求（2026-11-15）：**一个任务只产出一个版本**。
#
# 背景：以前 write/patch 每成功一次就 commit 一次。模型改一个涉及 5 个文件的
# 功能，版本列表里就冒出 5 条，同一个任务把列表刷屏，回滚时也分不清
# 「这是一次任务」还是「五个独立改动」。
#
# 现在改成两段式：
#   ① 工具每次改动只**登记**（stage）到 data/version_task.json，不 commit；
#   ② 任务结束时（模型说做完了 / 重启前 / 用户手动）一次性 commit 成一个版本，
#      说明里汇总「改了哪些文件 + 每文件增删行 + AI 自己写的小结」。
#   ③ 任务中途失败或被终止 → 照样提交，但标「（未完成）」，前端显示成灰色。
#
# 任务内的每一步都存档到 data/version_tasks/<hash>.json，
# 前端「详情」里可以精确退回某一步 —— 列表上仍然只显示任务版本。

_TASK_LOCK = threading.Lock()

# ★ 为什么要 owner：单聊和群聊（甚至两个群）可能**同时**在改代码。
#   如果共用一个池子，单聊任务结束时会把群里正在改的文件一起提交走，
#   回滚时就成了"一次任务"里混着两个不相干的东西。
#   owner 用 contextvars 传（群聊跑在独立线程里，会显式 set 一次）。
_OWNER = contextvars.ContextVar("version_owner", default="")


def set_owner(owner) -> None:
    """标记"当前正在跑的是哪个任务"（单聊：chat:<session>，群聊：hive:<gid>）。"""
    try:
        _OWNER.set(str(owner or ""))
    except Exception:
        logger.debug("silent-except", exc_info=True)


def get_owner() -> str:
    try:
        return _OWNER.get() or ""
    except Exception:
        return ""


def _task_path() -> str:
    """本次任务待提交的改动池（放 data/，不进版本库）。"""
    return os.path.join(project_root(), "data", "version_task.json")


def _task_archive_dir() -> str:
    """已提交任务的逐步记录（按版本号存档，供「详情里精细退回」）。"""
    return os.path.join(project_root(), "data", "version_tasks")


def _atomic_write_json(path, obj) -> None:
    """原子写：先写 .tmp 再 replace，避免半截 JSON 把任务池弄坏。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _task_read() -> dict:
    """读改动池。⚠️ 任何异常都必须返回**带 steps 的空结构** ——
    返回纯 {} 会让调用方 `d["steps"].append(...)` 直接 KeyError
    （2026-11-15 实测：池子文件还不存在时，第一次 stage 就崩）。
    """
    try:
        with open(_task_path(), encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return {"steps": []}
    if not isinstance(d, dict):
        return {"steps": []}
    if not isinstance(d.get("steps"), list):
        d["steps"] = []
    return d


def _task_write(d: dict) -> None:
    _atomic_write_json(_task_path(), d)


def _clear_stale_lock(max_age=90) -> bool:
    """清掉陈旧的 .git/index.lock。

    为什么需要：git 进程被强杀（或本应用重启时打断了一次 commit）会在仓库里
    留下 index.lock，之后**所有** git 操作都失败并报
    「Unable to create '...index.lock': File exists」——
    版本功能会整体瘫掉，用户只能手动去删。这里自动清理。
    只删「存在超过 max_age 秒」的，避免误删正在进行的 git 操作。
    """
    p = os.path.join(project_root(), ".git", "index.lock")
    try:
        if os.path.isfile(p) and (time.time() - os.path.getmtime(p)) > max_age:
            os.remove(p)
            return True
    except OSError:
        logger.debug("silent-except", exc_info=True)
    return False


def _syntax_check(rels=None) -> dict:
    """语法自检：这些 .py 还能不能编译。

    用户要求（2026-11-15）：回滚后如果代码是坏的、应用起不来，
    必须自动撤销回滚并说明原因 —— 否则用户会卡在"回滚完就启动失败"。
    rels 为空 = 检查 backend/*.py + 根目录 *.py（整轮回滚时用）。
    """
    import py_compile
    import tempfile
    root = project_root()
    targets = []
    if rels:
        for r in rels:
            r = str(r or "")
            if r.endswith(".py") and ".." not in r:
                targets.append(r)
    else:
        bdir = os.path.join(root, "backend")
        if os.path.isdir(bdir):
            for dirpath, dirnames, filenames in os.walk(bdir):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    if fn.endswith(".py"):
                        targets.append(os.path.relpath(os.path.join(dirpath, fn), root))
        try:
            for fn in os.listdir(root):
                if fn.endswith(".py"):
                    targets.append(fn)
        except OSError:
            logger.debug("silent-except", exc_info=True)
    tmpdir = tempfile.mkdtemp(prefix="verchk_")
    bad = []
    checked = 0
    for i, r in enumerate(targets[:400]):
        p = os.path.join(root, r)
        if not os.path.isfile(p):
            continue
        checked += 1
        try:
            py_compile.compile(p, cfile=os.path.join(tmpdir, "c%d.pyc" % i),
                               doraise=True)
        except Exception as e:  # noqa: BLE001
            bad.append({"rel": r, "error": str(e)[:200]})
    return {"ok": not bad, "checked": checked, "bad": bad[:5],
            "error": ("%s：%s" % (bad[0]["rel"], bad[0]["error"])) if bad else ""}


def stage(act, rel="", note="", backup="", added=0, removed=0, owner=None) -> dict:
    """登记一次源码改动（**不提交**）。

    任务结束时由 flush_task 合成一个版本。同一次任务里改同一个文件多次，
    每次都会记成一步（给「详情里精细退回」用），但版本列表只算一条。
    """
    own = get_owner() if owner is None else str(owner or "")
    with _TASK_LOCK:
        d = _task_read()
        d["steps"].append({
            "act": str(act or ""), "rel": str(rel or ""),
            "note": str(note or "")[:120], "backup": str(backup or ""),
            "added": int(added or 0), "removed": int(removed or 0),
            "owner": own,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.strftime("%H:%M:%S"),
        })
        try:
            _task_write(d)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "登记失败：%s" % e}
        mine = [s for s in d["steps"] if (s.get("owner") or "") == own]
        rels = []
        for s in mine:
            r = s.get("rel") or ""
            if r and r not in rels:
                rels.append(r)
        return {"ok": True, "staged": True, "owner": own,
                "files": len(rels), "steps": len(mine)}


def task_state(owner=None) -> dict:
    """本任务已经登记了多少改动（前端显示「待合并成一个版本」用）。"""
    own = get_owner() if owner is None else str(owner or "")
    d = _task_read()
    steps = [s for s in (d.get("steps") or [])
             if own == "*" or (s.get("owner") or "") == own]
    rels = []
    started = ""
    for s in steps:
        r = s.get("rel") or ""
        if r and r not in rels:
            rels.append(r)
        at = s.get("at") or ""
        if at and (not started or at < started):
            started = at
    return {"ok": True, "active": bool(steps), "owner": own,
            "started": started,
            "files": len(rels), "steps": len(steps), "rels": rels[:40]}


def flush_task(reason="任务结束", summary="", incomplete=False, owner=None) -> dict:
    """把本次任务登记的所有改动**合成一个版本**提交。

    incomplete=True → 说明里标「（未完成）」，前端显示成灰色、不算正式版本。
    owner 指定只提交哪个任务的改动（"*" = 全部）；其余任务的步骤留在池子里，
    不会被这次提交带走 —— 单聊和群聊可能同时在改代码。
    提交失败时**保留改动池**（下次再试），绝不把改动记录丢掉。
    """
    own = get_owner() if owner is None else str(owner or "")
    with _TASK_LOCK:
        _clear_stale_lock()
        d = _task_read()
        allsteps = d.get("steps") or []
        if own == "*":
            steps = list(allsteps)
            keep = []
        else:
            steps = [s for s in allsteps if (s.get("owner") or "") == own]
            keep = [s for s in allsteps if (s.get("owner") or "") != own]
        if not steps:
            return {"ok": True, "skipped": True, "reason": "本任务没有源码改动"}
        rels = []
        per = {}
        for s in steps:
            r = s.get("rel") or ""
            if not r:
                continue
            if r not in rels:
                rels.append(r)
            v = per.setdefault(r, {"acts": [], "added": 0, "removed": 0})
            a = s.get("act") or ""
            if a and a not in v["acts"]:
                v["acts"].append(a)
            v["added"] += int(s.get("added") or 0)
            v["removed"] += int(s.get("removed") or 0)
        if not rels:
            _task_write({"steps": keep})
            return {"ok": True, "skipped": True, "reason": "本任务没有源码改动"}

        # ---- 提交说明：第一行要能一眼看出「一个任务 = 一条版本」----
        head = "自迭代任务 · %d 个文件%s" % (
            len(rels), "（未完成）" if incomplete else "")
        lines = [head]
        sm = (summary or "").strip().replace("\r", "")
        if sm:
            lines.append("")
            lines.append(sm[:300])
        lines.append("")
        for r in rels[:40]:
            v = per.get(r) or {}
            lines.append("- %s（%s，+%d −%d）" % (
                r, "/".join(v.get("acts") or []) or "改动",
                int(v.get("added") or 0), int(v.get("removed") or 0)))
        if len(rels) > 40:
            lines.append("- …另有 %d 个文件" % (len(rels) - 40))
        msg = "\n".join(lines)

        # 只提交本任务涉及的文件 —— 别的任务正在改的文件不能被这次提交带走
        res = snapshot(msg, paths=rels)
        if not res.get("ok"):
            return {"ok": False, "error": str(res.get("error") or "")[:300],
                    "kept": True}
        h = res.get("hash") or ""
        # 存档：前端「详情」里据此精确退回某一步
        try:
            _started = ""
            for s in steps:
                at = s.get("at") or ""
                if at and (not _started or at < _started):
                    _started = at
            _atomic_write_json(
                os.path.join(_task_archive_dir(), "%s.json" % (h or "unknown")),
                {"task_id": d.get("task_id") or "", "owner": own,
                 "started": _started,
                 "ended": time.strftime("%Y-%m-%d %H:%M:%S"), "hash": h,
                 "reason": str(reason or "")[:120], "incomplete": bool(incomplete),
                 "summary": sm[:600], "steps": steps})
        except Exception:
            logger.debug("silent-except", exc_info=True)
        # 只清掉本任务的步骤（提交已成功，留着会导致下次重复提交同一批）；
        # 别的任务（另一个群 / 单聊）还在跑的步骤**原样保留**。
        try:
            _task_write({"steps": keep})
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return {"ok": True, "skipped": bool(res.get("skipped")), "hash": h,
                "files": len(rels), "steps": len(steps),
                "incomplete": bool(incomplete), "subject": head}


def steps_of(hash) -> dict:
    """某个任务版本内部的每一步（「详情」里展示 / 精细退回用）。"""
    if not hash or not _valid_ref(hash):
        return {"ok": False, "error": "版本号不合法", "steps": []}
    p = os.path.join(_task_archive_dir(), "%s.json" % hash)
    if not os.path.isfile(p):
        return {"ok": True, "steps": [],
                "note": "这个版本没有逐步记录（可能是重启前快照或手动快照）"}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "读逐步记录失败：%s" % e, "steps": []}
    out = []
    for i, s in enumerate(d.get("steps") or []):
        bak = str(s.get("backup") or "")
        cmt = str(s.get("commit") or "")
        out.append({"i": i, "act": s.get("act") or "",
                    "rel": s.get("rel") or "", "note": s.get("note") or "",
                    "ts": s.get("ts") or "",
                    "added": int(s.get("added") or 0),
                    "removed": int(s.get("removed") or 0),
                    # 能退回的两种依据：写盘前的 .bak 备份，或历史里对应的那一步提交
                    "has_backup": bool(bak and os.path.isfile(bak)),
                    "can_restore": bool((bak and os.path.isfile(bak))
                                        or (cmt and _valid_ref(cmt)))})
    return {"ok": True, "hash": hash, "summary": d.get("summary") or "",
            "incomplete": bool(d.get("incomplete")),
            "started": d.get("started") or "", "ended": d.get("ended") or "",
            "reason": d.get("reason") or "", "steps": out}


def restore_step(hash, idx) -> dict:
    """把某个任务里的第 idx 步**退回**（用那一步的备份还原文件）。

    这是「精细退回」，藏在详情里用；它本身会生成一个新版本，
    所以在版本列表里仍然看得见（可再退回来）。
    """
    if not hash or not _valid_ref(hash):
        return {"ok": False, "error": "版本号不合法"}
    p = os.path.join(_task_archive_dir(), "%s.json" % hash)
    if not os.path.isfile(p):
        return {"ok": False, "error": "找不到这个版本的逐步记录"}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "读逐步记录失败：%s" % e}
    steps = d.get("steps") or []
    try:
        idx = int(idx)
    except Exception:
        return {"ok": False, "error": "步骤号不合法"}
    if idx < 0 or idx >= len(steps):
        return {"ok": False, "error": "没有第 %d 步" % (idx + 1)}
    st = steps[idx] or {}
    rel = str(st.get("rel") or "")
    bak = str(st.get("backup") or "")
    cmt = str(st.get("commit") or "")
    if not rel or ".." in rel:
        return {"ok": False, "error": "这一步没有可还原的文件"}
    how = "用备份还原到这一步之前"
    if bak and os.path.isfile(bak):
        dst = os.path.join(project_root(), rel)
        try:
            with open(bak, "rb") as f:
                data = f.read()
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "还原失败：%s" % e}
    elif cmt and _valid_ref(cmt):
        # 没有 .bak（可能已被清理）→ 用历史里那一步提交**之前**的内容恢复。
        # 这比 .bak 更可靠：备份文件会被后续写入覆盖，提交不会。
        ok_c, out_c = _run(["checkout", "%s^" % cmt, "--", rel])
        if not ok_c:
            return {"ok": False, "error": "从历史提交恢复失败：%s" % out_c[:200]}
        how = "从历史提交还原到这一步之前"
    else:
        return {"ok": False, "error": "这一步没有备份也没有历史提交，无法精确退回；"
                                     "请改用「回滚」整个任务"}
    chk = _syntax_check([rel])
    if not chk.get("ok"):
        return {"ok": False,
                "error": "退回后语法有错，已中止（文件可能被改坏）：%s"
                         % chk.get("error")}
    # 登记这次还原：否则池子是空的，flush_task 会直接 skipped，不留下版本记录
    stage("restore", rel, "退回第 %d 步" % (idx + 1), "", 0, 0)
    r = flush_task("手动退回「%s」第 %d 步" % (hash, idx + 1),
                   summary="退回 %s 到第 %d 步之前（%s）" % (rel, idx + 1, how))
    return {"ok": True, "rel": rel, "how": how, "version": r,
            "msg": "已把 %s %s。重启后生效。" % (rel, how)}



def count() -> int:
    """仓库里一共有多少个版本（前端显示「当前版本 vN」用）。"""
    ok, out = _run(["rev-list", "--count", "HEAD"])
    try:
        return int(out.strip()) if ok else 0
    except Exception:
        return 0


def list_versions(limit=30) -> dict:
    """版本列表（最新在前）。每条带改了几个文件。"""
    av = available()
    if not av.get("ok"):
        return {"ok": False, "error": av.get("error"), "items": []}
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 30
    # 一次调用把「提交头 + 文件清单」全拿回来。
    # 逐条 git show 太慢：30 个版本要 30 次子进程，冷启动能卡三秒。
    # 用 SEP 分隔字段、用 \x01 标记提交头，避免提交信息里的符号干扰解析。
    SEP = chr(31)
    fmt = "%x01%H" + SEP + "%h" + SEP + "%at" + SEP + "%s"
    ok, out = _run(["log", "-n", str(limit), "--pretty=format:" + fmt,
                    "--name-only"])
    if not ok:
        return {"ok": False, "error": out[:300], "items": []}
    items = []
    cur = None
    for line in out.splitlines():
        if line.startswith("\x01"):
            parts = line[1:].split(SEP)
            if len(parts) < 4:
                cur = None
                continue
            full, short, ts, subject = parts[0], parts[1], parts[2], parts[3]
            try:
                t = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
            except Exception:
                t = ""
            cur = {"hash": short, "full": full, "time": t,
                   "subject": subject, "files": 0}
            items.append(cur)
        elif cur is not None and line.strip():
            cur["files"] += 1
    ok_c, cnt = _run(["rev-list", "--count", "HEAD"])
    try:
        total = int(cnt.strip())
    except Exception:
        total = len(items)
    cur = current()
    return {"ok": True, "items": items, "current": cur, "total": total}


def detail(hash) -> dict:
    """某个版本改了哪些文件（状态 + 增删行数）。"""
    if not hash or not _valid_ref(hash):
        return {"ok": False, "error": "版本号不合法"}
    ok, out = _run(["show", "--numstat", "--pretty=format:%H%x1f%s", hash])
    if not ok:
        return {"ok": False, "error": out[:300]}
    lines = out.splitlines()
    head = lines[0] if lines else ""
    subject = head.split("\x1f")[-1] if "\x1f" in head else ""
    files = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, rel = parts[0], parts[1], parts[2]
        files.append({
            "rel": rel,
            "added": 0 if add == "-" else int(add or 0),
            "deleted": 0 if dele == "-" else int(dele or 0),
            "binary": add == "-",
        })
    return {"ok": True, "hash": hash, "subject": subject, "files": files}


def file_diff(hash, rel) -> dict:
    """某版本里某个文件的具体差异（给前端折叠展示）。"""
    if not hash or not _valid_ref(hash):
        return {"ok": False, "error": "版本号不合法"}
    if not rel or ".." in rel:
        return {"ok": False, "error": "文件路径不合法"}
    ok, out = _run(["show", "--format=", hash, "--", rel])
    if not ok:
        return {"ok": False, "error": out[:300]}
    return {"ok": True, "hash": hash, "rel": rel, "diff": out[:40000]}


# ---------------------------------------------------------------- 回滚
def _undo_path() -> str:
    """「上一次回滚」的记录文件（放在 data/，不进版本库）。"""
    return os.path.join(project_root(), "data", "version_undo.json")


def save_undo(restore_hash, target="", note="") -> dict:
    """记住「回滚前的还原点」，前端据此提供「撤销这次回滚」。"""
    if not restore_hash:
        return {"ok": False, "error": "没有还原点"}
    rec = {"restore_hash": str(restore_hash), "target": str(target)[:40],
           "note": str(note)[:120], "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        p = _undo_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        return {"ok": True, "undo": rec}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "写还原点记录失败：%s" % e}


def get_undo() -> dict:
    """上一次回滚的还原点信息（没有就 ok=False）。"""
    try:
        with open(_undo_path(), encoding="utf-8") as f:
            rec = json.load(f) or {}
    except Exception:
        return {"ok": False}
    if not rec.get("restore_hash"):
        return {"ok": False}
    ok, _ = _run(["rev-parse", "--verify",
                  "%s^{commit}" % rec["restore_hash"]])
    if not ok:
        return {"ok": False, "error": "还原点已不在仓库里"}
    return {"ok": True, "undo": rec}



def _valid_ref(ref) -> bool:
    """只允许 hash / HEAD / 分支名这类安全字符，防止拼出奇怪的命令。"""
    if not ref or len(str(ref)) > 80:
        return False
    s = str(ref)
    return all(c.isalnum() or c in "-_./" for c in s)


def rollback(hash, files=None, note="") -> dict:
    """回滚到某个版本。

    files 为空 = 整轮回滚；给了文件列表 = 只回滚这几个文件。
    回滚前一定先打一个还原点，返回它的 hash，前端可据此「撤销回滚」。
    """
    av = available()
    if not av.get("ok"):
        return {"ok": False, "error": av.get("error")}
    if not hash or not _valid_ref(hash):
        return {"ok": False, "error": "版本号不合法"}
    ok, real = _run(["rev-parse", "--verify", "%s^{commit}" % hash])
    if not ok:
        return {"ok": False, "error": "找不到这个版本：%s" % hash}

    # 1) 还原点（回滚前把当前状态存下来）
    rp = snapshot("还原点：回滚到 %s 之前自动保存" % str(hash)[:7])
    if not rp.get("ok"):
        return {"ok": False, "error": "建还原点失败，已中止回滚：%s" % rp.get("error")}
    restore_hash = rp.get("hash") or ""
    # 记下还原点：前端「撤销这次回滚」用它，刷新页面也不丢
    save_undo(restore_hash, target=str(hash)[:7], note=note or "")

    # 2) 把目标版本的文件检出到工作区
    targets = [str(f) for f in (files or []) if f]
    restored = []
    if targets:
        ok2, out2 = _run(["checkout", str(hash), "--"] + targets)
        if not ok2:
            return {"ok": False, "restore_hash": restore_hash,
                    "error": "检出文件失败：%s" % out2[:300]}
        restored = targets
    else:
        ok2, out2 = _run(["checkout", str(hash), "--", "."])
        if not ok2:
            return {"ok": False, "restore_hash": restore_hash,
                    "error": "整轮检出失败：%s" % out2[:300]}
        # 该版本里不存在、但当前存在的文件要删掉，否则「回滚」不彻底
        ok3, added = _run(["diff", "--name-only", "--diff-filter=A",
                           str(hash), "HEAD"])
        if ok3:
            for rel in added.splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                p = os.path.join(project_root(), rel)
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        restored.append("删除 " + rel)
                except OSError:
                    logger.debug("silent-except", exc_info=True)

    # 2.5) 自检：回滚后的 .py 必须还能编译。
    #      用户要求（2026-11-15）：回滚到坏代码会导致应用起不来，
    #      必须**自动撤销**这次回滚并说明原因，不能把人晾在"启动失败"上。
    _chk = _syntax_check(targets if targets else None)
    if not _chk.get("ok"):
        undone = False
        if restore_hash:
            ok_u, _out_u = _run(["checkout", str(restore_hash), "--", "."])
            undone = bool(ok_u)
        return {"ok": False, "restore_hash": restore_hash,
                "auto_undone": undone,
                "error": ("回滚后的代码语法有错（%s）。已自动撤销这次回滚，"
                          "当前仍是回滚前的代码。%s"
                          % (_chk.get("error"),
                             "" if undone else
                             "⚠️ 自动撤销也失败了，请点「↩ 撤销上次回撤」手动恢复。"))}

    # 3) 把这次回滚也提交成一个版本，历史保持线性
    tag = "整轮" if not targets else "%d 个文件" % len(targets)
    cm = snapshot("回滚到 %s（%s）%s" % (str(hash)[:7], tag,
                                        (" · " + note) if note else ""))
    if not cm.get("ok"):
        return {"ok": False, "restore_hash": restore_hash,
                "error": "回滚已落到工作区，但提交失败：%s" % cm.get("error")}

    return {"ok": True,
            "target": str(hash)[:7],
            "restore_hash": restore_hash,
            "rollback_hash": cm.get("hash"),
            "files": restored[:60],
            "count": len(restored),
            "msg": ("已回滚到 %s（%s）。还原点 %s，点「撤销回滚」可恢复。"
                    % (str(hash)[:7], tag, restore_hash or "-"))}
