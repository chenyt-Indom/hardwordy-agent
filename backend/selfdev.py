# -*- coding: utf-8 -*-
"""自开发（self-dev）：让应用**改自己的源码**，实现自我迭代。

用户原话：「我需要智能体自己就能完成自我迭代……我不需要在你这里操作，
我在我的那个项目上就可以完成自我不断开发与更新。」

为什么单开一个模块而不是直接放开工件区的 write_file
--------------------------------------------------
工作区（workspace）是**沙箱**：模型只能在 data/workspace/N 里折腾，碰不到应用本身。
自我迭代要动的是 backend/*.py、frontend/*.js —— 那是"能把自己改坏"的目录，
所以这里做一套**带护栏**的写入口，而不是把 workspace 的根目录一改了事：

  1. **白名单根**：只能动 self_dev_root（默认项目根）下的文件；
     运行期数据目录（data/、sessions/、image_library/、models/、logs/）一律禁写。
  2. **每次写先备份**：data/self_backups/<时间戳>_<文件名>，可一键回滚。
  3. **写完立刻语法校验**：.py 走 ast.parse，.js 走 node --check。
     语法不过 → **不写盘**，把错误原样返回给模型去修。
  4. **重启前先试导入**：真的 import 一遍 backend.main，导入失败就**拒绝重启**。
     这是整套护栏里最关键的一步 —— 否则应用起不来，用户只能手动救。
  5. **留痕**：每次写/改/回滚都记到 data/selfdev_journal.jsonl。

边界：这不是版本控制，也不做代码审查。它的定位是"模型能自己把活干完"，
把「改坏 → 起不来 → 人工救」的概率压到最低，而不是保证零风险。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from . import config

# 运行期数据/二进制目录：绝对禁止模型改写（写坏了用户数据就没了）
DENY_DIRS = {
    "data", "sessions", "image_library", "models", "logs", "dist", "build",
    "node_modules", "__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
    "tools_dev", "self_backups",
}
DENY_EXT = {".onnx", ".pt", ".pth", ".bin", ".safetensors", ".db", ".sqlite",
            ".zip", ".7z", ".rar", ".exe", ".dll", ".pyd", ".so", ".log"}
MAX_READ = 400 * 1024          # 单文件读取上限 400KB
MAX_WRITE = 1200 * 1024        # 单文件写入上限 1.2MB


def project_root() -> str:
    """项目根目录（源码模式下就是 D://local-multimodal-src）。"""
    cfg = config.load_config() or {}
    r = str(cfg.get("self_dev_root") or "").strip()
    if r and os.path.isdir(r):
        return os.path.abspath(r)
    return os.path.abspath(getattr(config, "PROJECT_ROOT", os.getcwd()))


def enabled() -> bool:
    return bool((config.load_config() or {}).get("self_dev_enabled"))


# ⚠️⚠️ **这些文件禁止模型改**（2026-11-08 用户要求："务必确保回撤按钮的安全，
#     不能因为后面的自我迭代哪次出错而导致回撤不了"）。
#     理由：回撤能力本身如果被改坏，用户就再也退不回去了 —— 那是最坏的情况。
#     所以把"回撤机制自己"（本模块 + 独立恢复脚本）划成只读，
#     代价是以后要改 selfdev 本身得由人来改，这是刻意接受的取舍。
PROTECTED = {
    "backend/selfdev.py",
    "restore.py",
    "回滚.bat",
}

# 备份的**外部镜像**：放在项目目录之外，模型的所有工具都写不到这里。
# 只要项目外的这份还在，就算整个项目被改烂、应用起不来，也能靠它恢复。
def mirror_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "LocalMultimodalAssistant", "rollback")
    try:
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "_meta"), exist_ok=True)
    except Exception:
        pass
    return d


def backup_dir() -> str:
    """备份目录：data/self_backups/（和 doclib 一样放在 data 下）。

    ⚠️ 2026-11-08 修正：原来写 config.data("self_backups")，而 data() 只是拼
    data_root，于是备份落到了**项目根**（D://...//self_backups）—— 位置乱、
    还容易和源码目录混在一起。改到 data/ 下，并顺手迁移老目录里的内容。
    """
    d = config.data("data", "self_backups")
    legacy = os.path.join(getattr(config, "PROJECT_ROOT", ""), "self_backups")
    try:
        if os.path.isdir(legacy) and os.path.abspath(legacy) != os.path.abspath(d):
            os.makedirs(d, exist_ok=True)
            for fn in os.listdir(legacy):
                src = os.path.join(legacy, fn)
                dst = os.path.join(d, fn)
                if os.path.isfile(src) and not os.path.exists(dst):
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
            try:
                os.rmdir(legacy)          # 空了就删掉；不空留着无害
            except OSError:
                pass
    except Exception:
        pass
    os.makedirs(d, exist_ok=True)
    return d


