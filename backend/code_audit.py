"""代码自检（code audit）—— 让 AI 主动检查自己代码里的语法 / 调用 / 逻辑问题。

2026-11-15 用户要求（原话要点）：
  · 智能体要能主动且时不时检查自己的代码，排除不合理、有 bug、失灵、
    逻辑漏洞、语法和调用错误的地方；
  · 每次代码被改完自动跑一次 + 每天定时一次；
  · 信任模式下发现的问题直接修。

设计原则：**只报高置信度的问题**。
这个功能的价值完全取决于「报了是不是真有问题」——误报一堆，用户就不看了，
等于没做。所以每条检查都必须能说清「为什么这条几乎不会是误报」，拿不准的
宁可交给 LLM 深度审查（deep=True）去判断，也不在这里硬报。

本模块只做**只读检查**，不改任何文件。要修由 self_iterate 走「备份 + 校验 +
回滚」那条路，这样才安全。
"""

from __future__ import annotations

import ast
import builtins
import re
import subprocess
import sys
import time
from pathlib import Path
import logging
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
_FRONTEND = _ROOT / "frontend"

_MAX_BYTES = 2_500_000          # 超过这个大小的文件跳过（不是人手写的）
_MAX_SILENT_PER_FILE = 1        # 静默 except 每个文件只汇总成 1 条

_LEVEL_ORDER = {"high": 0, "mid": 1, "low": 2, "info": 3}


# ---------------------------------------------------------------- 基础工具

# 受保护文件：回撤机制本身（selfdev.py / restore.py）。它们被 selfdev 的
# PROTECTED 锁死、模型改不了，报出来也修不掉，只会让报告永久挂一条噪音。
# 2026-11-15：实测 `selfdev.py:73 _extract_locked` 就是这种情况
# （已被 `_extract_all_locked` 取代，但文件不许改，只能跳过）。
_SKIP_FILES = {"selfdev.py", "restore.py"}


def _py_files():
    out = []
    for p in sorted(_BACKEND.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if p.name in _SKIP_FILES:
            continue
        try:
            if p.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _ref_files():
    """**引用计数**专用：backend + 根目录脚本 + 前端。

    为什么要单独一份（2026-11-15）：
      `_py_files()` 只扫 backend/，于是「根目录的 run.py / test_*.py 用到、
      但 backend 内部没人用」的私有函数会被误判成死代码 ——
      实测 `_lonlat_to_tile` / `_tile_latlon` 明明被 `test_mode_switch.py`
      引用着，自检却报 dead。
      这里把根目录和前端也纳入**只做引用计数**（不做完整检查，
      免得测试脚本里成片的 `except: pass` 把报告淹掉）。
    """
    out = list(_py_files())
    skip_dirs = {"__pycache__", "node_modules", ".git", "data", "models",
                 "sessions", "image_library", "dist", "vendor", "_internal"}
    try:
        for p in sorted(_ROOT.glob("*.py")):
            try:
                if p.stat().st_size <= _MAX_BYTES:
                    out.append(p)
            except OSError:
                continue
        for p in sorted(_FRONTEND.rglob("*")):
            if p.suffix not in (".js", ".html"):
                continue
            if any(x in p.parts for x in skip_dirs):
                continue
            try:
                if p.stat().st_size <= _MAX_BYTES:
                    out.append(p)
            except OSError:
                continue
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return out


def _js_files():
    out = []
    for p in sorted(_FRONTEND.rglob("*.js")):
        try:
            if p.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _finding(level, kind, rel, line, msg, hint=""):
    return {"level": level, "kind": kind, "file": rel,
            "line": int(line or 0), "msg": msg, "hint": hint}


# ---------------------------------------------------------------- 1. 语法

def _check_syntax(paths):
    """语法错误是硬错误，一条都不用怀疑。"""
    out = []
    for p in paths:
        src = _read(p)
        if not src.strip():
            continue
        try:
            compile(src, str(p), "exec")
        except SyntaxError as e:
            out.append(_finding("high", "syntax", _rel(p), e.lineno or 0,
                                "语法错误：%s" % (e.msg or "无法编译"),
                                "这一行改完必须能 compile 通过，否则整个后端都起不来"))
        except (ValueError, MemoryError) as e:
            out.append(_finding("mid", "syntax", _rel(p), 0,
                                "编译异常：%s: %s" % (type(e).__name__, e)))
    return out


# ---------------------------------------------------------------- 2. 模块符号表

def _top_level_names(tree):
    """一个模块里顶层可见的名字（函数/类/赋值/import）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for el in t.elts:
                        if isinstance(el, ast.Name):
                            names.add(el.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # 顶层 if/try 里的定义也算（比如平台分支里的函数）
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
    return names


def _module_symbols(trees):
    """模块名(stem) -> 顶层名字集合。"""
    sym = {}
    for rel, (p, tree) in trees.items():
        sym[Path(rel).stem] = _top_level_names(tree)
    return sym


# ---------------------------------------------------------------- 3. 跨模块调用不存在的东西

def _check_cross_module(trees, sym):
    """`from . import xxx` 之后调 `xxx.attr`，而 xxx 里没有 attr。

    这条是**这个项目真实踩过的坑**：self_iterate 里调 `_read_meta()`，
    但那函数从来不存在，闭嘴路径每次抛 NameError，被 except 吞掉，
    表现成「功能静默失效」。所以它值得单独一条检查。
    """
    out = []
    for rel, (p, tree) in trees.items():
        alias = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or 0) >= 1:
                for a in node.names:
                    if a.name in sym:                     # from . import xxx
                        alias[a.asname or a.name] = a.name
        if not alias:
            continue
        # 同一个名字如果在本文件里被重新赋值过（比如 `_am = {...}` 一个局部 dict），
        # 那它就不是模块了，`_am.get(...)` 是 dict.get —— 这类必须跳过，否则全是误报。
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadowed.add(node.id)
            elif isinstance(node, ast.arg):
                shadowed.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                shadowed.add(node.name)
            elif isinstance(node, ast.Global):
                shadowed.update(node.names)
        for nm in shadowed:
            alias.pop(nm, None)
        if not alias:
            continue
        # 运行时动态打属性：`_sd._patch_fixed = True` 这种，属性在源码里没有 def，
        # 但被赋过值就是存在的（本项目 tools.py 就这么给 selfdev 打补丁）。
        attr_assigned = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                        attr_assigned.add((t.value.id, t.attr))
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            mod = alias.get(node.value.id)
            if not mod:
                continue
            if (node.value.id, node.attr) in attr_assigned:
                continue
            attr = node.attr
            if attr.startswith("__"):
                continue
            if attr in sym.get(mod, set()):
                continue
            key = (mod, attr)
            if key in seen:
                continue
            seen.add(key)
            out.append(_finding(
                "high", "missing-attr", rel, getattr(node, "lineno", 0),
                "调用了 `%s.%s`，但 %s.py 里没有 %s" % (node.value.id, attr, mod, attr),
                "这类调用会抛 AttributeError；如果外面套了 except，就会静默失效"))
    return out


# ---------------------------------------------------------------- 4. 模块内调用不存在的函数

def _collect_module_names(tree):
    """文件里所有可能被当作函数名使用的名字（含局部赋值、参数、import）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            ov = node.optional_vars
            if isinstance(ov, ast.Name):
                names.add(ov.id)
    return names


_BUILTIN_NAMES = set(dir(builtins)) | {"self", "cls", "__file__", "__name__"}


def _check_undefined_call(trees):
    """直接调 `foo(...)`，但 foo 在本文件里既没定义也没赋值也没 import。"""
    out = []
    for rel, (p, tree) in trees.items():
        known = _collect_module_names(tree) | _BUILTIN_NAMES
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Name):
                continue
            nm = fn.id
            if nm in known or nm in seen:
                continue
            seen.add(nm)
            out.append(_finding(
                "high", "undefined-call", rel, getattr(node, "lineno", 0),
                "调用了 `%s(...)`，但本文件里没有定义 / 赋值 / import 过 %s" % (nm, nm),
                "运行到这一行会抛 NameError"))
    return out


