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

_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
_FRONTEND = _ROOT / "frontend"

_MAX_BYTES = 2_500_000          # 超过这个大小的文件跳过（不是人手写的）
_MAX_SILENT_PER_FILE = 1        # 静默 except 每个文件只汇总成 1 条

_LEVEL_ORDER = {"high": 0, "mid": 1, "low": 2, "info": 3}


# ---------------------------------------------------------------- 基础工具

def _py_files():
    out = []
    for p in sorted(_BACKEND.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            if p.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
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
    for rel, (p, tree) in trees.items():
        for w in _WORD_RE.findall(_read(p)):
            counter[w] = counter.get(w, 0) + 1

    out = []
    for rel, (p, tree) in trees.items():
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