def _journal_path() -> str:
    """改动留痕：data/selfdev_journal.jsonl（同样迁就一下老位置）。"""
    p = config.data("data", "selfdev_journal.jsonl")
    old = config.data("selfdev_journal.jsonl")
    try:
        if os.path.abspath(old) != os.path.abspath(p) and os.path.isfile(old) \
                and not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.replace(old, p)
    except Exception:
        pass
    return p


def journal(act: str, rel: str, **kw) -> None:
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "act": act, "rel": rel}
        rec.update(kw)
        with open(_journal_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_journal(n: int = 30) -> list:
    out = []
    try:
        with open(_journal_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-n:][::-1]


# ---------------------------------------------------------------- 独立回滚工具
def ensure_restore_kit() -> dict:
    """把独立回滚工具铺到**项目外**，保证应用起不来也能回撤。

    产物（都在 %LOCALAPPDATA%\\LocalMultimodalAssistant\\）：
      · rollback\\restore.py   —— 纯标准库脚本，不 import 项目里任何代码
      · rollback\\回滚.bat     —— 双击即用
      · 回滚到上一版.bat       —— 桌面便利入口（指向上面那个）
    """
    out = {"restore": "", "bat": "", "desktop": ""}
    try:
        src = os.path.join(project_root(), "restore.py")
        mdir = mirror_dir()
        dst = os.path.join(mdir, "restore.py")
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            out["restore"] = dst
    except Exception:
        pass

    # .bat 必须 GBK + CRLF（cmd 在 936 代码页下读 UTF-8 会乱码、路径含中文会失败）
    bat_lines = [
        "@echo off",
        "chcp 936 >nul",
        "title 回滚到上一个版本",
        'set "MM_PROJECT_ROOT=%s"' % project_root(),
        'cd /d "%%~dp0"',
        'set "PY="',
        'where py >nul 2>nul && set "PY=py -3"',
        'if not defined PY where python >nul 2>nul && set "PY=python"',
        'if not defined PY set "PY=%s"' % sys.executable,
        "%PY% restore.py",
        "pause",
    ]
    text = "\r\n".join(bat_lines) + "\r\n"
    for target in (os.path.join(mirror_dir(), "回滚.bat"),
                   os.path.join(os.path.expanduser("~"), "Desktop", "回滚到上一版.bat")):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(text.encode("gbk", "replace"))
            out["bat" if "Desktop" not in target else "desktop"] = target
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 路径护栏
class PathError(ValueError):
    pass


def safe_rel(rel: str) -> str:
    """把模型给的相对路径规范化，并做白名单校验。不合法就抛 PathError。"""
    r = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not r:
        raise PathError("路径不能为空")
    if ".." in r.split("/"):
        raise PathError("路径里不许出现 ..（只能改项目内的文件）")
    root = project_root()
    full = os.path.abspath(os.path.join(root, r))
    if not (full == root or full.startswith(root + os.sep)):
        raise PathError("越界：只能改 %s 目录下的文件" % root)
    first = r.split("/")[0]
    if first in DENY_DIRS:
        raise PathError("「%s」是运行期数据/二进制目录，禁止模型改写" % first)
    if r in PROTECTED:
        raise PathError("「%s」是回撤机制自身，禁止模型改写（改坏了就退不回来了）" % r)
    ext = os.path.splitext(full)[1].lower()
    if ext in DENY_EXT:
        raise PathError("这个扩展名（%s）是二进制/日志，不能直接写" % ext)
    return r


# ---------------------------------------------------------------- 读
def _humansize(n: int) -> str:
    for u in ("B", "KB", "MB"):
        if n < 1024:
            return "%d%s" % (n, u)
        n //= 1024
    return "%dGB" % n


def list_tree(rel: str = "", depth: int = 2) -> dict:
    """列出项目里**可改写**的文件（给模型看的目录树）。"""
    root_abs = os.path.abspath(project_root())
    base = root_abs
    if rel:
        try:
            base = os.path.join(root_abs, safe_rel(rel))
        except PathError as e:
            return {"ok": False, "error": str(e)}
    if not os.path.isdir(base):
        return {"ok": False, "error": "目录不存在：%s" % rel}
    lines = []

    def walk(d: str, lvl: int) -> None:
        try:
            items = sorted(os.listdir(d))
        except OSError:
            return
        for name in items:
            p = os.path.join(d, name)
            if os.path.isdir(p):
                if name in DENY_DIRS:
                    continue
                lines.append(os.path.relpath(p, root_abs).replace("\\", "/") + "/")
                if lvl > 1:
                    walk(p, lvl - 1)
            else:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                lines.append("%s  (%s)" % (
                    os.path.relpath(p, root_abs).replace("\\", "/"),
                    _humansize(sz)))

    walk(os.path.abspath(base), max(1, min(int(depth or 2), 4)))
    return {"ok": True, "root": root_abs, "files": lines[:400],
            "more": len(lines) > 400}


def read(rel: str) -> dict:
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    p = os.path.join(project_root(), r)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % r}
    if os.path.getsize(p) > MAX_READ:
        return {"ok": False,
                "error": "文件太大（>%dKB），请只读需要的部分" % (MAX_READ // 1024)}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    return {"ok": True, "rel": r, "path": p, "text": text,
            "chars": len(text), "lines": text.count("\n") + 1}


# ---------------------------------------------------------------- 校验
def _check_syntax(rel: str, content: str) -> str:
    """返回空串 = 通过；否则返回错误描述（**不写盘**）。"""
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".py":
        import ast
        try:
            ast.parse(content)
            return ""
        except SyntaxError as e:
            return ("Python 语法错误：第 %s 行 %s（`%s`）"
                    % (e.lineno, e.msg, (e.text or "").strip()[:80]))
    if ext == ".js":
        try:
            node = shutil.which("node")
            if not node:
                return ""
            tmp = os.path.join(backup_dir(), "_syntax_check.js")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            r = subprocess.run([node, "--check", tmp], capture_output=True,
                               text=True, timeout=30)
            if r.returncode != 0:
                return "JS 语法错误：" + (r.stderr or "")[:400]
        except Exception:
            return ""            # 校验器本身出问题不拦着
    if ext == ".json":
        try:
            json.loads(content)
        except Exception as e:
            return "JSON 不合法：%s" % e
    return ""


# 当前批次（一次"迭代"= 连续改动的一段时间窗口内的所有写入）
_BATCH = {"id": "", "ts": 0.0}
_KIT_DONE = {"v": False}
BATCH_WINDOW = 600.0          # 10 分钟内连续改的，算同一次迭代


def _batch_id() -> str:
    now = time.time()
    if _BATCH["id"] and (now - _BATCH["ts"]) < BATCH_WINDOW:
        _BATCH["ts"] = now
        return _BATCH["id"]
    _BATCH["id"] = time.strftime("%Y%m%d_%H%M%S")
    _BATCH["ts"] = now
    return _BATCH["id"]


def _meta_path(batch: str) -> str:
    return os.path.join(mirror_dir(), "_meta", batch + ".json")


def _update_meta(batch: str, rel: str, note: str = "", added: int = 0,
                 removed: int = 0, is_new: bool = False) -> None:
    """记录这一批"改了什么、为什么改"—— 前端要显示"版本更新内容"就靠它。"""
    p = _meta_path(batch)
    d = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    d.setdefault("batch", batch)
    d.setdefault("notes", [])
    d.setdefault("files", [])
    if note and note[:120] not in d["notes"]:
        d["notes"].append(note[:120])
    for f in d["files"]:
        if f.get("rel") == rel:
            f["added"] = int(f.get("added") or 0) + added
            f["removed"] = int(f.get("removed") or 0) + removed
            break
    else:
        d["files"].append({"rel": rel, "added": added, "removed": removed,
                           "is_new": bool(is_new)})
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _backup(rel: str, note: str = "") -> str:
    """在**改动之前**存一份快照：本地 data/self_backups + **项目外镜像**。

    ⚠️ 同一次迭代里重复改同一个文件时，**保留最早的快照**（不覆盖）——
      回撤要回到"这次迭代之前"，覆盖掉就没得退了。
    """
    p = os.path.join(project_root(), rel)
    if not os.path.isfile(p):
        return ""
    from urllib.parse import quote
    batch = _batch_id()
    fn = "%s_%s" % (batch, quote(rel, safe=""))
    dst = os.path.join(backup_dir(), fn)
    if not os.path.exists(dst):
        try:
            shutil.copy2(p, dst)
        except Exception:
            return ""
    # 镜像到项目外（模型所有工具都写不到那里）
    try:
        mdir = mirror_dir()
        mdst = os.path.join(mdir, fn)
        if not os.path.exists(mdst):
            shutil.copy2(dst, mdst)
    except Exception:
        pass
    return fn


def write(rel: str, content: str, note: str = "") -> dict:
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    if len(content.encode("utf-8")) > MAX_WRITE:
        return {"ok": False, "error": "内容太大（>%dKB），请拆成几次写"
                                      % (MAX_WRITE // 1024)}
    err = _check_syntax(r, content)
    if err:
        journal("write_rejected", r, err=err[:200])
        return {"ok": False, "error": err + "\n（没有写盘，改好再提交）"}
    before = _read_existing(r)
    bak = _backup(r, note)
    p = os.path.join(project_root(), r)
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        return {"ok": False, "error": "写盘失败：%s" % e}
    d = make_diff(before, content, r)
    _update_meta(_batch_id(), r, note, d["added"], d["removed"], not before)
    if not _KIT_DONE["v"]:
        try:
            ensure_restore_kit()
            _KIT_DONE["v"] = True
        except Exception:
            pass
    journal("write", r, chars=len(content), backup=bak, note=note[:120],
            added=d["added"], removed=d["removed"])
    return {"ok": True, "rel": r, "path": p, "chars": len(content), "backup": bak,
            "diff": d, "is_new": not before}


def patch(rel: str, find: str, replace: str, note: str = "") -> dict:
    """精确替换（和编辑器里的"替换"一样）。找不到 / 命中多处都会拒绝。"""
    got = read(rel)
    if not got.get("ok"):
        return got
    text = got["text"]
    n = text.count(find)
    if n == 0:
        return {"ok": False, "error": "没找到这段原文（空格/缩进/换行必须完全一致）"}
    if n > 1:
        return {"ok": False,
                "error": "这段原文出现了 %d 次，为免改错地方，请给更长的一段上下文" % n}
    out = write(rel, text.replace(find, replace, 1), note or "patch")
    return out


def rollback(rel: str, backup: str = "") -> dict:
    """回滚：用备份覆盖回去。

    ⚠️ **优先用项目外镜像**（2026-11-08）：本地 data/self_backups 有可能被
    用户的清理动作删掉，而项目外那份模型改不到、也删不到 —— 回撤必须留后手。
    """
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}

    def _resolve(name: str) -> str:
        bn = os.path.basename(name)
        for d in (mirror_dir(), backup_dir()):
            p2 = os.path.join(d, bn)
            if os.path.isfile(p2):
                return p2
        return ""

    if backup:
        src = _resolve(backup)
        if not src:
            return {"ok": False, "error": "备份文件不存在：" + os.path.basename(backup)}
    else:
        from urllib.parse import quote
        stem = quote(r, safe="")
        cands = []
        for d in (mirror_dir(), backup_dir()):
            try:
                cands += [f for f in os.listdir(d) if f.endswith("_" + stem)]
            except OSError:
                continue
        if not cands:
            return {"ok": False, "error": "没有找到这个文件的备份"}
        cands.sort(reverse=True)
        src = _resolve(cands[0])
        if not src:
            return {"ok": False, "error": "备份文件读取失败"}

    dst = os.path.join(project_root(), r)
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        return {"ok": False, "error": "回滚失败：%s" % e}
    journal("rollback", r, from_file=os.path.basename(src))
    return {"ok": True, "rel": r, "from": os.path.basename(src)}


# ---------------------------------------------------------------- 差异 / 历史
def _read_existing(rel: str) -> str:
    """读当前文件内容（不存在就当空）。"""
    p = os.path.join(project_root(), rel)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def make_diff(before: str, after: str, rel: str = "",
              max_lines: int = 240) -> dict:
    """生成**给人看的**统一 diff（前端折叠展示，不撑页面）。"""
    import difflib
    b = before.splitlines()
    a2 = after.splitlines()
    added = removed = 0
    lines = []
    for ln in difflib.unified_diff(b, a2, fromfile="改前", tofile="改后",
                                   lineterm="", n=3):
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
        lines.append(ln)
        if len(lines) >= max_lines:
            lines.append("…（差异过长已截断，完整内容见备份文件）")
            break
    text = "\n".join(lines)
    if len(text) > 20000:
        text = text[:20000] + "\n…（截断）"
    return {"rel": rel, "added": added, "removed": removed, "diff": text,
            "before_lines": len(b), "after_lines": len(a2)}


def _parse_backup_name(fn: str):
    """`20261108_170211_backend%2Ftools.py` → (batch, rel)。"""
    from urllib.parse import unquote
    if len(fn) < 17:
        return None
    batch = fn[:15]
    if not (batch[8] == "_" and batch.replace("_", "").isdigit()):
        return None
    if fn[15] != "_":
        return None
    try:
        return batch, unquote(fn[16:])
    except Exception:
        return None


def list_backups(limit: int = 200) -> list:
    """列出历史快照：**以项目外镜像为准**（本地删了也不影响回撤）。"""
    out = []
    seen = set()
    for d in (mirror_dir(), backup_dir()):
        try:
            names = sorted(os.listdir(d), reverse=True)
        except OSError:
            continue
        for fn in names:
            if fn in seen or fn.startswith("_"):
                continue
            if not os.path.isfile(os.path.join(d, fn)):
                continue
            pr = _parse_backup_name(fn)
            if not pr:
                continue
            seen.add(fn)
            p = os.path.join(d, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            out.append({"batch": pr[0], "rel": pr[1], "file": fn, "size": sz,
                        "path": p, "mirror": d == mirror_dir()})
            if len(out) >= limit:
                return out
    return out


def _load_meta(batch: str) -> dict:
    p = _meta_path(batch)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def list_batches(limit: int = 20) -> list:
    """按迭代批次聚合，带上**日期 + 这次改了什么**（前端版本历史靠它）。"""
    groups: dict = {}
    for b in list_backups(400):
        g = groups.setdefault(b["batch"], {"batch": b["batch"], "files": [],
                                           "size": 0})
        g["files"].append({"rel": b["rel"], "file": b["file"],
                           "size": b["size"]})
        g["size"] += b["size"]
    out = []
    for k in sorted(groups.keys(), reverse=True)[:limit]:
        g = groups[k]
        meta = _load_meta(k)
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.strptime(k, "%Y%m%d_%H%M%S"))
        except Exception:
            ts = k
        g["ts"] = ts
        g["notes"] = meta.get("notes") or []
        mf = {f.get("rel"): f for f in (meta.get("files") or [])}
        for f in g["files"]:
            m = mf.get(f["rel"]) or {}
            f["added"] = int(m.get("added") or 0)
            f["removed"] = int(m.get("removed") or 0)
        g["added"] = sum(f["added"] for f in g["files"])
        g["removed"] = sum(f["removed"] for f in g["files"])
        out.append(g)
    return out


def rollback_batch(batch: str) -> dict:
    """整批回撤：把这个迭代里改过的**所有**文件还原。"""
    items = [b for b in list_backups(600) if b["batch"] == batch]
    if not items:
        return {"ok": False, "error": "没有这个批次的备份：" + str(batch)}
    ok_n, failed = 0, []
    for it in items:
        r = rollback(it["rel"], it["file"])
        if r.get("ok"):
            ok_n += 1
        else:
            failed.append(it["rel"] + "：" + str(r.get("error"))[:60])
    journal("rollback_batch", batch, files=ok_n, failed=len(failed))
    return {"ok": failed == [] or ok_n > 0, "restored": ok_n,
            "failed": failed,
            "msg": "已回撤 %d 个文件" % ok_n + (
                "；%d 个失败" % len(failed) if failed else "")}


# ---------------------------------------------------------------- 跑 / 重启
def run_py(rel: str, args: str = "", timeout: int = 120) -> dict:
    """在项目根目录下跑一个 .py（用来验证自己刚写的脚本）。"""
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    if not r.endswith(".py"):
        return {"ok": False, "error": "只能直接运行 .py 文件"}
    p = os.path.join(project_root(), r)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % r}
    cmd = [sys.executable, p] + ([args] if args else [])
    t0 = time.time()
    try:
        r2 = subprocess.run(cmd, cwd=project_root(), capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "运行超时（%d 秒）" % timeout}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": r2.returncode == 0, "code": r2.returncode,
            "out": (r2.stdout or "")[-4000:], "err": (r2.stderr or "")[-4000:],
            "cost_s": round(time.time() - t0, 1)}