# ---------------------------------------------------------------- 4a. 子进程绕过 procwin

_SUBPROC_ATTRS = {"Popen", "run", "call", "check_output", "check_call"}


# ---------------------------------------------------------------- 4z. 篡改分类器（两轴）
#
# 对应论文 Auditing Harness Tampering。用户要求「保证程序不会被改坏」：
# 在 selfdev 的 patch 入口拦截「度量篡改」和「授权绕过」两类改动。
#
# 两轴：
#   轴一 · 框架功能角色（role）：授权 / 来源 / 完整性 / 度量 / 能力
#   轴二 · 篡改方式（mode）：删除 / 弱化 / 替换 / 注入 / 绕过
#
# 输出结构化 JSON 写 actlog，供 selfdev 入口判定是否拒绝。

# 轴一：框架功能角色
ROLE_AUTH = "authorization"      # 授权：谁能做什么
ROLE_PROVENANCE = "provenance"   # 来源：东西从哪来
ROLE_INTEGRITY = "integrity"     # 完整性：东西有没有被改
ROLE_METRIC = "metric"           # 度量：计数/统计/阈值
ROLE_CAPABILITY = "capability"   # 能力：能做什么

# 轴二：篡改方式
MODE_DELETE = "delete"           # 删除
MODE_WEAKEN = "weaken"           # 弱化（放宽条件/降低阈值）
MODE_REPLACE = "replace"         # 替换
MODE_INJECT = "inject"           # 注入
MODE_BYPASS = "bypass"           # 绕过

# 命中这两类角色 → selfdev 入口直接拒绝
BLOCKED_ROLES = {ROLE_METRIC, ROLE_AUTH}

# 角色识别关键词（在 find/replace 片段里出现即视为该角色）
_ROLE_PATTERNS = {
    ROLE_AUTH: [
        r"\bPROTECTED\b", r"\bDENY_DIRS\b", r"\bDENY_EXT\b",
        r"\bsafe_rel\b", r"\blocked\s*\(", r"\benabled\s*\(",
        r"\bpermission", r"\bauthoriz", r"\bauthenticat",
        r"\ballow(?:ed)?\b", r"\bdeny\b", r"\bforbid",
        r"\bMM_DISABLE_SELFDEV\b", r"\bself_dev_full_access\b",
        r"\bPathError\b", r"\b越界\b", r"\b禁止\b", r"\b权限\b",
    ],
    ROLE_PROVENANCE: [
        r"\bbackup", r"\bmirror_dir\b", r"\brestore", r"\brollback",
        r"\bjournal\b", r"\bactlog\b", r"\bprovenance", r"\bsource\b",
        r"\b备份\b", r"\b回滚\b", r"\b来源\b",
    ],
    ROLE_INTEGRITY: [
        r"\b_check_syntax\b", r"\bverify_import\b", r"\bhash\b",
        r"\bchecksum\b", r"\bsha256\b", r"\bmd5\b", r"\bintegrity\b",
        r"\b校验\b", r"\b完整性\b",
    ],
    ROLE_METRIC: [
        r"\bcount_fail_kind", r"\bfail_stats\b", r"\b_count_fail_kind\b",
        r"\bFILE_CHANGE_LIMIT", r"\bBATCH_WINDOW\b", r"\b_MAX_SILENT",
        r"\b_MAX_BYTES\b", r"\bMAX_READ\b", r"\bMAX_WRITE\b",
        r"\bthreshold\b", r"\blimit\b", r"\bquota\b", r"\bmetric\b",
        r"\bcount\b", r"\b统计\b", r"\b阈值\b", r"\b计数\b",
    ],
    ROLE_CAPABILITY: [
        r"\bdef\s+\w+", r"\bclass\s+\w+", r"\bfunction\b",
        r"\bcapability\b", r"\b能力\b",
    ],
}

# 篡改方式识别关键词
_MODE_PATTERNS = {
    MODE_DELETE: [r"^\s*$", r"\bdel\b", r"\bremove\b", r"\b删除\b"],
    MODE_WEAKEN: [
        r">=\s*\d+\s*[-+*/]?\s*\d*", r"<=\s*\d+", r"\bTrue\b",
        r"\bpass\b", r"\b放宽\b", r"\b弱化\b", r"\b降低\b",
    ],
    MODE_REPLACE: [r"\breplace\b", r"\b替换\b", r"\bsub\b"],
    MODE_INJECT: [r"\bexec\b", r"\beval\b", r"\b__import__\b", r"\b注入\b"],
    MODE_BYPASS: [
        r"\bbypass\b", r"\bskip\b", r"\b绕过\b", r"\b跳过\b",
        r"\breturn\s+True\b", r"\breturn\s+\{\s*['\"]ok['\"]\s*:\s*True",
    ],
}


def _match_roles(text: str) -> list:
    """返回文本命中的角色列表（去重、保序）。"""
    t = text or ""
    out = []
    for role, pats in _ROLE_PATTERNS.items():
        for pat in pats:
            try:
                if re.search(pat, t):
                    out.append(role)
                    break
            except re.error:
                continue
    return out


def _match_modes(text: str) -> list:
    """返回文本命中的篡改方式列表（去重、保序）。"""
    t = text or ""
    out = []
    for mode, pats in _MODE_PATTERNS.items():
        for pat in pats:
            try:
                if re.search(pat, t, re.MULTILINE):
                    out.append(mode)
                    break
            except re.error:
                continue
    return out


