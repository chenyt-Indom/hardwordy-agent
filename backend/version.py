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
import time
import json


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

def snapshot(message, allow_empty=False) -> dict:
    """把当前工作区提交成一个快照。没有改动时直接返回 skipped。"""
    av = available()
    if not av.get("ok"):
        return {"ok": False, "error": av.get("error")}
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
                    pass

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
