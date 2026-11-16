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

try:
    from . import config
    from . import actlog
except ImportError:  # 允许作为顶层模块被直接导入（如验收断言）
    import config
    import actlog
import logging
logger = logging.getLogger(__name__)

# 运行期数据/二进制目录：绝对禁止模型改写（写坏了用户数据就没了）
DENY_DIRS = {
    "data", "sessions", "image_library", "models", "logs", "dist", "build",
    "node_modules", "__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
    "tools_dev", "self_backups",
}
DENY_EXT = {".onnx", ".pt", ".pth", ".bin", ".safetensors", ".db", ".sqlite",
            ".zip", ".7z", ".rar", ".exe", ".dll", ".pyd", ".so", ".log"}
MAX_READ = 4 * 1024 * 1024     # 单文件读取上限 4MB（2026-11-09 从 400KB 放开）
MAX_WRITE = 8 * 1024 * 1024    # 单文件写入上限 8MB（2026-11-09 从 1.2MB 放开）


# ===== 🔒 自迭代禁区（2026-11-11 用户硬性要求）=====
# 为什么要有：
#   有几段代码被自迭代**反复改坏**，而它们的失效症状都是"界面一片空白 / 一直转圈"，
#   极难排查（错误本身也被吞掉了）。用户明确要求：把这几处**锁死**，不许再改。
#
# 机制：
#   文件里用 `/* 🔒LOCK:名字 ... */` … `/* 🔒END:名字 */` 圈出区块。
#   自迭代写入时逐字比对；**区块内容变了、或标记被删 → 直接拒绝写盘**。
#   只锁区块本身，文件其余部分照常可以改，不影响正常自迭代能力。
#
# 目前锁住的两处（都在 frontend/app.js）：
#   stream-error-order   —— 流式事件里 error 必须排在 __end 之前。
#                            顺序反了 → 后端所有 {error,__end} 事件被整条吞掉 → 界面全白。
#   default-model-ready  —— 「本地·自动」的通道判定必须先等 __defaultModel 就绪。
#                            没等 → 误判成本地通道 → 后端拒绝 → 同样全白。
_LOCKED_BLOCKS = {
    "frontend/app.js": [
        ("/* 🔒LOCK:stream-error-order", "/* 🔒END:stream-error-order */"),
        ("/* 🔒LOCK:default-model-ready", "/* 🔒END:default-model-ready */"),
    ],
}


def _extract_locked(text: str, begin: str, end: str):
    """取出 begin..end 之间的原文（含两端标记）；任一端缺失返回 None。"""
    i = (text or "").find(begin)
    if i < 0:
        return None
    j = text.find(end, i)
    if j < 0:
        return None
    return text[i:j + len(end)]


def _extract_all_locked(text: str, begin: str, end: str) -> list:
    """取出**所有**同名区块（按出现顺序），每项为含两端标记的原文。

    ⚠️⚠️ 2026-11-11 修：原来只用 `.find()` 取第一处，于是同一个名字在文件里
    出现两次时，**第二处完全不受保护**。实测 `frontend/app.js` 的
    `default-model-ready` 就在两处（初始化那处 + doSend 里的通道判定），
    `doSend` 那处正是最关键的，却被静默放过（篡改不报错）。
    现在改成逐处收全，数量不对 / 任何一处内容变了都拒绝。
    某处有 LOCK 却没有对应 END 时，该项为 None（视为该区块已破损）。
    """
    out = []
    s = text or ""
    i = 0
    while True:
        i = s.find(begin, i)
        if i < 0:
            return out
        j = s.find(end, i)
        if j < 0:
            out.append(None)          # 有头无尾
            return out
        out.append(s[i:j + len(end)])
        i = j + len(end)


def _check_locked_blocks(rel: str, old: str, new: str) -> str:
    """新内容必须**逐字保留**所有锁定区块（同名区块的**每一处**都要保留）。

    返回空串 = 通过；否则返回给模型看的拒绝原因。
    磁盘上本来就没有这一段时跳过（不拦首次添加）。
    """
    for b, e in _LOCKED_BLOCKS.get(rel, ()):
        olds = _extract_all_locked(old, b, e)
        if not olds:
            continue
        news = _extract_all_locked(new, b, e)
        if len(news) < len(olds):
            return ("拒绝：**锁定区被删除或合并**（%s）。\n"
                    "磁盘上有 %d 处，提交的内容里只剩 %d 处。\n"
                    "这几处是用户要求的硬性保护，删掉会让界面直接变空白、且极难排查。\n"
                    "请原样保留每一处（从 LOCK 到 END），只改区块外面的代码。"
                    % (b, len(olds), len(news)))
        for k, a in enumerate(olds, 1):
            if a is None:             # 磁盘上本来就是破损的（有头无尾），无从比对
                continue
            c = news[k - 1]
            if c is None:
                return ("拒绝：第 %d 处锁定区少了 END 标记（%s）。\n"
                        "请原样保留整个区块（从 LOCK 到 END），只改区块外面的代码。"
                        % (k, b))
            if c != a:
                return ("拒绝：第 %d 处锁定区**不允许修改**（%s）。\n"
                        "它是用户要求的硬性保护，改了会让界面直接变空白、且极难排查。\n"
                        "如需调整，请先在回答里说明改动原因，由用户手动解除锁定后再改。"
                        % (k, b))
    return ""