def _diff_lines(find: str, replace: str) -> tuple:
    """把 find/replace 拆成「被删的行」和「新增的行」。

    为什么要拆（2026-11-17）：两轴分类器如果只看整段文本，
    `find` 里原本就有的 `count` / `limit` 之类关键词会把**任何**改动
    都判成「度量篡改」—— 实测误报率极高，等于把分类器做废。
    真正该看的是「这次改动**新增/删除了什么**」：
      · 删掉一行 `if count > LIMIT: raise` → 这是弱化度量，该拦；
      · 只是把 `count` 那行原样保留、改别处 → 不该拦。
    所以这里做一次极简的行级 diff：只保留「新增行」和「被删行」，
    供角色/方式匹配使用。
    """
    f_lines = (find or "").splitlines()
    r_lines = (replace or "").splitlines()
    # 用集合差做粗粒度 diff（不追求 LCS 精度，够用且不会误伤）
    f_set = {}
    for ln in f_lines:
        f_set[ln] = f_set.get(ln, 0) + 1
    r_set = {}
    for ln in r_lines:
        r_set[ln] = r_set.get(ln, 0) + 1
    removed = []
    for ln, n in f_set.items():
        extra = n - r_set.get(ln, 0)
        if extra > 0:
            removed.extend([ln] * extra)
    added = []
    for ln, n in r_set.items():
        extra = n - f_set.get(ln, 0)
        if extra > 0:
            added.extend([ln] * extra)
    return removed, added


def classify_tamper(rel: str, find: str, replace: str) -> dict:
    """两轴分类器：判断一次 patch 是否属于篡改。

    返回结构化 dict：
      {
        "rel": 文件相对路径,
        "roles": [命中的框架功能角色],
        "modes": [命中的篡改方式],
        "blocked": True/False（是否应被 selfdev 入口拒绝）,
        "reason": 拒绝原因（blocked=True 时非空）,
        "axes": {"role": [...], "mode": [...]},
        "added": [新增行], "removed": [被删行]
      }

    判定规则（★ 2026-11-17 修正：只看**改动本身**，不看未改动的上下文）：
      · 命中「度量篡改」= 新增/删除行里含度量角色 且 方式非空 → 拒绝
      · 命中「授权绕过」= 新增/删除行里含授权角色 且 方式含 bypass/weaken → 拒绝
      · 其余放行（但记录到 actlog 供审计）

    为什么改成只看 diff：
      原实现把 find+replace 整段拼起来匹配角色，于是「find 里本来就有
      `count` 这个词」的普通改动全被判成度量篡改 —— 分类器一上线就会
      把正常自迭代全拦死。改成只看新增/删除行后，只有**真的动了**
      度量/授权代码才会命中。
    """
    f = find or ""
    r = replace or ""
    removed, added = _diff_lines(f, r)
    # 角色：只看「新增行 + 被删行」—— 未改动的上下文不算
    changed_text = "\n".join(added + removed)
    roles = _match_roles(changed_text)
    # 方式：主要看新增行（新内容怎么改的）；纯删除时记为 delete
    modes = _match_modes("\n".join(added))
    if not modes and removed and not added:
        modes = [MODE_DELETE]
    # 删除度量/授权代码本身也是一种「弱化」—— 补上
    if removed and not added and not modes:
        modes = [MODE_DELETE]

    blocked = False
    reason = ""
    # 度量篡改：动了度量相关代码，且方式非空
    if ROLE_METRIC in roles and modes:
        blocked = True
        reason = ("命中「度量篡改」：改动涉及度量/计数/阈值（%s），"
                  "方式为 %s。度量被改会让审计失真，拒绝。"
                  % (", ".join(roles), ", ".join(modes)))
    # 授权绕过：动了授权相关代码，且方式是绕过/弱化/删除
    if ROLE_AUTH in roles and (MODE_BYPASS in modes or MODE_WEAKEN in modes
                               or MODE_DELETE in modes):
        blocked = True
        reason = ("命中「授权绕过」：改动涉及授权/权限（%s），"
                  "方式为 %s。授权被绕过会让护栏失效，拒绝。"
                  % (", ".join(roles), ", ".join(modes)))

    return {
        "rel": rel,
        "roles": roles,
        "modes": modes,
        "blocked": blocked,
        "reason": reason,
        "axes": {"role": roles, "mode": modes},
        "added": added,
        "removed": removed,
    }


def audit_tamper(rel: str, find: str, replace: str, session: str = "") -> dict:
    """分类 + 写 actlog。返回分类结果（含 blocked）。

    selfdev.patch 入口调用此函数：blocked=True 直接拒绝，不进执行器。
    """
    result = classify_tamper(rel, find, replace)
    try:
        from . import actlog
        actlog.record(
            session or "selfdev", "tamper_audit",
            args={"rel": rel, "roles": result["roles"],
                  "modes": result["modes"], "blocked": result["blocked"],
                  "added": len(result.get("added") or []),
                  "removed": len(result.get("removed") or [])},
            ok=not result["blocked"],
            note=result["reason"] or "篡改分类：放行")
    except Exception:
        logger.debug("silent-except", exc_info=True)
    return result


def _check_subprocess_bypass(trees):
    """backend/*.py 里直接调 subprocess.Popen/run/call/check_output，
    但同文件没有 import procwin（也没走封装）—— 会弹黑窗口，报 high。

    2026-11-15 用户要求：防黑窗口复发。
    """
    out = []
    for rel, (p, tree) in trees.items():
        if not rel.startswith("backend/"):
            continue
        src = _read(p)
        if "procwin" in src:
            continue
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in _SUBPROC_ATTRS:
                continue
            if not isinstance(fn.value, ast.Name) or fn.value.id != "subprocess":
                continue
            key = (getattr(node, "lineno", 0), fn.attr)
            if key in seen:
                continue
            seen.add(key)
            out.append(_finding(
                "high", "subprocess-bypass", rel, getattr(node, "lineno", 0),
                "直接调用了 `subprocess.%s(...)`，但本文件没有 import procwin" % fn.attr,
                "Windows 下会弹黑窗口；请改用 procwin 封装（procwin.run 等）"))
    return out


# ---------------------------------------------------------------- 4a-2. subprocess 未走 procwin（mid）

def _check_subprocess_nowin(trees):
    """AST 扫 subprocess.Popen/run/call，同文件无 procwin 引用且无 creationflags 则报 mid。

    与 _check_subprocess_bypass 的区别：那条按源码字符串判断、报 high；
    这条按 AST 判断，且允许显式传 creationflags（自己处理了窗口）的情况。
    """
    out = []
    for rel, (p, tree) in trees.items():
        if not rel.startswith("backend/"):
            continue
        src = _read(p)
        if "procwin" in src:
            continue
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in _SUBPROC_ATTRS:
                continue
            if not isinstance(fn.value, ast.Name) or fn.value.id != "subprocess":
                continue
            if any(kw.arg == "creationflags" for kw in node.keywords):
                continue
            key = (getattr(node, "lineno", 0), fn.attr)
            if key in seen:
                continue
            seen.add(key)
            out.append(_finding(
                "mid", "subprocess-nowin", rel, getattr(node, "lineno", 0),
                "`subprocess.%s(...)` 未走 procwin 且未传 creationflags" % fn.attr,
                "Windows 下可能弹黑窗口；请改用 procwin 封装或显式传 creationflags"))
    return out