def verify_import() -> dict:
    """**重启前的试导入**：能不能 import backend.main。

    语法对不代表能启动（循环导入、少了函数、拼错常量都会在这里现形）。
    这一步过不了就**不许重启**，否则应用起不来，用户只能手动救。
    """
    code = ("import sys; sys.path.insert(0, %r); import backend.main; "
            "print('IMPORT_OK')" % project_root())
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=project_root(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except Exception as e:
        return {"ok": False, "error": "试导入超时/失败：%s" % e}
    ok = "IMPORT_OK" in (r.stdout or "")
    lines = (r.stderr or "").strip().splitlines() or [""]
    return {"ok": ok, "out": (r.stdout or "")[-800:],
            "err": "\n".join(lines[-6:])[-1200:]}


def restart(delay: float = 1.2) -> dict:
    """重启自己：先试导入，过了才拉新进程并退出本进程。"""
    chk = verify_import()
    if not chk.get("ok"):
        journal("restart_blocked", "-", err=(chk.get("err") or "")[:300])
        return {"ok": False,
                "error": "代码导入失败，**已拒绝重启**（改了会起不来）。先把错误修好：\n"
                         + (chk.get("err") or chk.get("error") or "")}

    root = project_root()
    entry = os.path.join(root, "run.py")
    frozen = getattr(sys, "frozen", False)
    if frozen:
        cmd = [sys.executable] + sys.argv[1:]
        cwd = os.path.dirname(sys.executable)
    else:
        if not os.path.isfile(entry):
            return {"ok": False, "error": "找不到启动入口 run.py"}
        cmd = [sys.executable, entry] + sys.argv[1:]
        cwd = root

    env = dict(os.environ)
    env["MM_SKIP_ALREADY_CHECK"] = "1"   # 端口还没释放，别把自己挡回去
    env["MM_WAIT_PORT"] = "1"            # 新进程先等端口空出来，别急着 bind

    # ⚠️ Windows 上要三件套（2026-11-08 实测：只给 DETACHED_PROCESS 时，
    #   父进程一退出、或外层工具把进程放进 Job，子进程会跟着被杀）：
    #     DETACHED_PROCESS        —— 不继承控制台
    #     CREATE_NEW_PROCESS_GROUP—— 独立进程组，Ctrl+C 不波及
    #     CREATE_BREAKAWAY_FROM_JOB —— 逃出父进程所在的 Job 对象
    #   最后一个在某些环境会直接报错（Job 不允许 breakaway），所以要有退路。
    flags_try = [0x00000008 | 0x00000200 | 0x01000000,   # 全套
                 0x00000008 | 0x00000200,                # 去掉 breakaway
                 0x00000008]                             # 最保守
    proc = None
    last_err = ""
    for fl in flags_try:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True,
                                    creationflags=fl if os.name == "nt" else 0)
            break
        except Exception as e:                        # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, e)
            proc = None
    if proc is None:
        return {"ok": False, "error": "拉不起新进程（%s），已保持当前进程继续运行"
                                      % last_err}

    # 关键：**先确认子进程活着，再退出自己**。
    # 以前是"先 os._exit(0)"，结果子进程一出生就死（Job 被连带清理），
    # 应用直接没了 —— 用户只看到"重启了一下就再也没起来"。
    time.sleep(max(1.0, delay + 1.0))
    if proc.poll() is not None:
        log = ""
        try:
            log = (proc.stdout.read() if proc.stdout else "")[-600:]
        except Exception:
            pass
        journal("restart_failed", "-", code=proc.returncode)
        return {"ok": False,
                "error": "新进程启动后立刻退出了（退出码 %s），**已保持当前进程继续运行**"
                         "（服务没断）。多半是端口没空出来或代码有问题。%s"
                         % (proc.returncode, ("\n" + log) if log else "")}

    journal("restart", "-", cmd=" ".join(cmd)[:200], pid=proc.pid)

    def _bye() -> None:
        time.sleep(0.3)
        os._exit(0)

    import threading
    threading.Thread(target=_bye, daemon=True).start()
    return {"ok": True,
            "msg": "已拉起新进程（PID %d）并通过导入校验，正在切换…"
                   "约 10 秒后刷新页面即可。" % proc.pid}
