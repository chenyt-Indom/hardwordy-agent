"""自主立项 —— 让智能体自己判断「用户需要什么」，并把新功能做成**可验证**的改动。

为什么需要这个模块
------------------
原有链路是：反思 → features（建议清单）→ 转成单条 action → 改一个文件。
三个硬缺口正好卡住「自主开发新功能」：

1. **一次动作最多改 3 个文件**（_MAX_FILES_PER_ACTION=3）。
   真正的新功能是「新模块 + 路由 + 前端 + 验收」，4 个起步，永远做不了。
2. **新功能没有断言**。通用回归是固定用例，新加的模块改坏了根本发现不了 ——
   用户明确要求「需要验证所有功能是否正常」，没有断言就无从验证。
3. **「需要什么」的输入只有对话摘要**，没有系统化地挖「我反复做不到的事」。

这个模块补的就是这三块：

    gaps()        从真实执行日志里挖反复失败的模式（成功率 / 回滚 / 同文件反复改）
    spec()        把一条 feature 变成「立项书」：多文件 + 验收标准 + 断言骨架
    write_test()  把断言落成 backend/_feature_tests/*.py，回归自动加载
    verify_one()  在**子进程**里真跑一条断言，确认新功能真的可用

设计原则
--------
· 宁可少提，不可乱提：所有输出都必须带**可核对的证据**（日志里的真实数字）。
· 新功能的断言必须**可执行**，不是一句"已验证"。
· 断言代码是模型生成的，会真的执行 —— 所以必须过安全检查 + 子进程 + 超时。
"""
import ast
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND = Path(__file__).resolve().parent
_ROOT = _BACKEND.parent
_TEST_DIR = _BACKEND / "_feature_tests"

_MARK = "@@FEATTEST@@"

# 起进程 / 改环境 / 联网 —— 不看上下文，命中即拒（见 _dangerous）
_DANGER_HARD = (
    "shutil.rmtree",            # 递归删目录，误删不可逆，一律禁
    "subprocess", "os.system",
    "os.popen", "os.exec", "os.spawn",
    "requests.", "urllib.", "socket.", "pip ",
    "sys.exit", "os._exit", "atexit",
)

# 文件破坏类 —— 不能只看字面，要用 AST 看**目标路径**（见 _dangerous_file_ops）
_FILE_OPS = {
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "shutil.move", "os.rename", "unlink", "remove",
}

# 视为「临时目录来源」的函数名
_TEMP_FUNCS = ("gettempdir", "mkdtemp", "mkstemp", "NamedTemporaryFile",
               "TemporaryDirectory", "TemporaryFile")