# ---------------------------------------------------------------- 4a-3. 全局补丁检测

def _has_global_nowin_patch(trees):
    """main.py 里是否调用了 procwin.install_nowin_patch()。

    装了之后，整个进程的 Popen.__init__ / _execute_child / _winapi.CreateProcess
    都被打了 STARTF_USESHOWWINDOW + SW_HIDE —— 那么「某个文件没 import procwin」
    就不再意味着会弹黑窗（实测：装了补丁的进程里跑 py_compile / node --check /
    回归子进程，窗口数均为 0）。
    所以有这个全局兜底时，subprocess 告警只作提示、不逐处报 high/mid，
    否则一个项目就是几十条误报，信任模式还会照着去改 —— 那是真危险。
    """
    for rel, (p, tree) in trees.items():
        src = _read(p)
        if "install_nowin_patch" not in src:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "install_nowin_patch":
                return True
            if isinstance(fn, ast.Name) and fn.id == "install_nowin_patch":
                return True
    return False


# ---------------------------------------------------------------- 4b. 使用未定义的名字

def _star_imports(tree):
    """有 `from x import *` 时无法推断有哪些名字，整个文件跳过。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    return True
    return False


def _scope_bound_body(nodes):
    """收集「这一层作用域」绑定的名字（不进入嵌套函数/类体）。"""
    bound = set()
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            for d in getattr(n, "decorator_list", []) or []:
                stack.append(d)
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
            continue
        if isinstance(n, ast.arg):
            bound.add(n.arg)
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
            continue
        if isinstance(n, ast.ExceptHandler):
            # ⚠️ 不能 continue：`except ... as e:` 的**块体**里还有赋值，
            # 跳过它会把块内的变量误判成「未定义」（实测踩过 iter_runner 的 tb）。
            if n.name:
                bound.add(n.name)
            for ch in ast.iter_child_nodes(n):
                stack.append(ch)
            continue
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
            continue
        for ch in ast.iter_child_nodes(n):
            stack.append(ch)
    return bound


class _UndefNameVisitor(ast.NodeVisitor):
    """按作用域栈检查 `Name` 是不是真的从没被绑定过。

    保守策略：**只在「整个作用域链里都找不到这个名字」时报**，
    宁可漏报也不误报（误报多了这个功能就没人看了）。
    """

    def __init__(self, module_names, rel):
        self.stack = [set(module_names) | _BUILTIN_NAMES]
        self.rel = rel
        self.out = []
        self.seen = set()

    def _known(self, nm):
        for s in reversed(self.stack):
            if nm in s:
                return True
        return False

    def _visit_comp(self, node):
        """推导式（生成器/列表/集合/字典）有自己的作用域。

        2026-11-15 实测误报：`(_ for _ in ()).throw(...)` 里 `_` 是生成器
        表达式的循环变量，但 visitor 没为推导式压栈 → 报「使用了 `_`，但本文件
        里没有定义」（high）。target 变量只在推导式内部可见，这里压个临时作用域。
        """
        names = set()
        for gen in getattr(node, "generators", []):
            for t in ast.walk(gen.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        self.stack.append(names)
        try:
            self.generic_visit(node)
        finally:
            self.stack.pop()

    def visit_GeneratorExp(self, node):
        self._visit_comp(node)

    def visit_ListComp(self, node):
        self._visit_comp(node)

    def visit_SetComp(self, node):
        self._visit_comp(node)

    def visit_DictComp(self, node):
        self._visit_comp(node)

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        nm = node.id
        if nm.startswith("__") and nm.endswith("__"):
            return
        if self._known(nm):
            return
        key = (getattr(node, "lineno", 0), nm)
        if key in self.seen:
            return
        self.seen.add(key)
        self.out.append(_finding(
            "high", "undefined-name", self.rel, getattr(node, "lineno", 0),
            "使用了 `%s`，但本文件里没有定义 / 赋值 / import 过它" % nm,
            "运行到这一行会抛 NameError"))

    def visit_Call(self, node):
        fn = node.func
        if isinstance(fn, ast.Name):
            # `foo(...)` 由 _check_undefined_call 负责，这里不重复报
            for a in node.args:
                self.visit(a)
            for k in node.keywords:
                self.visit(k.value)
            return
        self.generic_visit(node)

    def _enter_func(self, node):
        a = node.args
        # 装饰器与默认值在外层作用域求值
        for d in getattr(node, "decorator_list", []) or []:
            self.visit(d)
        for d in (getattr(a, "defaults", None) or []):
            self.visit(d)
        for d in (getattr(a, "kw_defaults", None) or []):
            if d is not None:
                self.visit(d)
        scope = set()
        for arg in ((getattr(a, "posonlyargs", None) or []) + (a.args or [])
                    + (getattr(a, "kwonlyargs", None) or [])):
            scope.add(arg.arg)
        if getattr(a, "vararg", None):
            scope.add(a.vararg.arg)
        if getattr(a, "kwarg", None):
            scope.add(a.kwarg.arg)
        globals_decl = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Global):
                globals_decl.update(n.names)
        scope |= (_scope_bound_body(node.body) - globals_decl)
        self.stack.append(scope)
        try:
            for st in node.body:
                self.visit(st)
        finally:
            self.stack.pop()

    def visit_FunctionDef(self, node):
        self._enter_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._enter_func(node)

    def visit_Lambda(self, node):
        a = node.args
        for d in (getattr(a, "defaults", None) or []):
            self.visit(d)
        scope = set()
        for arg in ((getattr(a, "posonlyargs", None) or []) + (a.args or [])
                    + (getattr(a, "kwonlyargs", None) or [])):
            scope.add(arg.arg)
        if getattr(a, "vararg", None):
            scope.add(a.vararg.arg)
        if getattr(a, "kwarg", None):
            scope.add(a.kwarg.arg)
        self.stack.append(scope)
        try:
            self.visit(node.body)
        finally:
            self.stack.pop()

    def visit_ClassDef(self, node):
        for d in node.decorator_list or []:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        self.stack.append(_scope_bound_body(node.body))
        try:
            for st in node.body:
                self.visit(st)
        finally:
            self.stack.pop()


def _check_undefined_name(trees):
    """用了一个本文件从没定义 / 赋值 / import 过的名字（NameError）。"""
    out = []
    for rel, (p, tree) in trees.items():
        if _star_imports(tree):
            continue
        known = _scope_bound_body(tree.body)
        v = _UndefNameVisitor(known, rel)
        try:
            for st in tree.body:
                v.visit(st)
        except RecursionError:
            continue
        out.extend(v.out)
    return out


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_dead_private_func(trees):
    """私有函数定义了却全项目只有定义处出现一次 —— 典型「加了函数没接线」。

    ⚠️ 保守规则：只看**模块级、以下划线开头、没有装饰器**的函数。
    带装饰器的可能被框架注册（路由/事件），按名字找不到调用点属正常；
    注释或字符串里提到过也算「出现过」，宁可漏报。
    """
    counter = {}
    # ⚠️ 用 _ref_files（backend + 根目录 + 前端）而不是只数 backend 的树：
    #   否则「根目录测试在用、backend 内部没人用」会被误报成死代码。
    for p in _ref_files():
        try:
            for w in _WORD_RE.findall(_read(p)):
                counter[w] = counter.get(w, 0) + 1
        except Exception:
            continue

    out = []
    for rel, (p, tree) in trees.items():
        src_lines = _read(p).splitlines()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nm = node.name
            if not nm.startswith("_") or nm.startswith("__"):
                continue
            if node.decorator_list:
                continue
            if counter.get(nm, 0) > 1:
                continue
            # 支持 `# noqa: dead-func` 显式豁免：有些函数是**有意保留**的
            # （如「已失效、保留仅为记录」的引擎），删了反而让后来者重复踩坑。
            # 只在 def 行本身和紧邻的上下 2 行里找，避免远处注释误豁免。
            _ln = getattr(node, "lineno", 0)
            _win = src_lines[max(0, _ln - 3): _ln + 2]
            if any("noqa" in _x.lower() for _x in _win):
                continue
            out.append(_finding(
                # ⚠️ 刻意用 low：接线要理解上下文，不能让信任模式自动改。
                "low", "dead-func", rel, getattr(node, "lineno", 0),
                "私有函数 `%s` 定义了，但全项目再没出现过这个名字" % nm,
                "很可能是「加了函数没接线」——改动看着成功其实没生效；接上调用点或删掉"))
    return out


# ---------------------------------------------------------------- 5. 裸 except / 静默吞异常

def _check_except(trees):
    out = []
    for rel, (p, tree) in trees.items():
        src_lines = _read(p).splitlines()
        silent = 0
        silent_first = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            ln = getattr(node, "lineno", 0) or 0
            tail = ""
            if 0 < ln <= len(src_lines):
                tail = src_lines[ln - 1]
            if node.type is None:
                out.append(_finding(
                    "mid", "bare-except", rel, ln,
                    "裸 `except:` 会连 KeyboardInterrupt / SystemExit 一起吞掉",
                    "改成 `except Exception as e:` 并记录日志"))
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                if "noqa" in tail.lower():
                    continue
                silent += 1
                if not silent_first:
                    silent_first = ln
        if silent >= _MAX_SILENT_PER_FILE:
            out.append(_finding(
                "info", "silent-except", rel, silent_first,
                "有 %d 处 `except ...: pass` 把异常完全吞掉了（未标 noqa）" % silent,
                "吞异常会让「功能静默失效」——这次踩过的坑就是它掩盖了 AttributeError"))
    return out


# ---------------------------------------------------------------- 6. 重复定义

def _deco_routes(node):
    """取出函数装饰器里的 (方法, 路径)，比如 ('get', '/api/secret/status')。"""
    out = []
    for d in getattr(node, "decorator_list", None) or []:
        if not isinstance(d, ast.Call):
            continue
        fn = d.func
        if not isinstance(fn, ast.Attribute):
            continue
        if not d.args:
            continue
        a0 = d.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            out.append((fn.attr, a0.value))
    return tuple(sorted(out))


def _check_duplicate_def(trees):
    """同名函数定义两次。

    ⚠️ 这里有个容易误报的细节：FastAPI 里两个同名函数挂在**不同路由**上时，
    装饰器在定义时就把路由注册好了，后一个覆盖前一个的名字**不影响功能**
    （这个项目里 library_delete 就是 doclib 一个、图片库一个，都是好的）。
    所以只有「路由也完全相同」或者「根本没有路由装饰器」才算真问题。
    """
    out = []
    for rel, (p, tree) in trees.items():
        seen = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            routes = _deco_routes(node)
            if node.name in seen:
                prev_ln, prev_routes = seen[node.name]
                if routes and prev_routes and routes != prev_routes:
                    # 同名但挂不同路由 → 功能正常，不报
                    seen[node.name] = (node.lineno, routes)
                    continue
                same = "（路由完全相同，后一个会盖掉前一个）" if routes and routes == prev_routes else ""
                out.append(_finding(
                    "mid", "duplicate-def", rel, node.lineno,
                    "`%s` 在同一文件里定义了两次（第 %d 行和第 %d 行）%s"
                    % (node.name, prev_ln, node.lineno, same),
                    "确认是不是复制粘贴留下的；路由相同时前一个等于白写"))
                seen[node.name] = (node.lineno, routes)
            else:
                seen[node.name] = (node.lineno, routes)
    return out


# ---------------------------------------------------------------- 7. 前后端路由一致性

_ROUTE_RE = re.compile(r"""@app\.(get|post|put|delete|patch)\(\s*["']([^"']+)["']""")
_FE_CALL_RE = re.compile(r"""["'`](/api/[A-Za-z0-9_\-/.]+)["'`]""")


def _backend_routes(paths):
    routes = []
    for p in paths:
        src = _read(p)
        for m in _ROUTE_RE.finditer(src):
            path = m.group(2)
            line = src.count("\n", 0, m.start()) + 1
            routes.append((path, _rel(p), line))
    return routes


def _route_matcher(path):
    """把 /api/x/{id} 变成可匹配前端具体路径的正则。

    前端经常写成 `"/api/avatar2/video/" + id`，正则只能抓到带尾斜杠的前缀。
    这种情况按**前缀匹配**处理，否则会误报一堆「路由不存在」。
    """
    parts = []
    for seg in path.split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(seg))
    tail = "" if path.endswith("/") else "$"
    return re.compile("^" + "/".join(parts) + tail)


def _check_routes(trees):
    """前端调了 `/api/xxx`，后端没有这个路由。

    这条同样是真实踩过的坑：前端调 `/api/selfiter/...` 之前，
    后端路由插错了位置（被缩进塞进函数体），路由没注册，前端 404。
    """
    routes = _backend_routes([p for p, _ in trees.values()])
    if not routes:
        return []
    matchers = [_route_matcher(r[0]) for r in routes]
    known = {r[0] for r in routes}
    out = []
    for p in _js_files():
        src = _read(p)
        seen = set()
        for m in _FE_CALL_RE.finditer(src):
            path = m.group(1)
            if path in seen or path in known:
                continue
            seen.add(path)
            if any(mt.match(path) for mt in matchers):
                continue
            line = src.count("\n", 0, m.start()) + 1
            out.append(_finding(
                "high", "missing-route", _rel(p), line,
                "前端调用了 `%s`，但后端没有注册这个路由" % path,
                "会拿到 404；要么路由没注册（插错位置），要么路径写错了"))
    return out


# ---------------------------------------------------------------- 8. JS 语法

def _node_exe():
    import shutil
    return shutil.which("node")


def _check_js_syntax():
    node = _node_exe()
    if not node:
        return [_finding("info", "js-syntax", "frontend/", 0,
                         "本机没找到 node，跳过 JS 语法检查")]
    out = []
    for p in _js_files():
        r = None
        err_obj = None
        # 实测踩过：同一个文件单独跑 `node --check` 是 rc=0，
        # 但在批量循环里偶尔返回非 0 且 stderr 为空（冷启动 / 杀软扫描）。
        # 这种必须重试一次；两次都拿不到错误信息就不报 —— 宁可漏报也不报假错。
        for _attempt in range(2):
            try:
                r = subprocess.run([node, "--check", str(p)],
                                   capture_output=True, text=True, timeout=25)
            except (subprocess.SubprocessError, OSError) as e:
                r, err_obj = None, e
                continue
            if r.returncode == 0 or (r.stderr or "").strip():
                break
        if r is None:
            out.append(_finding("info", "js-syntax", _rel(p), 0,
                                "JS 检查没跑起来：%s" % err_obj))
            continue
        if r.returncode != 0 and not (r.stderr or "").strip():
            continue
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            msg = err[0] if err else "node --check 失败"
            line = 0
            for ln in err:
                m = re.search(r":(\d+)\s*$", ln.strip())
                if m:
                    line = int(m.group(1))
                    break
            out.append(_finding("high", "js-syntax", _rel(p), line,
                                "JS 语法错误：%s" % msg[:180],
                                "前端会整份脚本加载失败，界面直接白掉"))
    return out


# ---------------------------------------------------------------- 汇总

# ---------------------------------------------------------------- 10. 逻辑类检查
# 2026-11-15 用户要求「逻辑 bug 也必须能查出来」。
# 下面 5 类是「语法合法、名字全对、行为却一定错」的固定形态，静态可判、几乎不误报。
# ⚠️ 边界必须说清：静态检查只能覆盖**有固定形态**的逻辑错。
#    off-by-one、条件写反、状态时序、并发竞争没有固定形态 —— 静态永远查不出来，
#    必须靠 regress.py 的行为级回归（真跑一遍、断言行为）来兜。
_MUTABLE_FACTORIES = ("list", "dict", "set", "bytearray", "deque",
                      "defaultdict", "Counter", "OrderedDict")


def _check_mutable_default(trees):
    """`def f(x=[])` / `={}` / `=set()`：默认值只在定义时求值一次，会被跨调用污染。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            pairs = []
            if a.defaults:
                pairs += list(zip(a.args[len(a.args) - len(a.defaults):], a.defaults))
            if a.kw_defaults:
                pairs += list(zip(a.kwonlyargs, a.kw_defaults))
            for arg, d in pairs:
                if d is None:
                    continue
                bad = isinstance(d, (ast.List, ast.Dict, ast.Set,
                                     ast.ListComp, ast.DictComp, ast.SetComp))
                if not bad and isinstance(d, ast.Call):
                    nm = getattr(d.func, "id", None) or getattr(d.func, "attr", None)
                    bad = nm in _MUTABLE_FACTORIES
                if bad:
                    out.append(_finding(
                        "mid", "mutable-default", rel,
                        getattr(d, "lineno", 0) or node.lineno,
                        "`%s()` 的参数 `%s` 用了可变默认值 —— 默认值只在定义时创建一次，"
                        "多次调用会共用同一个对象" % (node.name, arg.arg),
                        "改成 `%s=None`，函数体里 `if %s is None: %s = ...`"
                        % (arg.arg, arg.arg, arg.arg)))
    return out


def _check_is_literal(trees):
    """`x is "abc"` / `x is 5`：is 比的是对象身份，和字面量比几乎恒为 False。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comp in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Is, ast.IsNot)):
                    continue
                if not isinstance(comp, ast.Constant):
                    continue
                v = comp.value
                if v is None or v is Ellipsis or isinstance(v, bool):
                    continue            # is None / is True / is False 是正确写法
                out.append(_finding(
                    "mid", "is-literal", rel, getattr(comp, "lineno", 0) or node.lineno,
                    "`is` 用来比对象身份，和字面量 `%r` 比较几乎永远是 False" % (v,),
                    "字符串/数字一律用 `==`，只有 None/True/False 才用 `is`"))
    return out