def _check_failure_evidence(fail_kind: str, days: int = 7) -> dict:
    """改动前校验「这个失败真的发生过」（2026-11-17 改动证据卡）。

    对应 weakness「幽灵修复」：模型幻觉出一个不存在的失败，然后加护栏去修它。
    做法：从 actlog 聚合该 fail_kind 近 days 天的真实计数，为 0 就拒绝并记 phantom。

    返回 dict：
      ok        True=放行（计数>0，或账本读不出来时保守放行）
      count     近 days 天命中条数
      phantom   True=判定为幽灵修复（账本可读且计数为 0）
      reason    拒绝原因（ok=False 时非空）
      evidence  给改动卡片展示的一句话证据
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return {"ok": True, "count": 0, "phantom": False, "reason": "",
                "evidence": ""}
    try:
        n, readable = actlog.count_fail_kind_ex(fk, days=days)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        n, readable = 0, False
    evidence = "针对 fail_kind: %s，近 %d 天 %d 次" % (fk, int(days), int(n))
    if not readable:
        # 账本坏了不能挡住正常改动 —— 保守放行，但证据里说明查不到
        return {"ok": True, "count": 0, "phantom": False, "reason": "",
                "evidence": evidence + "（账本不可读，未拦截）"}
    if int(n) <= 0:
        reason = ("拒绝：fail_kind「%s」近 %d 天真实发生 0 次，属于幽灵修复。\n"
                  "请先确认这个失败真的出现过（actlog / task.json 里有记录），"
                  "再动手改代码。" % (fk, int(days)))
        try:
            journal("phantom", "", fail_kind=fk, days=int(days), count=0)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return {"ok": False, "count": 0, "phantom": True, "reason": reason,
                "evidence": evidence}
    return {"ok": True, "count": int(n), "phantom": False, "reason": "",
            "evidence": evidence}


def _require_real_failure(fail_kind: str, days: int = 7) -> dict:
    """patch 入口的**硬前置**：fail_kind 必须近 days 天真实发生过。

    为什么单独再包一层（2026-11-17 用户要求「不再依赖模型自觉，而是硬前置」）：
      过去 3 次立项都栽在「模型自己觉得该修，就编一个 fail_kind 去修」上。
      光靠提示词/自觉没用 —— 必须在**入口**用账本硬拦：
        · fail_kind 为空 → 放行（不是针对某个失败的修复，无需证据）；
        · 账本可读且计数为 0 → 返回 {'ok': False, 'reason': 'phantom'}，
          并写一条 phantom 记录（journal 里 act=phantom）；
        · 账本读不出来 → 保守放行（不能因为账本坏了挡住正常改动）。

    返回 dict（与 _check_failure_evidence 同构，额外带 reason_code）：
      ok          True=放行
      reason      'phantom' / ''（给调用方做机器判定用）
      reason_text 给模型看的中文拒绝原因
      count       近 days 天命中条数
      evidence    一句话证据
    """
    fk = str(fail_kind or "").strip()
    if not fk:
        return {"ok": True, "reason": "", "reason_text": "", "count": 0,
                "evidence": ""}
    # ★ 2026-11-17 幽灵修复闸门（用户要求「改动前必须从 actlog 取真实 fail_kind 计数」）：
    #   这里**直接**从 actlog 聚合真实计数，不再经过 _check_failure_evidence 的
    #   「账本不可读就放行」分支 —— 入口硬前置必须拿到真实数字：
    #     · 账本可读且计数 > 0 → 放行，evidence 带上真实次数；
    #     · 账本可读且计数 == 0 → 拒绝（phantom），并写一条 phantom 记录；
    #     · 账本读不出来 → 保守放行（不能因为账本坏了挡住正常改动），
    #       但 evidence 里明确标注「账本不可读，未拦截」，让前端卡片能看出来。
    #   上次「改动证据卡」失败是因为给未定义的 count 赋常量；这次改为从 actlog
    #   真实聚合，且计数为 0 直接拒绝，不再占位。
    try:
        n, readable = actlog.count_fail_kind_ex(fk, days=days)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        n, readable = 0, False
    n = int(n or 0)
    evidence = "针对 fail_kind: %s，近 %d 天 %d 次" % (fk, int(days), n)
    if not readable:
        # 账本坏了不能挡住正常改动 —— 保守放行，但证据里说明查不到
        return {"ok": True, "reason": "", "reason_text": "", "count": 0,
                "evidence": evidence + "（账本不可读，未拦截）"}
    if n <= 0:
        # 幽灵修复：计数为 0 直接拒绝，并写 phantom 记录（act=phantom）
        reason_text = ("拒绝：fail_kind「%s」近 %d 天真实发生 0 次，属于幽灵修复。\n"
                       "请先确认这个失败真的出现过（actlog / task.json 里有记录），"
                       "再动手改代码。" % (fk, int(days)))
        try:
            journal("phantom", "", fail_kind=fk, days=int(days), count=0)
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return {"ok": False, "reason": "phantom",
                "reason_text": reason_text,
                "count": 0,
                "evidence": evidence}
    return {"ok": True, "reason": "", "reason_text": "",
            "count": n,
            "evidence": evidence}


def locked() -> bool:
    """服务器版锁死自开发（2026-11-09）。

    为什么：部署到公网服务器后，`/api/selfdev/*` 能改服务器上的源码、重启服务，
    而当前没有登录鉴权 —— 等于把 shell 权限挂在公网上。
    用环境变量 `MM_DISABLE_SELFDEV=1` 一刀切：所有写操作直接拒绝，
    读（tree/grep）也一并关掉，避免被当成文件浏览器。
    """
    return str(os.environ.get("MM_DISABLE_SELFDEV") or "").strip() in ("1", "true", "yes")


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
        logger.debug("silent-except", exc_info=True)
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
                        logger.debug("silent-except", exc_info=True)
            try:
                os.rmdir(legacy)          # 空了就删掉；不空留着无害
            except OSError:
                logger.debug("silent-except", exc_info=True)
    except Exception:
        logger.debug("silent-except", exc_info=True)
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
        logger.debug("silent-except", exc_info=True)
    return p


def journal(act: str, rel: str, **kw) -> None:
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "act": act, "rel": rel}
        rec.update(kw)
        with open(_journal_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("silent-except", exc_info=True)


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
        logger.debug("silent-except", exc_info=True)

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
            logger.debug("silent-except", exc_info=True)
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
    # ★ 完全访问开关（2026-11-08 用户要求，类比 WorkBuddy 的权限模式）
    #   关：只允许改 frontend/ 和新建文件（保护后端核心，默认）
    #   开：允许改 backend/*.py 等核心代码 —— 自我迭代改后端必须开这个
    if r.startswith("backend/"):
        try:
            _full = bool((config.load_config() or {}).get("self_dev_full_access"))
        except Exception:
            _full = False
        if not _full:
            raise PathError(
                "改 backend/ 需要先打开「完全访问」开关"
                "（顶栏 🛠 自开发 面板里）。当前只能改 frontend/ 或新建文件。")
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


def read(rel: str, start: int = 0, end: int = 0, inline: bool = True) -> dict:
    if locked():
        return {"ok": False, "error": "服务器版已锁定自开发：不允许读取服务端源码"}
    """读源码。**支持按行取**（2026-11-08 改）。

    ⚠️ 为什么必须分页：`backend/main.py` 有 340KB / 7000+ 行，
    整份返回会把模型的上下文一次灌满 —— 实测本地 8B 读完就再也写不动了
    （思考 5000 字、只调 read/tree，就是不落笔）。
    现在默认最多给 200 行，并告诉它总行数和怎么翻页。
    """
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    p = os.path.join(project_root(), r)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % r}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            _raw_text = f.read()          # ★ 保留原始末尾换行
        all_lines = _raw_text.splitlines()
    except OSError as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    total = len(all_lines)
    if start or end:
        s0 = max(1, int(start or 1))
        e0 = min(total, int(end or (s0 + 199)))
    else:
        s0, e0 = 1, min(total, 200)
    chunk = all_lines[s0 - 1:e0]
    # 带行号：模型才能说出"加在第 X 行附近"，patch 也更容易对准
    text = "\n".join("%5d| %s" % (i, ln) for i, ln in enumerate(chunk, s0))
    body = ("【%s】共 %d 行，本次给你第 %d~%d 行\n```\n%s\n```" %
            (r, total, s0, e0, text))
    if e0 < total:
        body += ("\n（后面还有 %d 行。要看继续用 start=%d，或先用 "
                 "selfdev_grep 搜关键字再精准定位）" % (total - e0, e0 + 1))
    # ★ 2026-11-16 修致命 bug（用户要求「自主创新」时查出来的）：
    #   上面的 text 是**给人/模型看的展示文本**（带 `    1| ` 行号 + ``` 围栏）。
    #   而 patch() 里 `text = got["text"]` 直接拿它做 find 替换并写盘 ——
    #   结果把带行号的展示文本写回文件，整个文件被污染。
    #   Python 文件被 write 的语法校验挡住（报「第 1 行 invalid character」，
    #   看起来像模型的错，其实是这里把文件写坏了）；
    #   .txt/.css/.js 没有语法校验，**直接被写坏且无任何提示**。
    #   这一个 bug 同时造成了历史上「find 找不到」「语法校验失败」两类失败。
    #   所以额外返回 raw = 完整原文（不受分页影响），供 patch 等写操作使用。
    return {"ok": True, "rel": r, "path": p, "text": body,
            "raw": _raw_text,
            "total_lines": total,
            "start": s0, "end": e0, "truncated": e0 < total}


def grep(rel: str, pattern: str, limit: int = 40) -> dict:
    """在文件里搜关键字 → 返回行号 + 那一行（给模型定位用）。

    比"整份读完再找"快一个数量级，也是小模型唯一可行的改码路径。
    """
    try:
        r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    p = os.path.join(project_root(), r)
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % r}
    try:
        lines = open(p, "r", encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    hits = []
    for i, ln in enumerate(lines, 1):
        if pattern and pattern in ln:
            hits.append("%5d| %s" % (i, ln[:200]))
            if len(hits) >= limit:
                break
    if not hits:
        return {"ok": True, "text": "没搜到「%s」（共 %d 行）" % (pattern, len(lines))}
    return {"ok": True,
            "text": "在 %s 里搜到 %d 处「%s」：\n```\n%s\n```"
                    % (r, len(hits), pattern, "\n".join(hits))}


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
        logger.debug("silent-except", exc_info=True)


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
        logger.debug("silent-except", exc_info=True)
    return fn


def write(rel: str, content: str, note: str = "") -> dict:
    if locked():
        return {"ok": False, "error": "服务器版已锁定自开发：不允许改写服务端源码"}
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
    # 🔒 锁定区校验（2026-11-11）：这些区块被自迭代改坏过多次，
    #    症状都是"界面空白/一直转"，用户要求硬性保护 —— 内容变了或标记被删就拒绝写盘。
    _lock_err = _check_locked_blocks(r, before, content)
    if _lock_err:
        journal("write_rejected_locked", r, err=_lock_err[:200])
        return {"ok": False, "error": _lock_err}
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
            logger.debug("silent-except", exc_info=True)
    journal("write", r, chars=len(content), backup=bak, note=note[:120],
            added=d["added"], removed=d["removed"])
    return {"ok": True, "rel": r, "path": p, "chars": len(content), "backup": bak,
            "diff": d, "is_new": not before}


def insert_after(rel: str, anchor: str, text: str, note: str = "") -> dict:
    """在**某一行之后**插入一段文本（只要给"那一行里的一句"，不用复述整段）。

    为什么加它（2026-11-08 实测）：`patch` 要求 `find` 和原文**一字不差**，
    让本地 8B 复述 30 行几乎不可能 —— 它读了 8 分钟也没敢下手。
    这个工具只要一句**唯一的锚点**（比如「每一轮先判一句」），
    模型的工作量从"抄写"降到"指认"，小模型才有可能把活干完。
    """
    got = read(rel, start=1, end=0)
    if not got.get("ok"):
        return got
    # read 返回的是带行号的展示文本，这里重新取原文
    p = os.path.join(project_root(), safe_rel(rel))
    try:
        lines = open(p, "r", encoding="utf-8").read().splitlines()
    except OSError as e:
        return {"ok": False, "error": "读取失败：%s" % e}

    # ⚠️ 2026-11-08 实测：模型（DeepSeek V4.1）会把换行写成**字面量 `\n`**
    #   （两个字符），插进 Python 源码后报 "unexpected character after line
    #   continuation character"。这里提前拦住并给一句它能听懂的话，
    #   别让它对着一句晦涩的语法错误瞎猜。
    if "\\n" in text and "\n" not in text:
        return {"ok": False,
                "error": "你传的 text 里是**字面量 \\n**（反斜杠+n 两个字符），不是换行。"
                         "请传真正的换行（JSON 里就是 \\n 转义），或者干脆写成多行。"}

    a = (anchor or "").strip()
    if not a:
        return {"ok": False, "error": "anchor 不能为空"}
    hits = [i for i, ln in enumerate(lines) if a in ln]
    if not hits:
        return {"ok": False, "error": "没找到包含「%s」的行" % a[:60]}
    if len(hits) > 1:
        return {"ok": False,
                "error": "「%s」出现在 %d 行里，请给更长的锚点（第 %s 行）"
                         % (a[:40], len(hits), "、".join(str(h + 1) for h in hits[:8]))}

    idx = hits[0]
    # 缩进跟随锚点行，插入内容才不会破坏结构。
    # ⚠️ 先**去掉模型自带的公共缩进**：模型经常把整段代码带着缩进传来，
    #   直接再补一次就变成 8 空格 → Python 报 unexpected indent
    #   （我自己第一版就踩了，实测报错「第 4 行 unexpected indent」）。
    raw = text.splitlines()
    pads = [len(ln) - len(ln.lstrip()) for ln in raw if ln.strip()]
    cut = min(pads) if pads else 0
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    block = [indent + ln[cut:] if ln.strip() else ln for ln in raw]
    new_lines = lines[:idx + 1] + block + lines[idx + 1:]
    return write(rel, "\n".join(new_lines) + "\n", note or ("insert_after: " + a[:40]))


def list_pending() -> list:
    """读取待审核动作存储，返回给前端展示的待审核列表。

    沿用现有 selfdev 的 pending 结构（data/selfdev_pending.json 里的 actions 列表），
    每条动作转成 {id, title, risk, files, diff_summary, note}：
      · diff_summary 截断到 500 字（避免前端一次渲染几十 KB 的 diff）；
      · note 从 a.get('note','') 读取（approve 时写入的备注）。

    任何异常都返回空列表 —— 待审核列表读不出来不该把整个面板带崩。
    """
    out = []
    try:
        p = _pending_path()
        if not os.path.isfile(p):
            return out
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        acts = data.get("actions") if isinstance(data, dict) else None
        if not isinstance(acts, list):
            return out
        for a in acts:
            if not isinstance(a, dict):
                continue
            diff = str(a.get("diff") or a.get("diff_summary") or "")
            if len(diff) > 500:
                diff = diff[:500] + "…"
            files = a.get("files")
            if not isinstance(files, list):
                files = [a.get("rel")] if a.get("rel") else []
            out.append({
                "id": str(a.get("id") or ""),
                "title": str(a.get("title") or a.get("description") or ""),
                "risk": str(a.get("risk") or ""),
                "files": [str(x) for x in files if x],
                "diff_summary": diff,
                "note": str(a.get("note", "") or ""),
            })
    except Exception:
        logger.debug("silent-except", exc_info=True)
        return []
    return out


def _pending_path() -> str:
    """待审核动作存储路径：data/selfdev_pending.json。"""
    return config.data("data", "selfdev_pending.json")


def _recent_file_changes(rel: str, hours: float = 24.0) -> int:
    """统计该文件近 hours 小时内被改动的次数（读 selfdev journal）。

    为什么要有（2026-11-11 用户要求）：
      main.py 被自迭代连改 21 次、self_iterate.py 连改 17 次 —— 反复叠加，
      越改越乱、根因没确认就继续叠补丁。这里在 patch 入口加一道冷却：
      24h 内同文件改动超阈值就拒绝，逼模型先确认根因。
    """
    try:
        cutoff = time.time() - float(hours) * 3600.0
        cnt = 0
        with open(_journal_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("rel") != rel:
                    continue
                if rec.get("act") not in ("write", "patch"):
                    continue
                ts = rec.get("ts") or ""
                try:
                    t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if t >= cutoff:
                    cnt += 1
        return cnt
    except Exception:
        return 0


# 同文件改动冷却阈值（24h 内超过这个次数就拒绝，逼模型先确认根因）
FILE_CHANGE_LIMIT_24H = 3


def _guess_intent_from_diff(find: str, replace: str, description: str = "") -> str:
    """从 patch 请求里猜一句话中文意图（给 changelog 用）。

    优先级：
      1) 请求自带的 description（模型/用户写的，最准）；
      2) diff 首个 hunk 的注释（# ... / // ... / /* ... */）；
      3) diff 首个 hunk 里出现的函数名（def xxx / function xxx / xxx = function）。
    都拿不到就返回空串，由调用方兜底。
    """
    d = " ".join(str(description or "").split())
    if d:
        return d[:120]
    # 只看首个 hunk：从 find 的第一行开始，最多 40 行
    src = (find or "").splitlines()[:40]
    # 2) 注释
    for ln in src:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            t = s.lstrip("#").strip()
            if t:
                return t[:120]
        if s.startswith("//"):
            t = s.lstrip("/").strip()
            if t:
                return t[:120]
        if s.startswith("/*") or s.startswith("*"):
            t = s.strip("/* \t").strip()
            if t and not t.startswith("🔒"):
                return t[:120]
    # 3) 函数名
    import re as _re
    for ln in src:
        m = _re.search(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)", ln)
        if m:
            return "调整函数 %s" % m.group(1)
        m = _re.search(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)", ln)
        if m:
            return "调整函数 %s" % m.group(1)
        m = _re.search(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?function", ln)
        if m:
            return "调整函数 %s" % m.group(1)
    # 4) 实在没有：用 replace 的第一行非空内容当线索
    for ln in (replace or "").splitlines()[:20]:
        s = ln.strip()
        if s and not s.startswith(("#", "//", "/*", "*")):
            return "修改 %s" % s[:60]
    return ""


def _make_changelog(rel: str, find: str, replace: str, fail_kind: str = "",
                    history: int = 0, description: str = "") -> str:
    """拼一句话中文 changelog（写进 actlog.record_act 的 summary）。

    模板：'修复 fail_kind=X（历史 N 次），改动 backend/yyy.py：<一句话意图>'
    fail_kind 缺失时退化成 '改动 backend/yyy.py：<意图>'。
    """
    intent = _guess_intent_from_diff(find, replace, description) or "（未提供意图）"
    fk = str(fail_kind or "").strip()
    # ★ 2026-11-16 修小瑕疵：原来两个分支都拼了「改动」，
    #   fail_kind 空时会输出「改动，改动 backend/x.py：…」重复一次。
    if fk:
        return "修复 fail_kind=%s（历史 %d 次），改动 %s：%s" % (
            fk, int(history or 0), rel, intent)
    return "改动 %s：%s" % (rel, intent)


def patch(rel: str, find: str, replace: str, note: str = "",
          description: str = "", fail_kind: str = "",
          session: str = "", root_cause: str = "") -> dict:
    """精确替换（和编辑器里的"替换"一样）。找不到 / 命中多处都会拒绝。

    成功后自动生成一句话中文 changelog，写入 actlog.record_act 的 summary 字段
    （2026-11-16 用户要求：patch 成功要留痕，方便回看"这次改了什么"）。

    🧊 同文件改动冷却（2026-11-11 用户要求，2026-11-17 强化）：
      24h 内同一文件改动 ≥ FILE_CHANGE_LIMIT_24H 次时，**必须**附带
      root_cause 字段（引用 actlog 证据说明根因），否则拒绝。
      背景：main.py 28 次、self_iterate.py 24 次、app.js 20 次反复改，
      越改越乱 —— 冷却不是为了拦改动，而是逼模型先确认根因再动手。
    """
    # 🧪 失败真实性前置校验（2026-11-17）：改动前先查该 fail_kind 的真实计数。
    #   计数为 0 → 说明这个"失败"根本没发生过，是模型凭空捏的幽灵修复，
    #   直接拒绝并记 phantom，从源头压掉"改了个不存在的问题"这类回滚。
    #   ★ 2026-11-17 调整：把这道拦截提到 patch 入口最前面（在 read/冷却之前），
    #     幽灵修复连读文件都不必做，直接拒绝，省一次 IO 也避免被 read 的失败
    #     掩盖掉真正的拒绝原因。
    #   ★ 2026-11-17 二次调整（改动证据卡）：改用 count_fail_kind_ex 按**近 7 天**
    #     时间窗统计，而不是 fail_stats 的「最近 100 条」。原因：fail_stats 只按
    #     条数截断，3 个月前反复出现的 fail_kind 会一直算作「有」，幽灵修复照样
    #     放行。现在只有「近 7 天真的发生过」才放行，证据卡上展示的也是这个数。
    #   ★ 2026-11-17 三次调整（幽灵修复闸门·第一步）：把这段逻辑抽成
    #     _check_failure_evidence()，patch 入口只调用它。好处：
    #       · 单步可验证 —— 闸门逻辑独立成函数，可单独断言；
    #       · 与 write()/insert_after() 复用同一套判定，避免两处口径漂移；
    #       · 拒绝原因/证据文案集中一处，前端卡片直接吃 evidence 字段。
    _fk = str(fail_kind or "").strip()
    _real = -1              # -1 = 未查/查不到（不拦）
    _evidence = ""          # 改动证据卡：一句话证据（写进 journal / 返回给前端）
    if _fk:
        # ★ 2026-11-17 硬前置：走 _require_real_failure，不再依赖模型自觉。
        #   不通过（reason=='phantom'）直接返回，连读文件/冷却都不做。
        _gate = _require_real_failure(_fk, days=7)
        if not _gate.get("ok"):
            return {"ok": False,
                    "error": _gate.get("reason_text") or
                    ("拒绝：fail_kind=%s 在最近 7 天里真实计数为 0，"
                     "说明这个失败并没有发生过（幽灵修复）。" % _fk),
                    "reason": _gate.get("reason") or "phantom",
                    "evidence": _gate.get("evidence") or ""}
        _real = int(_gate.get("count") or 0)
        _evidence = _gate.get("evidence") or ""
        if _real <= 0:
            # 账本不可读时 _require_real_failure 保守放行，count=0；
            # 这里保持 -1 语义（未查/查不到），避免证据卡误报「0 次」。
            _real = -1
        # ★ 2026-11-17 幽灵修复闸门（本次改动）：不再给 count 赋常量，
        #   而是用 _require_real_failure 返回的**真实计数**做硬断言。
        #   计数 >0 才放行；计数为 0 已在上面被拒绝（phantom）。
        #   这里再断言一次，防止未来有人把上面的 return 改掉而静默放行。
        assert _real > 0, (
            "幽灵修复闸门失效：fail_kind=%s 计数=%r，应为 >0" % (_fk, _real))
        # 把 fail_kind + 真实计数写进改动记录（journal），供事后审计。
        try:
            journal("gate_pass", rel, fail_kind=_fk, count=int(_real),
                    days=7, evidence=_evidence[:200])
        except Exception:
            logger.debug("silent-except", exc_info=True)
    # ★ 2026-11-17 幽灵修复闸门·真正接进 selfdev 提案入口（本次改动）：
    #   上面那段只在 fail_kind 非空时才查账本 —— 但 selfdev 的提案入口
    #   （self_iterate 生成 patch 前）经常**不带 fail_kind**，于是整道闸门
    #   被静默跳过，幽灵修复照样放行。这是 02:44/03:01/03:21 三次立项都没
    #   落地的关键一步：闸门逻辑写好了，但提案路径上根本没调用它。
    #   现在改成：**无论 fail_kind 是否为空，都从 self_iterate._count_fail_kind
    #   取真实失败计数**；计数为 0 则拒绝该提案并写 actlog 记录 phantom=true。
    #   注意：fail_kind 为空时用 "(unspecified)" 占位，避免把「没写 fail_kind」
    #   当成「无需证据」而放行 —— 那正是幽灵修复的温床。
    _proposal_fk = _fk or "(unspecified)"
    _proposal_count = -1
    try:
        from . import self_iterate as _si
        _cnt_map = _si._count_fail_kind(_proposal_fk, days=7) or {}
        _proposal_count = int(_cnt_map.get(_proposal_fk) or 0)
    except Exception:
        logger.debug("silent-except", exc_info=True)
        _proposal_count = -1
    if _proposal_count == 0:
        # 幽灵修复：提案路径上真实计数为 0 → 拒绝，并写 actlog 记录 phantom=true。
        _phantom_msg = (
            "拒绝：提案 fail_kind「%s」近 7 天真实失败计数为 0，"
            "属于幽灵修复（phantom=true）。请先确认这个失败真的出现过"
            "（actlog / task.json 里有记录），再动手改代码。" % _proposal_fk)
        try:
            journal("phantom", rel, fail_kind=_proposal_fk, days=7,
                    count=0, phantom=True, source="selfdev.patch")
        except Exception:
            logger.debug("silent-except", exc_info=True)
        try:
            actlog.record_act(
                "selfdev_phantom",
                summary="幽灵修复被拒：fail_kind=%s 近 7 天计数 0" % _proposal_fk,
                result="fail",
                detail=_phantom_msg,
                files=[rel] if rel else [],
                phantom=True,
                fail_kind=_proposal_fk,
                count=0,
            )
        except Exception:
            logger.debug("silent-except", exc_info=True)
        return {"ok": False, "error": _phantom_msg,
                "reason": "phantom", "phantom": True,
                "evidence": "针对 fail_kind: %s，近 7 天 0 次" % _proposal_fk}
    if _proposal_count > 0 and _real <= 0:
        # 提案路径查到了真实计数，但上面 fail_kind 分支没走（fail_kind 为空）——
        # 把真实计数补进证据卡，让前端能看到「这次改动有 N 次真实失败依据」。
        _real = _proposal_count
        _evidence = "针对 fail_kind: %s，近 7 天 %d 次" % (
            _proposal_fk, _proposal_count)
    # ★ 2026-11-17 改动证据卡：把 evidence 挂到本次 patch 的返回结果上，
    #   前端动作卡片直接渲染（fail_kind / 计数 / 回归结论）。
    #   这里先建一个 dict，后面成功/失败分支都往里塞，最后统一 merge 进返回值。
    _card = {"fail_kind": _fk, "count": int(_real) if _real > 0 else 0,
             "evidence": _evidence}
    got = read(rel)
    if not got.get("ok"):
        got["evidence"] = _card
        return got
    # 🧊 同文件改动冷却（2026-11-11 用户要求，2026-11-17 强化）：
    #   24h 内同文件改动 ≥ FILE_CHANGE_LIMIT_24H 次时，patch **必须**附带
    #   root_cause 字段（引用 actlog 证据说明根因），否则拒绝。
    #   为什么：main.py 28 次、self_iterate.py 24 次、app.js 20 次反复改，
    #   越改越乱 —— 冷却不是为了拦改动，而是逼模型先确认根因再动手。
    try:
        _r = safe_rel(rel)
    except PathError as e:
        return {"ok": False, "error": str(e)}
    _n = _recent_file_changes(_r, 24.0)
    if _n >= FILE_CHANGE_LIMIT_24H:
        _rc = " ".join(str(root_cause or "").split())
        if not _rc:
            _msg = ("该文件已改 %d 次（近 24 小时），请先确认根因再改。\n"
                    "反复叠加补丁只会越改越乱 —— 先读代码、定位真正的问题，"
                    "在回答里说清根因和方案，再动手。\n"
                    "⚠️ 冷却已触发：本次 patch 必须附带 root_cause 字段"
                    "（引用 actlog 证据说明根因），否则拒绝。" % _n)
            journal("patch_rejected_cooldown", _r, count=_n,
                    reason="缺少 root_cause")
            return {"ok": False, "error": _msg}
        # 有 root_cause：记一条冷却放行事件（含根因摘要），便于事后审计
        journal("patch_cooldown_override", _r, count=_n,
                root_cause=_rc[:300])
    # ★ 2026-11-16：必须用 raw（完整原文），不能用 text（带行号的展示文本）。
    #   用 text 会把整个文件污染成「    1| xxx」的格式。
    text = got.get("raw")
    if text is None:
        text = got["text"]
    n = text.count(find)
    if n == 0:
        return {"ok": False, "error": "没找到这段原文（空格/缩进/换行必须完全一致）"}
    if n > 1:
        return {"ok": False,
                "error": "这段原文出现了 %d 次，为免改错地方，请给更长的一段上下文" % n}
    out = write(rel, text.replace(find, replace, 1), note or "patch")
    # 📝 成功分支：自动生成一句话中文 changelog 并记到 actlog
    if out.get("ok"):
        try:
            from . import actlog
            _summary = _make_changelog(
                _r, find, replace,
                fail_kind=fail_kind, history=_n, description=description)
            # 🧾 改动证据卡（2026-11-17 用户要求）：
            #   用户反复问「详细说明改了什么」，而 timeline 里只有一句 summary，
            #   看不到「针对哪个失败、改了几次、改在文件哪一行」。
            #   这里把证据一并写进 actlog 的 evidence 字段，前端卡片直接渲染
            #   「针对：<fail_kind> × N」。
            #   · fail_kind：本次针对的失败分类（空则不带）
            #   · count：该 fail_kind 的真实计数（-1 表示未查/查不到）
            #   · history：该文件近 24h 改动次数（冷却依据）
            #   · root_cause：冷却触发时模型给出的根因说明（空则不带）
            _evidence = {"rel": _r, "history": int(_n)}
            if _fk:
                _evidence["fail_kind"] = _fk
                _evidence["count"] = int(_real)
            _rc2 = " ".join(str(root_cause or "").split())
            if _rc2:
                _evidence["root_cause"] = _rc2[:300]
            try:
                actlog.record(session or "default", "selfdev_patch",
                              {"rel": _r}, ok=True,
                              note=_summary, evidence=_evidence)
            except TypeError:
                # 兼容旧版 actlog.record（无 evidence 参数）
                actlog.record(session or "default", "selfdev_patch",
                              {"rel": _r}, ok=True, note=_summary)
        except Exception:
            logger.debug("silent-except", exc_info=True)
    return out
    # ★ 2026-11-16：必须用 raw（完整原文），不能用 text（带行号的展示文本）。
    #   用 text 会把整个文件污染成「    1| xxx」的格式。
    text = got.get("raw")
    if text is None:
        text = got["text"]
    n = text.count(find)
    if n == 0:
        return {"ok": False, "error": "没找到这段原文（空格/缩进/换行必须完全一致）"}
    if n > 1:
        return {"ok": False,
                "error": "这段原文出现了 %d 次，为免改错地方，请给更长的一段上下文" % n}
    out = write(rel, text.replace(find, replace, 1), note or "patch")
    # 📝 成功分支：自动生成一句话中文 changelog 并记到 actlog
    if out.get("ok"):
        try:
            from . import actlog
            _summary = _make_changelog(
                _r, find, replace,
                fail_kind=fail_kind, history=_n, description=description)
            # 🧾 改动证据卡（2026-11-17 用户要求）：
            #   用户反复问「详细说明改了什么」，而 timeline 里只有一句 summary，
            #   看不到「针对哪个失败、改了几次、改在文件哪一行」。
            #   这里把证据一并写进 actlog 的 evidence 字段，前端卡片直接渲染
            #   「针对：<fail_kind> × N」。
            #   · fail_kind：本次针对的失败分类（空则不带）
            #   · count：该 fail_kind 的真实历史计数（幽灵修复已被上面拦掉，
            #     这里只可能是 >=1 或 -1=查不到）
            #   · line：find 片段在原文里的起始行号（1 基），方便点开定位
            #   · lines：find 片段覆盖的行数
            _evidence = {}
            if _fk:
                _evidence["fail_kind"] = _fk
                _evidence["count"] = int(_real if _real > 0 else 0)
            try:
                _idx = text.find(find)
                if _idx >= 0:
                    _evidence["line"] = text.count("\n", 0, _idx) + 1
                    _evidence["lines"] = max(1, find.count("\n") + 1)
            except Exception:
                logger.debug("silent-except", exc_info=True)
            _args = {"rel": _r, "fail_kind": fail_kind or ""}
            if _evidence:
                _args["evidence"] = _evidence
            actlog.record(
                session or "selfdev", "selfdev_patch",
                args=_args,
                ok=True, note=_summary)
            out["summary"] = _summary
            if _evidence:
                out["evidence"] = _evidence
        except Exception:
            logger.debug("silent-except", exc_info=True)
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


def _pid_alive(pid: int) -> bool:
    """这个 PID 现在是否真的还活着（拿不到 psutil 时保守返回 False）。"""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _window_owner_alive() -> bool:
    """启动器还活着吗？只有它还活着，新进程才可以「不开窗口」。

    ⚠️⚠️ 2026-11-12：用户反复报「自迭代之后重启，桌面总打不开」，根因在这里。

      · 启动器（launcher/启动窗口.pyw）用 `MM_NO_WINDOW=1` 拉起后端，
        语义是「窗口我来开，你别开」；
      · 而重启是 `env = dict(os.environ)` **原样继承** —— 于是重启出来的
        新进程也成了无窗口后端；
      · 它偏偏还占着 8000。用户再双击快捷方式，撞上 run.py 的
        「已有实例 → 打开浏览器 → 退出」分支，**永远不会有桌面窗口**；
      · 一旦进了这个状态就自锁：没有任何路径会清掉 MM_NO_WINDOW，
        所以「上次也是这样」—— 只要走过一次启动器，之后每次重启都没界面。

    判据：启动器把自己的 PID 写进 `MM_LAUNCHER_PID`（见 启动窗口.pyw）。
      · PID 还活着 → 让它继续管窗口，新进程保持无窗口（避免两个窗口）
      · PID 没了 / 老版本启动器没写 → 新进程必须自己开窗，否则用户没界面
    """
    pid = (os.environ.get("MM_LAUNCHER_PID") or "").strip()
    if not pid.isdigit():
        return False          # 确认不了就当它没了，宁可自己开窗
    return _pid_alive(int(pid))


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

    # ★★ 2026-11-12：没有启动器接管窗口时，必须让新进程自己开窗口。
    #   否则重启后只剩一个「无窗口后端」占着 8000，用户双击快捷方式会被
    #   run.py 的「已有实例」分支挡回去 —— 表现就是「自迭代完桌面打不开」。
    #   详细机制见 _window_owner_alive() 的注释。
    if env.get("MM_NO_WINDOW") == "1" and not _window_owner_alive():
        env.pop("MM_NO_WINDOW", None)
        journal("restart_reown_window", "-",
                launcher=(os.environ.get("MM_LAUNCHER_PID") or "未记录"))

    # ⚠️ Windows 上要三件套（2026-11-08 实测：只给 DETACHED_PROCESS 时，
    #   父进程一退出、或外层工具把进程放进 Job，子进程会跟着被杀）：
    #     DETACHED_PROCESS        —— 不继承控制台
    #     CREATE_NEW_PROCESS_GROUP—— 独立进程组，Ctrl+C 不波及
    #     CREATE_BREAKAWAY_FROM_JOB —— 逃出父进程所在的 Job 对象
    #   最后一个在某些环境会直接报错（Job 不允许 breakaway），所以要有退路。
    # ⚠️⚠️ 2026-11-12：**不能带 DETACHED_PROCESS(0x8)**。
    #   实测 pid 31808：带 DETACHED 重启出来的进程活得好好的，
    #   /api/health 自报 `window=True`（说明它认为该有窗口、也确实执行了
    #   pywebview），但 `MainWindowHandle=0`、进程里一个窗口都枚举不到 ——
    #   窗口被建在了**不可见的窗口站/桌面**上，用户永远看不到。
    #   这就是「点前端重启后没反应、退出了没重进」的真身。
    #
    #   pythonw.exe 本来就没有控制台，DETACHED 对我们**毫无用处**，
    #   只带来这个副作用。能保证子进程活下来的是 BREAKAWAY（逃出父进程的 Job），
    #   而它对窗口没有任何影响。
    flags_try = [
        0x01000000 | 0x00000200,   # BREAKAWAY + NEW_PROCESS_GROUP（首选：能逃 Job，又能正常开窗）
        0x00000200,                # Job 不允许 breakaway 时的退路
        0,                         # 最保守
    ]
    proc = None
    last_err = ""
    _via_start = False
    # ★★ 2026-11-12：**要自己开窗口时，必须走 shell 的 start**。
    #   实测（pid 55624）：同样参数直接用 Popen 拉 pythonw.exe，进程活得好好的、
    #   run.py 也走到了「准备创建窗口」、webview.start() 也进去了，但
    #   MainWindowHandle 始终是 0 —— 窗口建在了一个看不见的桌面上。
    #   用户看到的就是「点重启后没反应，退出了没重进」。
    #   而 `start` 走的是 ShellExecute，和**双击快捷方式完全同一条路径**，能正常出窗。
    #   注意：cmd 会秒退，所以这条分支不能用 proc.poll() 判断子进程死活。
    if os.name == "nt" and not frozen and env.get("MM_NO_WINDOW") != "1":
        try:
            subprocess.Popen('start "" "%s" "%s"' % (sys.executable, entry),
                             shell=True, cwd=cwd, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _via_start = True
            journal("restart_via_start", "-")
        except Exception as e:                        # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, e)
    if not _via_start:
        for fl in flags_try:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True,
                                        creationflags=fl if os.name == "nt" else 0)
                break
            except Exception as e:                    # noqa: BLE001
                last_err = "%s: %s" % (type(e).__name__, e)
                proc = None
    if (not _via_start) and proc is None:
        return {"ok": False, "error": "拉不起新进程（%s），已保持当前进程继续运行"
                                      % last_err}

    # 关键：**先确认子进程活着，再退出自己**。
    # 以前是"先 os._exit(0)"，结果子进程一出生就死（Job 被连带清理），
    # 应用直接没了 —— 用户只看到"重启了一下就再也没起来"。
    time.sleep(max(1.0, delay + 1.0))
    if (not _via_start) and proc.poll() is not None:
        log = ""
        try:
            log = (proc.stdout.read() if proc.stdout else "")[-600:]
        except Exception:
            logger.debug("silent-except", exc_info=True)
        journal("restart_failed", "-", code=proc.returncode)
        return {"ok": False,
                "error": "新进程启动后立刻退出了（退出码 %s），**已保持当前进程继续运行**"
                         "（服务没断）。多半是端口没空出来或代码有问题。%s"
                         % (proc.returncode, ("\n" + log) if log else "")}

    _newpid = 0 if _via_start else proc.pid
    journal("restart", "-", cmd=" ".join(cmd)[:200], pid=_newpid,
            via=("start" if _via_start else "popen"))

    def _bye() -> None:
        time.sleep(0.3)
        os._exit(0)

    import threading
    threading.Thread(target=_bye, daemon=True).start()
    return {"ok": True,
            "msg": ("已通过 start 拉起新实例（窗口会自己出现），正在切换…"
                    if _via_start else
                    "已拉起新进程（PID %d）并通过导入校验，正在切换…" % _newpid)
                   + "约 10 秒后刷新页面即可。"}