def _call_name(node):
    """把 ast.Call（或它的 func）还原成 "os.path.join" 这样的点号名。

    ⚠️ 必须先把 Call 拆成 .func —— 少了这一步，Call 本身不是 Attribute，
       循环一次都不进，永远返回空串，安全检查会**静默全部放行**（实测踩过）。
    """
    if isinstance(node, ast.Call):
        node = node.func
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _safe_temp_vars(tree):
    """收集「值来自 tempfile 临时目录」的变量名（含链式赋值）。"""
    safe = set()

    def _has_temp(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and n.attr in _TEMP_FUNCS:
                return True
            if isinstance(n, ast.Name) and n.id == "tempfile":
                return True
        return False

    for _ in range(3):                    # 迭代几轮，覆盖 a = b = c 链式
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            val = n.value
            ok = _has_temp(val) or any(
                isinstance(x, ast.Name) and x.id in safe for x in ast.walk(val))
            if not ok:
                continue
            for t in n.targets:
                if isinstance(t, ast.Name):
                    safe.add(t.id)
    return safe


def _arg_is_temp(arg, safe):
    """判断一个路径表达式是否指向临时目录。字面量路径一律不放行。"""
    if arg is None:
        return False
    if isinstance(arg, ast.Name):
        return arg.id in safe
    if isinstance(arg, ast.Constant):
        return False
    for n in ast.walk(arg):
        if isinstance(n, ast.Attribute) and n.attr in _TEMP_FUNCS:
            return True
        if isinstance(n, ast.Name) and (n.id in safe or n.id == "tempfile"):
            return True
    return False


def _dangerous_file_ops(tree):
    """用 AST 找「破坏/写入非临时路径」的调用。安全则返回空串。"""
    safe = _safe_temp_vars(tree)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = _call_name(n)
        if fn in _FILE_OPS:
            if not n.args:
                return "%s(无参数)" % fn
            if not _arg_is_temp(n.args[0], safe):
                return "%s(非临时路径)" % fn
        elif fn == "open":
            mode = ""
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = str(n.args[1].value)
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(c in mode for c in "wax+"):
                if not n.args or not _arg_is_temp(n.args[0], safe):
                    return "open(写非临时路径)"
    return ""

# 断言文件名里不能出现的字符（Windows 文件名限制 + 空白）
_BAD_NAME = re.compile(r'[\\/:*?"<>|\s]+')

# 断言代码里**绝对不允许**出现的危险调用。
# 理由：这段代码是模型写的、而且会真的执行。用户要求「想怎么搞就怎么搞，
# 只要不会搞坏」—— 那「搞坏」的底线就在这里：不许删文件、不许起进程、
# 不许改环境、不许联网。发现即拒绝写入，不是警告。
_DANGER = (
    "shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "Path.unlink", ".unlink(", "rmdir(", "subprocess", "os.system",
    "os.popen", "os.exec", "os.spawn", "shutil.move", "os.rename",
    "open(.*['\"]w", "requests.", "urllib.", "socket.", "pip ",
    "sys.exit", "os._exit", "atexit",
)


def _slug(title, fallback="feat"):
    """把中文标题转成安全的文件名（保留中文，只替换非法字符）。"""
    s = _BAD_NAME.sub("_", str(title or "").strip())
    s = s.strip("._-")[:40]
    return s or fallback


def _dangerous(code):
    """检查断言代码里有没有危险调用。返回命中的第一条，没有就返回空串。

    这是「不搞坏」的底线，不是可选优化：断言代码会真的在用户机器上执行。

    ★ 2026-11-15 重写：原来对 _DANGER 做**纯字面子串匹配**，一看到
      os.remove 就整条拒绝，不管删的是什么。于是「建临时文件 → 验证 →
      清理」这种测试代码的标准写法被判违规，f5/f6 两条功能的断言被整条
      丢弃 → 功能实现了却没有验收标准（assert_file 为空），自修循环也
      不会触发。现在拆成两层：
        · _DANGER_HARD：起进程/改环境/联网 —— 不看上下文，命中即拒；
        · 文件删除与写入 —— 用 AST 看**目标路径**是否来自 tempfile，
          是临时目录才放行，字面量路径（如 "D:/x"）一律拒绝。
    """
    low = str(code or "").lower()
    for pat in _DANGER_HARD:
        if pat.lower() in low:
            return pat
    try:
        tree = ast.parse(str(code or ""))
    except SyntaxError:
        return ""                      # 语法错误交给 check_code 单独报
    return _dangerous_file_ops(tree)


# ---------------------------------------------------------------- 能力缺口
def gaps(limit=6):
    """从**真实执行日志**里挖「反复做不到的事」，作为自主立项的输入。

    只统计可核对的事实：成功/失败次数、同一文件被反复改、失败动作的标题聚类。
    不做语义猜测 —— 猜出来的"用户需要"比没有更糟。

    返回：
        {"total":n, "ok":n, "fail":n, "rate":0.0~1.0,
         "hot_files":[{"file":..,"hits":n}], "fail_titles":[{"title":..,"n":n}],
         "summary":"一句话"}
    """
    out = {"total": 0, "ok": 0, "fail": 0, "rate": 0.0,
           "hot_files": [], "fail_titles": [], "summary": "（还没有执行记录）"}
    try:
        from . import self_iterate as SI
        log = SI.list_actlog(limit=300)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return out
    if not log:
        return out

    files, titles = {}, {}
    for it in log:
        if not isinstance(it, dict):
            continue
        out["total"] += 1
        ok = str(it.get("result") or "") == "ok"
        if ok:
            out["ok"] += 1
        else:
            out["fail"] += 1
            t = str(it.get("title") or "").strip()
            if t:
                k = t[:24]
                titles[k] = titles.get(k, 0) + 1
        for f in (it.get("files") or []):
            f = str(f or "").strip()
            if f:
                files[f] = files.get(f, 0) + 1

    if out["total"]:
        out["rate"] = round(out["ok"] / float(out["total"]), 3)
    # 同一文件被改 ≥3 次 = 反复没改对（实测 avatar_mouth.py 一天改 9 次）
    out["hot_files"] = [{"file": k, "hits": v}
                        for k, v in sorted(files.items(), key=lambda x: -x[1])
                        if v >= 3][:limit]
    out["fail_titles"] = [{"title": k, "n": v}
                          for k, v in sorted(titles.items(), key=lambda x: -x[1])
                          if v >= 2][:limit]

    bits = ["近 %d 次自动改动成功率 %.0f%%" % (out["total"], out["rate"] * 100)]
    if out["hot_files"]:
        bits.append("反复改同一文件：%s" % "、".join(
            "%s(%d次)" % (x["file"].split("/")[-1], x["hits"])
            for x in out["hot_files"][:3]))
    if out["fail_titles"]:
        bits.append("反复失败：%s" % "、".join(
            "%s(%d次)" % (x["title"], x["n"]) for x in out["fail_titles"][:3]))
    out["summary"] = "；".join(bits)
    return out


def gap_prompt():
    """把 gaps() 压成一段给反思 prompt 用的文字。"""
    g = gaps()
    if not g.get("total"):
        return ""
    lines = ["## 能力缺口（从真实执行日志统计，不是猜的）",
             "- %s" % g["summary"]]
    if g.get("hot_files"):
        lines.append("- ⚠️ 这些文件被反复改说明**上次没改对**，先想清楚根因，"
                     "不要再盲目叠加改动：")
        for x in g["hot_files"]:
            lines.append("  · %s 被自动改了 %d 次" % (x["file"], x["hits"]))
    if g.get("fail_titles"):
        lines.append("- ⚠️ 反复失败的动作（说明当前做法行不通，要换思路）：")
        for x in g["fail_titles"]:
            lines.append("  · %s（失败 %d 次）" % (x["title"], x["n"]))
    if g.get("rate") is not None and g["rate"] < 0.7:
        lines.append("- ⚠️ 成功率低于 70%%，**先提高可靠性，别急着加新功能**。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 断言文件
def _test_path(title):
    return _TEST_DIR / ("%s.py" % _slug(title))


def check_code(code):
    """校验断言代码。返回 (ok, why)。ok=False 时 why 说明原因。"""
    code = str(code or "").strip()
    if not code:
        return False, "断言代码为空"
    if "def run(" not in code:
        return False, "缺少 run() 函数"
    bad = _dangerous(code)
    if bad:
        return False, "断言代码里有危险调用（%s），拒绝写入" % bad
    try:
        compile(code, "<feature_test>", "exec")
    except SyntaxError as e:
        return False, "断言代码语法错误：第 %s 行 %s" % (e.lineno, e.msg)
    return True, ""


_BUILTIN_MODS = frozenset("""os sys re json time typing pathlib subprocess math random
collections itertools traceback logging shutil tempfile hashlib datetime ast glob
threading io string textwrap functools copy pickle base64 uuid socket struct csv
sqlite3 urllib html xml http asyncio concurrent dataclasses enum abc inspect
numpy PIL requests""".split())


def _wiring_code(code):
    """生成「接线断言」：被 import 的 backend 模块，其函数必须被其他文件真实调用。

    为什么需要（2026-11-15 用户指出「建好了但没接线」）
    ---------------------------------------------------
    实测「回复形态闸门」：模块 reply_gate.py 建出来了、自身断言也通过，
    但 main.py 里只有 `import reply_gate`、**从未调用 format_reply** ——
    功能建好却没生效。断言只验模块自身行为，验不出这个盲区。

    规则：断言里 `from X import f` 的 X（且 backend/X.py 存在），
    必须在 backend/ 下**其他 .py** 里出现 `f(` 形式的调用。
    只 import 不算接线。

    ⚠️ 接不上时断言会失败 → 触发既有的自修循环，让模型自己去调用方补一行。
    这正是我们要的：不靠人肉记得接线。
    """
    text = str(code or "")
    pairs, seen = [], set()
    for m in re.finditer(r"^\s*from\s+([A-Za-z_]\w*)\s+import\s+([^\n#]+)", text, re.M):
        mod, names = m.group(1), m.group(2)
        if mod in _BUILTIN_MODS or mod in seen:
            continue
        fns = []
        for n in re.split(r"[,()]", names):
            n = n.strip().split(" as ")[0].strip()
            if re.match(r"^[A-Za-z_]\w*$", n):
                fns.append(n)
        if fns:
            seen.add(mod)
            pairs.append((mod, fns))
    if not pairs:
        return ""
    rows = "".join("        (%r, %r),\n" % (m, f) for m, f in pairs)
    return (
        "\n\n# ---- 接线断言（自动生成，2026-11-15）----\n"
        "# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，\n"
        "# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。\n"
        "def _wiring_check():\n"
        "    import os as _wo, re as _wr\n"
        "    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))\n"
        "    _pairs = [\n" + rows + "    ]\n"
        "    _bad = []\n"
        "    for _mod, _fns in _pairs:\n"
        "        if not _wo.path.isfile(_wo.path.join(_be, _mod + '.py')):\n"
        "            continue\n"
        "        _hit = False\n"
        "        for _fn in _fns:\n"
        "            for _f in _wo.listdir(_be):\n"
        "                if not _f.endswith('.py') or _f == _mod + '.py':\n"
        "                    continue\n"
        "                try:\n"
        "                    _s = open(_wo.path.join(_be, _f), encoding='utf-8', errors='ignore').read()\n"
        "                except Exception:\n"
        "                    continue\n"
        "                if _wr.search(r'\\b' + _wr.escape(_fn) + r'\\s*\\(', _s):\n"
        "                    _hit = True\n"
        "                    break\n"
        "            if _hit:\n"
        "                break\n"
        "        if not _hit:\n"
        "            _bad.append('%s 里的 %s 没有被任何其他模块调用（只 import 不算接线）'\n"
        "                        % (_mod, '/'.join(_fns)))\n"
        "    assert not _bad, '功能未接入主链路：' + '；'.join(_bad)\n\n"
        "_orig_run = globals().get('run')\n"
        "def run():\n"
        "    if _orig_run:\n"
        "        _orig_run()\n"
        "    _wiring_check()\n"
    )


def write_test(title, code, note=""):
    """把一条断言写成 backend/_feature_tests/<slug>.py。返回 (ok, path_or_why)。

    ⚠️ 已存在同名文件时**覆盖**——同一条功能被反复立项时，断言应该是最新那版。
    """
    ok, why = check_code(code)
    if not ok:
        return False, why
    p = _test_path(title)
    # ★ 2026-11-15 修致命 bug（实测 f8 因此被误判失败并回滚）：
    #   断言文件落在 backend/_feature_tests/ 下，但模型写的是
    #   sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))
    #   → 算出 backend/_feature_tests/backend（不存在的目录）
    #   → 新模块明明建出来了，却报 ModuleNotFoundError → 自修 3 轮全败 → 回滚删除。
    #   这里在文件头**强制注入**正确的两条路径，模型写的那行留着也无害。
    header = ('"""新功能验收断言：%s\n\n%s\n\n由自主立项自动生成，回归会自动加载它。\n'
              '出问题时直接删掉这个文件即可，不影响其他用例。\n"""\n'
              'import os as _os, sys as _sys\n'
              '_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))\n'
              '_sys.path.insert(0, _B)\n'
              '_sys.path.insert(0, _os.path.dirname(_B))\n\n'
              % (title, (note or "").strip()))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(header + str(code).strip() + "\n" + _wiring_code(code), encoding="utf-8")
        return True, str(p.relative_to(_ROOT).as_posix())
    except Exception as e:                              # noqa: BLE001
        return False, "写入失败：%s" % e


def _parked_path(title):
    return _TEST_DIR / ("_pending_%s.py" % _slug(title))


def park_test(title):
    """把断言「挂起」：重命名为 _pending_<slug>.py。返回 True 表示已挂起。

    为什么必须挂起
    --------------
    新功能的断言描述的是「功能做完之后应该成立的事」。在动作真正改完代码
    **之前**，它必然失败。如果直接留在 _feature_tests/ 里，每次回归都会红 ——
    回归一旦长期红，就没人再当回事了（狼来了），整套验证失去意义。

    挂起的文件以下划线开头，regress._load_dynamic() 会跳过它；
    等动作执行成功后调 unpark_test() 恢复启用再验证。
    """
    p, q = _test_path(title), _parked_path(title)
    try:
        if p.is_file():
            p.replace(q)
            return True
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return False


def unpark_test(title, path=None):
    """把挂起的断言恢复启用（动作执行成功后调用）。返回 True 表示已恢复。

    path 给了就按真实文件定位（动作里的 assert_file），否则按 title 推导 ——
    标题被截断或与断言标题不一致时，只有按路径才能找对文件。
    """
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = _ROOT / str(path)
        q = p.parent / ("_pending_%s" % p.name)
    else:
        p, q = _test_path(title), _parked_path(title)
    try:
        if q.is_file():
            q.replace(p)
            return True
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return False


def remove_test(title):
    """删掉一条断言文件（回滚时用）。"""
    hit = False
    for p in (_test_path(title), _parked_path(title)):
        try:
            if p.is_file():
                p.unlink()
                hit = True
        except Exception:                               # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
    return hit


def verify_one(title, timeout=40, path=None):
    """在**子进程**里真跑一条断言。返回 {"ok":bool,"why":str,"seconds":float}。

    为什么必须子进程：刚写盘的新代码在当前进程里还是旧模块，进程内跑等于
    在验改动前的代码（这个坑在 regress.run_disk 里已经踩过一次）。

    ★ 2026-11-15 修致命 bug：原来只用 title 反推文件名。但动作标题会经过
      `title[:120]` 截断、也可能与断言标题不同（实测：动作叫「新建中文数字
      统计模块 count_chars」，断言文件叫「E2E自动新功能演示_中文数字统计」），
      于是「断言文件不存在」→ 3 轮自修白跑 → **把正确的实现回滚掉**。
      现在优先用调用方给的真实路径 path（就是动作里的 assert_file）。
    """
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = _ROOT / str(path)
    else:
        p = _test_path(title)
    t0 = time.time()
    if not p.is_file():
        return {"ok": False, "why": "断言文件不存在：%s" % p.name,
                "seconds": 0.0}
    runner = (
        "import io,sys\n"
        "sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')\n"
        "sys.path.insert(0,'.')\n"
        "import importlib.util\n"
        "spec=importlib.util.spec_from_file_location('_ft',%r)\n"
        "m=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "m.run()\n"
        "print(%r)\n" % (str(p), _MARK)
    )
    try:
        r = subprocess.run([sys.executable, "-c", runner], cwd=str(_ROOT),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "断言超时（>%ds）" % timeout,
                "seconds": round(time.time() - t0, 2)}
    sec = round(time.time() - t0, 2)
    sout = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode == 0 and _MARK in sout:
        return {"ok": True, "why": "", "seconds": sec}
    serr = (r.stderr or b"").decode("utf-8", "replace")
    # 只挑最有用的一行：优先 AssertionError 的内容，否则最后一行
    why = ""
    for ln in reversed(serr.strip().splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("AssertionError"):
            why = ln
            break
        if not why:
            why = ln
    return {"ok": False, "why": (why or "断言进程退出码 %s" % r.returncode)[:400],
            "seconds": sec}


def list_tests():
    """列出所有新功能断言文件及其状态（不执行）。"""
    out = []
    try:
        if not _TEST_DIR.is_dir():
            return out
        for p in sorted(_TEST_DIR.glob("*.py")):
            if p.name.startswith("_"):
                continue
            out.append({"name": p.stem,
                        "file": p.relative_to(_ROOT).as_posix(),
                        "size": p.stat().st_size})
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return out


def verify_all(timeout=40):
    """跑一遍所有新功能断言。返回汇总。"""
    t0 = time.time()
    items = []
    for t in list_tests():
        r = verify_one(t["name"], timeout=timeout)
        items.append({"name": t["name"], "ok": r["ok"], "why": r["why"],
                      "seconds": r["seconds"]})
    ok_n = sum(1 for i in items if i["ok"])
    return {"ok": ok_n == len(items), "total": len(items), "passed": ok_n,
            "failed": len(items) - ok_n,
            "seconds": round(time.time() - t0, 2),
            "items": items,
            "summary": ("%d 条新功能断言全部通过" % ok_n) if ok_n == len(items)
                       else ("%d/%d 条新功能断言失败：%s" % (
                           len(items) - ok_n, len(items),
                           "；".join("%s（%s）" % (i["name"], i["why"][:60])
                                    for i in items if not i["ok"])[:300]))}


def status():
    """给接口/前端用的一览。"""
    ts = list_tests()
    g = gaps()
    return {"tests": ts, "count": len(ts), "gaps": g,
            "dir": str(_TEST_DIR.relative_to(_ROOT).as_posix())}