def _check_assert_tuple(trees):
    """`assert (a, b)` —— 非空元组恒为真，断言等于没写。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) \
                    and node.test.elts:
                out.append(_finding(
                    "mid", "assert-tuple", rel, node.lineno,
                    "`assert (a, b)` 是非空元组，恒为真 —— 这个断言永远不会失败",
                    "去掉括号：`assert a, b`（b 是失败时的提示语）"))
    return out


_TERMINAL = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _check_unreachable(trees):
    """return/raise/break/continue 之后同一代码块里还有语句 —— 永远执行不到。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            for attr in ("body", "orelse", "finalbody"):
                block = getattr(node, attr, None)
                if not isinstance(block, list) or len(block) < 2:
                    continue
                for i, st in enumerate(block[:-1]):
                    if not isinstance(st, _TERMINAL):
                        continue
                    nxt = block[i + 1]
                    out.append(_finding(
                        "mid", "unreachable", rel, getattr(nxt, "lineno", 0) or 0,
                        "第 %d 行的 `%s` 之后还有语句，永远执行不到"
                        % (getattr(st, "lineno", 0), type(st).__name__.lower()),
                        "删掉不可达代码，或确认这里是不是漏了条件分支"))
                    break
    return out


def _check_return_in_finally(trees):
    """`finally` 里的 return 会把正在抛出的异常吞掉，错误就静默消失了。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for st in node.finalbody or []:
                for sub in ast.walk(st):
                    if isinstance(sub, ast.Return):
                        out.append(_finding(
                            "mid", "return-in-finally", rel, sub.lineno,
                            "`finally` 里的 return 会丢弃正在抛出的异常，错误被静默吞掉",
                            "把 return 移到 finally 之外；finally 只做清理"))
                        break
    return out


# ---------------------------------------------------------------- 逻辑形态
# 2026-11-15 用户要求「彻底解决：静态检查查不出逻辑 bug」。
# 诚实地说：off-by-one、条件写反这类**没有固定形态**，静态不可能全查出来
# （那要靠 regress.py 真跑断言）。但其中有几种**形态是固定的**，可以查：
#   · while True 里没有任何出口            → 一定卡死
#   · `x == x` 自比较                      → 结果恒定
#   · `range(len(x) - 1)`                  → 少遍历最后一个（off-by-one 经典形态）
#   · `for i in range(len(x))` 里 `x[i+1]` → 最后一轮越界
# 后两种只是「嫌疑」（也可能是刻意比较相邻元素），所以定 low：**报告但不自动改**，
# 免得模型把本来对的代码改错 —— 这正是用户反复强调的「不要误判乱改」。


def _walk_shallow(nodes):
    """遍历语句块，但**不进嵌套函数/类** —— 里面的 return/break 不算外层循环的出口。"""
    stack = list(nodes)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return "?"


def _check_while_true(trees):
    """`while True:` 里没有 break/return/raise —— 进去就出不来。"""
    out = []
    for rel, (p, tree) in trees.items():
        # ① 被 try/except 包着的 `while True` —— 靠捕获异常退出是标准写法
        #    （实测 main.py:12030 是 WebSocket 收包循环，靠 WebSocketDisconnect 退出）。
        guarded = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Try) and n.handlers:
                for b in n.body:
                    for s in ast.walk(b):
                        if isinstance(s, ast.While):
                            guarded.add(s.lineno)
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            t = node.test
            if not (isinstance(t, ast.Constant) and t.value is True):
                continue
            if node.lineno in guarded:
                continue
            body = list(_walk_shallow(node.body))
            if any(isinstance(s, (ast.Break, ast.Return, ast.Raise)) for s in body):
                continue
            # ⚠️ 这条规则的第一版在真实代码上误报了 2 处，都是**正确的服务循环**：
            #   main.py:12030  WebSocket 收包循环（靠 WebSocketDisconnect 异常退出）
            #   main.py:13220  队列 worker 的 `while True: job = q.get()`（阻塞等待）
            # 它们体内**没有任何 Call/Await 之外的东西**，静态分不出「故意常驻」
            # 和「忘了写 break」——而误判成 mid 会被信任模式自动改，直接搞死调度线程。
            # 所以只有「体内连一个调用都没有」的纯计算死循环才定 mid（必卡死、零误报）；
            # 有调用/Await 的一律 low：报告给人看，不自动改。
            # ⚠️ 2026-11-15 二次降噪：体内有 `sleep(...)` 的是**标准轮询循环**
            # （看门狗、调度器主循环都是这个形态，靠 daemon 线程随主进程退出），
            # 实测 5 条里 5 条都是这种，全误报 —— 一律不报。
            has_sleep = any(
                isinstance(s, ast.Call) and (
                    getattr(s.func, "id", None) == "sleep"
                    or getattr(s.func, "attr", None) == "sleep")
                for s in body)
            if has_sleep:
                continue
            # ② 队列 worker 形态：`while True: job = q.get()` —— 阻塞等待任务，
            #    跑在 daemon 线程里随主进程退出（实测 main.py:13220）。
            has_qget = any(
                isinstance(s, ast.Call) and getattr(s.func, "attr", None) == "get"
                for s in body)
            if has_qget:
                continue
            has_blocking = any(
                isinstance(s, (ast.Call, ast.Await)) for s in body)
            out.append(_finding(
                "low" if has_blocking else "mid", "while-true-no-exit", rel, node.lineno,
                "`while True` 里没有 break/return/raise —— 这个循环没有出口，会把线程卡死",
                "补一个出口条件（break），或改成 `while <条件>:`"))
    return out


def _check_self_compare(trees):
    """`x == x` / `x != x` —— 两边是同一个表达式，结果恒定。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            if isinstance(left, ast.Call):
                continue                    # 调用可能有副作用，不算自比较
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(comp, ast.Call):
                    continue
                if not isinstance(comp, (ast.Name, ast.Attribute, ast.Subscript)):
                    continue
                # ⚠️ 第一版把 `f != f` / `val != val` 全报了 —— 那是**标准 NaN 判断**
                # （唯一能判 NaN 的写法，真实代码里 3 处全是这个用途）。
                # 变量和自己做 ==/!= 一律视为 NaN 惯用法，不算 bug；
                # 只有 <、>、<=、>= 和自己比（恒假）才是真写错。
                if (isinstance(left, ast.Name) and isinstance(comp, ast.Name)
                        and isinstance(op, (ast.Eq, ast.NotEq))):
                    continue
                if ast.dump(left) == ast.dump(comp):
                    out.append(_finding(
                        "mid", "self-compare", rel, node.lineno,
                        "`%s` 两边是同一个表达式，比较结果恒定（==/<= 恒真，!=/< 恒假）"
                        % _unparse(node)[:70],
                        "这里应该是拿两个不同的值比较，检查是不是变量名写错了"))
    return out


# 滑窗/相邻比较特征：`x[i:i + 2]`、`x[i + 1]`
# 出现这些时，`range(len(x) - 1)` 是**必须**的写法（否则切片越界），不是 off-by-one。
_SLIDE_RE = re.compile(
    r"\[\s*\w+\s*:\s*\w+\s*\+\s*[1-9]\s*\]"
    r"|\[\s*\w+\s*\+\s*1\s*\]")


def _near_slide(p, lineno, span=4):
    """命中行附近是否出现滑窗切片 / 相邻索引。"""
    try:
        lines = _read(p).splitlines()
    except Exception:
        return False
    lo = max(0, lineno - 1 - span)
    hi = min(len(lines), lineno - 1 + span + 1)
    return bool(_SLIDE_RE.search("\n".join(lines[lo:hi])))


def _check_range_len_minus(trees):
    """`range(len(x) - 1)` —— 少遍历最后一个元素的经典 off-by-one 写法。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "range"):
                continue
            # ⚠️ 三参数的 range 一律不报：`range(len(x) - 1, -1, -1)` 是**倒序遍历**
            # （从最后一个元素往前走），是完全正确的写法。第一版没排除它，
            # 真实代码里 13 条有 8 条都是这个形态 —— 全是误报。
            if len(node.args) != 1:
                continue
            for a in node.args:
                if (isinstance(a, ast.BinOp) and isinstance(a.op, ast.Sub)
                        and isinstance(a.right, ast.Constant) and a.right.value == 1
                        and isinstance(a.left, ast.Call)
                        and getattr(a.left.func, "id", None) == "len"):
                    # ⚠️ 2026-11-15 二次降噪：实测 13 条**全部是正确写法** ——
                    #   ① 相邻比较 `for i in range(len(x)-1): x[i], x[i+1]`
                    #   ② 中文 bigram 切分 `ch[i:i+2]`（必须到 len-1，否则越界）
                    # 静态分不出「刻意少一个」和「写错少一个」，所以降到 info：
                    # 只作参考提示，不进 low 统计、更不会变成可执行动作。
                    # ★ 三次降噪（2026-11-15）：既然上面两种形态都是**必须**这么写，
                    #   就干脆识别出来不报 —— 上下文里有 `x[i:i+N]` 或 `x[i+1]` 即判定为
                    #   滑窗/相邻比较。这样真实代码从 13 条 → 0 条，只剩真可疑的。
                    if _near_slide(p, node.lineno):
                        continue
                    out.append(_finding(
                        "info", "range-len-minus", rel, node.lineno,
                        "`range(len(...) - 1)` 少遍历最后一个元素 —— 若是在比较相邻元素或做"
                        "滑窗/切片，这是正确写法；只有本意要遍历全部时才是 off-by-one",
                        "确认本意：要遍历全部 → 用 `range(len(x))`；比较相邻 → 可忽略"))
    return out


def _check_index_shift(trees):
    """`for i in range(len(x)):` 里访问 `x[i+1]` —— 最后一轮必然越界。"""
    out = []
    for rel, (p, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if not (isinstance(it, ast.Call)
                    and getattr(it.func, "id", None) == "range"):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            iv = node.target.id
            seqs = set()
            for a in it.args:
                if (isinstance(a, ast.Call) and getattr(a.func, "id", None) == "len"
                        and a.args and isinstance(a.args[0], ast.Name)):
                    seqs.add(a.args[0].id)
            if not seqs:
                continue
            for s in _walk_shallow(node.body):
                if not isinstance(s, ast.Subscript):
                    continue
                if not (isinstance(s.value, ast.Name) and s.value.id in seqs):
                    continue
                sl = s.slice
                if (isinstance(sl, ast.BinOp) and isinstance(sl.op, (ast.Add, ast.Sub))
                        and isinstance(sl.left, ast.Name) and sl.left.id == iv
                        and isinstance(sl.right, ast.Constant)
                        and isinstance(sl.right.value, int)):
                    out.append(_finding(
                        "low", "index-shift", rel, s.lineno,
                        "循环 `for %s in range(len(%s))` 里访问 `%s[%s%s%d]` —— "
                        "边界那一轮会越界" % (iv, s.value.id, s.value.id, iv,
                                            "+" if isinstance(sl.op, ast.Add) else "-",
                                            sl.right.value),
                        "把循环上界改小（range(len(x)-1)），或在访问前判断边界"))
                    break
    return out


def audit(deep_files=None):
    """跑一遍静态自检，返回 findings（按严重程度排序）。

    deep_files：可选，交给 LLM 深度审查的文件清单（由调用方决定，
    因为 LLM 审查要花 token，不适合每次自动跑）。
    """
    t0 = time.time()
    paths = _py_files()
    trees = {}
    for p in paths:
        src = _read(p)
        if not src.strip():
            continue
        try:
            trees[_rel(p)] = (p, ast.parse(src, filename=str(p)))
        except SyntaxError:
            # 语法错的文件进不了 AST 分析，_check_syntax 会单独报
            continue

    sym = _module_symbols(trees)
    findings = []
    findings += _check_syntax(paths)
    findings += _check_cross_module(trees, sym)
    findings += _check_undefined_call(trees)
    findings += _check_undefined_name(trees)
    findings += _check_dead_private_func(trees)
    findings += _check_except(trees)
    findings += _check_duplicate_def(trees)
    findings += _check_routes(trees)
    findings += _check_mutable_default(trees)
    findings += _check_is_literal(trees)
    findings += _check_assert_tuple(trees)
    findings += _check_unreachable(trees)
    findings += _check_return_in_finally(trees)
    findings += _check_while_true(trees)
    findings += _check_self_compare(trees)
    findings += _check_range_len_minus(trees)
    findings += _check_index_shift(trees)
    # ⚠️ 2026-11-15 补：这两个检查器早就写好了，但 audit() 里一直没调用 ——
    #    等于黑窗口那类问题从来没被静态检查覆盖过。补上接线。
    #    ★ 但直接接上会炸出 38 条告警（21 high + 17 mid），全是误报：
    #      判据是「本文件没 import procwin」，而 main.py 里 install_nowin_patch()
    #      是**全局**补丁，整个进程的子进程都被兜住了。所以先看有没有全局补丁：
    #      有 → 合并成 1 条 info 提示（不自动改）；没有 → 保持原级别（这时才是真风险）。
    _sub = _check_subprocess_bypass(trees) + _check_subprocess_nowin(trees)
    if _has_global_nowin_patch(trees):
        _seen, _locs = set(), []
        for _f in _sub:
            _k = (_f["file"], _f["line"])
            if _k in _seen:
                continue
            _seen.add(_k)
            _locs.append("%s:%d" % (Path(str(_f["file"])).name, _f["line"]))
        if _locs:
            findings.append(_finding(
                "info", "subprocess-global-patched", "backend/main.py", 76,
                "检测到 %d 处 subprocess 直接调用，但 procwin.install_nowin_patch() 已全局兜住"
                "（实测窗口数 0），故不逐处报 high/mid" % len(_locs),
                "显式传 creationflags=procwin.NOWIN 更稳妥；位置：" + ", ".join(_locs[:10])
                + ("…" if len(_locs) > 10 else "")))
    else:
        findings += _sub
    findings += _check_js_syntax()

    # 去重（同文件同种类同位置同描述只留一条）
    uniq, seen = [], set()
    for f in findings:
        k = (f["file"], f["kind"], f["line"], f["msg"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    uniq.sort(key=lambda f: (_LEVEL_ORDER.get(f["level"], 9), f["file"], f["line"]))

    by_level = {"high": 0, "mid": 0, "low": 0, "info": 0}
    for f in uniq:
        by_level[f["level"]] = by_level.get(f["level"], 0) + 1
    by_kind = {}
    for f in uniq:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1

    return {
        "ok": True,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": len(paths),
        "js_files": len(_js_files()),
        "seconds": round(time.time() - t0, 2),
        "counts": by_level,
        "kinds": by_kind,
        "total": len(uniq),
        "findings": uniq,
    }


def for_llm(result, limit=40):
    """把 findings 压成给 LLM 看的文本。"""
    items = (result or {}).get("findings") or []
    lines = []
    for f in items[:limit]:
        lines.append("[%s] %s:%d %s —— %s（%s）"
                     % (f["level"], f["file"], f["line"], f["kind"],
                        f["msg"], f.get("hint") or ""))
    if len(items) > limit:
        lines.append("…另有 %d 条同类问题未列出" % (len(items) - limit))
    return "\n".join(lines) if lines else "（本次静态检查没有发现问题）"
