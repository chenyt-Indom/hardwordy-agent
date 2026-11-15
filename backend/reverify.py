"""自检结果的「复核」层 —— 修之前先反复确认，防止误诊乱改。

为什么需要它
------------
code_audit 是**确定性**检查器：同一份代码重跑必然得到同样结果。
所以「再跑一次自检」在数学上等于没复核 —— 它只会重复同一个结论。

真正的复核必须**换方法**，四轮互相独立：

    R1 上下文豁免   读那几行的真实代码，看是不是静态检查的已知盲区
                    （星号导入 / 动态注入 / 字符串内 / 注释行）
    R2 独立重算     用另一套 AST 逻辑重算同一件事，不一致就说明原检查器可能错
    R3 真跑验证     能编译的真编译、能导入的真导入
    R4 语义复核     LLM 读代码判断真假，采样 2 次，结论必须一致

判定规则
--------
    任一轮「否决」        → rejected      不修
    任一轮「说不准」      → inconclusive  不自动修，转人工
    四轮全部通过          → verified      信任模式可以自动修

R4 只在有 Key 且 finding 是 high/mid 时才跑（省 token）；没有 Key 时
前两轮通过就判 verified，并在 rounds 里如实标注「未做语义复核」。
"""
import ast
import io
import json
import logging
import re
import subprocess
import sys
import time
import tokenize
from pathlib import Path

from . import jsonfix as _jsonfix

logger = logging.getLogger(__name__)

_BACKEND = Path(__file__).resolve().parent
_ROOT = _BACKEND.parent

# 这些 kind 属于「信息类」，本来就不进自动修复，只做轻量复核
_LIGHT_KINDS = {"dead-func", "silent-except", "missing-route", "route-mismatch"}


# ------------------------------------------------------------------ 基础
def _abs(rel):
    """相对路径 → 绝对路径；越界或不存在返回 None。"""
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    p = (_ROOT / rel).resolve()
    try:
        p.relative_to(_ROOT)
    except ValueError:
        return None
    return p if p.is_file() else None


def _src(rel):
    p = _abs(rel)
    if not p:
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _window(src, line, before=4, after=3):
    """取 line 附近的一段源码（1 基行号）。"""
    lines = src.splitlines()
    lo = max(0, int(line or 1) - 1 - before)
    hi = min(len(lines), int(line or 1) + after)
    return "\n".join(lines[lo:hi]), (lo + 1, hi)


# ------------------------------------------------------- R1 上下文豁免
# 基于 AST 的检查不可能把注释/字符串当代码，所以它们**不需要**行级豁免；
# 只有基于正则的检查才需要。早先一刀切按行豁免，结果「行尾带注释」的
# 真 bug 也被放过了 —— 这个区分是必须的。
_AST_KINDS = {"undefined-call", "undefined-name", "duplicate-def",
              "bare-except", "syntax"}
_REGEX_KINDS = {"dead-func", "missing-route", "route-mismatch", "silent-except"}

_GLOBALS_KEY = re.compile(r"""globals\s*\(\s*\)\s*\[\s*['"]([^'"]+)['"]\s*\]\s*=""")
_LOCALS_KEY = re.compile(r"""locals\s*\(\s*\)\s*\[\s*['"]([^'"]+)['"]\s*\]\s*=""")
_SETATTR_KEY = re.compile(
    r"""setattr\s*\(\s*(?:globals|locals|self|cls|__import__\S*)\s*,\s*['"]([^'"]+)['"]""")
_NAME_IN_MSG = re.compile(r"`([_A-Za-z][_A-Za-z0-9]*)[^`]*`")


def _extract_name(msg):
    """从 finding 的 msg 里取出被反引号包住的标识符（函数名/变量名）。"""
    m = _NAME_IN_MSG.search(str(msg or ""))
    return m.group(1) if m else ""


def _line_text(src, lineno):
    lines = src.splitlines()
    ln = int(lineno or 0)
    return lines[ln - 1] if 1 <= ln <= len(lines) else ""


def _is_comment_only(src, lineno):
    """整行都是注释（行首就是 #）。**行尾注释不算**。"""
    txt = _line_text(src, lineno)
    s = txt.strip()
    return bool(s) and s.startswith("#")


def _star_imported_names(src):
    """解析 `from X import *`，真的去 X 里取公开名字。

    取不到（相对导入 / 模块不可导入）就返回空集 —— 宁可少豁免，
    也不能因为「文件里有星号导入」就把整份文件的真 bug 全放过。
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if not isinstance(n, ast.ImportFrom):
            continue
        if not any(a.name == "*" for a in n.names):
            continue
        if n.level:                       # 相对导入：不冒险
            continue
        mod = n.module or ""
        if not mod:
            continue
        try:
            import importlib
            m = importlib.import_module(mod)
        except Exception:                 # noqa: BLE001
            logger.debug("silent-except", exc_info=True)
            continue
        out |= {k for k in dir(m) if not k.startswith("_")}
    return out


def _dyn_injected_names(src):
    """收集 globals()['x']= / setattr(..., 'x', ...) 这类动态注入的名字。"""
    out = set()
    for pat in (_GLOBALS_KEY, _LOCALS_KEY, _SETATTR_KEY):
        out |= set(pat.findall(src))
    return out


def _ctx_exempt(rel, line, name="", kind=""):
    """R1：针对**具体名字**判断这条 finding 是否属于静态检查盲区。

    返回 (exempt, why)。exempt=True → 不能信，否决。

    ⚠️ 关键设计：豁免必须精确到名字。
    早先按「文件里有星号导入 → 整份文件全豁免」，结果连
    `this_function_really_missing()` 这种真 bug 也被当成误报放过了。
    """
    src = _src(rel)
    if not src:
        return False, ""

    # ① 正则类检查才需要「整行注释」豁免
    if kind in _REGEX_KINDS and _is_comment_only(src, line):
        return True, "该行整行都是注释，正则类检查容易把它当代码"

    # ② 拿不到具体名字 → 无法精确判断，不豁免（宁可多查一遍）
    if not name:
        return False, ""

    # ③ 这个名字真的来自星号导入？
    if name in _star_imported_names(src):
        return True, "`%s` 确实来自 `from x import *`，静态检查看不到它" % name

    # ④ 这个名字真的是动态注入的？
    if name in _dyn_injected_names(src):
        return True, "`%s` 是 globals()/setattr 动态注入的，运行时才存在" % name

    # ⑤ 名字出现在 eval/exec 的字符串里
    win, _rng = _window(src, line)
    if re.search(r"""(eval|exec)\s*\(\s*['"][^'"]*\b%s\b""" % re.escape(name), win):
        return True, "`%s` 出现在 eval/exec 的字符串里，运行时才解析" % name

    return False, ""


# --------------------------------------------------------- R2 独立重算
_BUILTINS = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)


def _bound_names(tree):
    """收集文件里所有「被绑定过」的名字（定义/导入/赋值/参数/except/with）。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global):
            out.update(n.names)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    for m in ast.walk(it.optional_vars):
                        if isinstance(m, ast.Name):
                            out.add(m.id)
    return out


def _names_near(tree, line, kinds, span=3):
    """取 line 附近 span 行内出现的指定节点里的 Name。"""
    out = []
    ln = int(line or 0)
    for n in ast.walk(tree):
        if not isinstance(n, kinds):
            continue
        if not (ln - span <= getattr(n, "lineno", 0) <= ln + span):
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.append(n.func.id)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            base = n.value
            if isinstance(base, ast.Name):
                out.append(base.id)
    return out


def _recompute(rel, kind, line):
    """R2：用另一套独立逻辑重算。返回 (agree, detail)。

    agree=True  重算后问题仍在 → 支持原结论
    agree=False 重算后问题不存在 → 原检查器可能误报，否决
    agree=None  这种 kind 不支持重算（不算否决）
    """
    src = _src(rel)
    if not src:
        return None, "读不到文件内容"
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        # 文件有语法错：只有 syntax 类 finding 可信
        return (kind in ("syntax", "js-syntax")), "文件有语法错（第 %s 行），只能做语法层判断" % (e.lineno or "?")

    if kind in ("undefined-call", "undefined-name"):
        names = _names_near(tree, line, (ast.Call, ast.Name, ast.Attribute))
        bound = _bound_names(tree) | _BUILTINS
        bad = [n for n in names if n not in bound]
        if bad:
            return True, "独立重算：附近仍引用了未绑定的名字 %s" % ", ".join(sorted(set(bad))[:4])
        return False, "独立重算：附近引用的名字全部有绑定，原结论站不住"

    if kind == "duplicate-def":
        cnt = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                cnt[n.name] = cnt.get(n.name, 0) + 1
        dup = {k: v for k, v in cnt.items() if v > 1}
        if dup:
            return True, "独立重算：确实有重名定义 %s" % ", ".join("%s×%d" % (k, v) for k, v in list(dup.items())[:3])
        return False, "独立重算：没有任何重名定义，原结论站不住"

    if kind in ("bare-except", "silent-except"):
        bare = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.type is None]
        if bare:
            return True, "独立重算：确实有 %d 处裸 except" % len(bare)
        return False, "独立重算：没有裸 except，原结论站不住"

    if kind == "syntax":
        return True, "独立重算：编译确实不通过"

    if kind == "dead-func":
        # 从该行的 def 语句里取函数名（finding 的行号指向 def 所在行）
        fm = re.search(r"def\s+([_A-Za-z][_A-Za-z0-9]*)", _msg_of(rel, line))
        name = fm.group(1) if fm else ""
        if not name:
            return None, "拿不到函数名"
        whole = _src(rel)
        hits = len(re.findall(r"\b%s\b" % re.escape(name), whole))
        if hits <= 1:
            return True, "独立重算：`%s` 全文件只出现 %d 次（只有定义）" % (name, hits)
        return False, "独立重算：`%s` 全文件出现 %d 次，有调用点" % (name, hits)

    return None, "这种检查项不支持独立重算"


def _msg_of(rel, line):
    """取该行源码文本（用于从上下文推断函数名）。"""
    src = _src(rel)
    lines = src.splitlines()
    ln = int(line or 0)
    if 1 <= ln <= len(lines):
        return lines[ln - 1]
    return ""


# ------------------------------------------------------- R3 真跑验证
def _node_check(p):
    node = None
    for cand in ("node", "node.exe"):
        from shutil import which
        node = which(cand)
        if node:
            break
    if not node:
        return None, "本机没有 node，跳过 JS 真跑"
    try:
        r = subprocess.run([node, "--check", str(p)], capture_output=True,
                           text=True, timeout=25, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "node 跑不起来：%s" % e
    return (r.returncode == 0), (r.stderr or "")[-260:]


def _import_check(rel):
    """真的 import 这个模块，看模块级是否崩。"""
    mod = rel.replace("\\", "/")
    if mod.endswith(".py"):
        mod = mod[:-3]
    mod = mod.replace("/", ".")
    if not mod.startswith("backend."):
        return None, "不在 backend 包内，不做导入验证"
    try:
        r = subprocess.run([sys.executable, "-c", "import %s" % mod],
                           capture_output=True, text=True, timeout=45,
                           cwd=str(_ROOT), encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "导入进程起不来：%s" % e
    if r.returncode == 0:
        return True, "真导入 %s 成功（模块级没有崩）" % mod
    tail = (r.stderr or "").strip().splitlines()
    return False, "真导入失败：%s" % (tail[-1][:200] if tail else "rc=%d" % r.returncode)


def _runtime_check(rel, kind):
    """R3：能真跑就真跑。返回 (ok, detail)，ok=None 表示不适用。"""
    p = _abs(rel)
    if not p:
        return None, "文件不存在，跳过真跑"
    if rel.lower().endswith(".js"):
        return _node_check(p)
    if kind == "syntax":
        try:
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                               capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            return None, "编译进程起不来：%s" % e
        if r.returncode == 0:
            return True, "真编译通过（与原结论冲突，原检查器可能误报）"
        tail = (r.stderr or "").strip().splitlines()
        return False, "真编译失败：%s" % (tail[-1][:200] if tail else "")
    if kind in ("undefined-call", "undefined-name"):
        return _import_check(rel)
    return None, ""


# ------------------------------------------------------- R4 语义复核
def _llm_once(prompt, max_tokens=1800, temperature=0.2):
    try:
        from . import keyvault
        from .deepseek_client import chat_stream, DeepSeekError
    except Exception as e:                              # noqa: BLE001
        return None, "导入 LLM 客户端失败：%s" % e
    key = ""
    try:
        key = keyvault.get_key()
    except Exception:                                   # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key"
    try:
        buf = []
        for kind, txt in chat_stream([{"role": "user", "content": prompt}],
                                     "deepseek-flash", key,
                                     temperature=temperature,
                                     max_tokens=max_tokens, thinking=False,
                                     # 取 JSON 时不能把截断提示混进正文（2026-11-16）
                                     truncation_notice=False):
            if kind == "content" and txt:
                buf.append(txt)
        return _jsonfix.strip_notice("".join(buf)), None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    """从 LLM 输出里抠出 JSON（含截断修复）。实现见 backend/jsonfix.py。"""
    return _jsonfix.parse(txt)


def _llm_review(items, rounds=2):
    """R4：让 LLM 读真实代码判断，采样 rounds 次，结论必须一致。

    items: [{'id', 'file', 'line', 'kind', 'msg', 'code'}]
    返回 {id: ('yes'|'no'|'unsure', 说明)}
    """
    if not items:
        return {}, "没有需要语义复核的条目"
    blocks = []
    for it in items:
        blocks.append("### 条目 %s\n文件：%s　第 %s 行\n检查器判定：%s\n```\n%s\n```"
                      % (it["id"], it["file"], it["line"], it["msg"], it["code"]))
    prompt = """你是资深 Python 维护者。下面每条都是**静态检查器**报出的问题。请判断：**这一条是不是真的会出问题**。

%s

## 判断标准
- 真的会导致崩溃 / 功能失效 / 静默错误 → real=true
- 属于静态检查的已知盲区（星号导入、动态注入、运行时才定义、框架约定、故意为之）→ real=false
- 代码片段信息不足以判断 → real="unsure"
- **不要**因为「代码风格不好」判 true，只看会不会真出事

## 输出（只输出 JSON，不要解释）
{"verdicts":[{"id":1,"real":true,"why":"20 字内理由"}]}
""" % "\n\n".join(blocks)

    votes = {}
    for i in range(max(1, int(rounds or 1))):
        txt, err = _llm_once(prompt, max_tokens=1500, temperature=0.15 + 0.35 * i)
        if err:
            return {}, "语义复核不可用：%s" % err
        data = _parse_json(txt)
        if not isinstance(data, dict):
            return {}, "语义复核没给出合法 JSON"
        for v in data.get("verdicts") or []:
            if not isinstance(v, dict):
                continue
            try:
                vid = int(v.get("id"))
            except (TypeError, ValueError):
                continue
            raw = v.get("real")
            if isinstance(raw, bool):
                val = "yes" if raw else "no"
            elif isinstance(raw, str) and raw.lower() in ("true", "yes"):
                val = "yes"
            elif isinstance(raw, str) and raw.lower() in ("false", "no"):
                val = "no"
            else:
                val = "unsure"
            votes.setdefault(vid, []).append((val, str(v.get("why") or "")[:80]))

    out = {}
    for vid, vs in votes.items():
        vals = [v[0] for v in vs]
        why = vs[0][1] if vs else ""
        if all(v == "yes" for v in vals):
            out[vid] = ("yes", why or "两次语义复核都判为真问题")
        elif all(v == "no" for v in vals):
            out[vid] = ("no", why or "两次语义复核都判为误报")
        else:
            out[vid] = ("unsure", "两次语义复核结论不一致（%s），不敢断定" % "/".join(vals))
    return out, ""


# --------------------------------------------------------------- 主入口
def verify_findings(findings, use_llm=True, rounds=2, max_llm=10):
    """对每条 finding 做多轮复核，返回带 verify 字段的副本。

    绝不抛异常：复核本身出问题只会让结论变成 inconclusive，
    不会让整条自检链路崩掉。
    """
    t0 = time.time()
    items = list(findings or [])
    out = []
    for f in items:
        rec = dict(f)
        rec["verify"] = {"verdict": "inconclusive", "rounds": [], "seconds": 0.0}
        out.append(rec)

    # ---- R1 上下文豁免（快，先做，能挡掉一批）
    for rec in out:
        v = rec["verify"]
        try:
            ex, why = _ctx_exempt(rec.get("file"), rec.get("line"),
                                  _extract_name(rec.get("msg")), rec.get("kind"))
        except Exception as e:                          # noqa: BLE001
            ex, why = False, "R1 出错：%s" % e
        v["rounds"].append({"round": "R1-上下文豁免", "pass": (not ex),
                            "why": why or "上下文里没有豁免特征"})
        if ex:
            v["verdict"] = "rejected"
            v["why"] = "R1 否决：" + why

    # ---- R2 独立重算
    for rec in out:
        v = rec["verify"]
        if v["verdict"] == "rejected":
            continue
        try:
            agree, detail = _recompute(rec.get("file"), rec.get("kind"), rec.get("line"))
        except Exception as e:                          # noqa: BLE001
            agree, detail = None, "R2 出错：%s" % e
        ok = (agree is not False)
        v["rounds"].append({"round": "R2-独立重算", "pass": ok,
                            "why": detail or "重算完成"})
        if agree is False:
            v["verdict"] = "rejected"
            v["why"] = "R2 否决：" + detail

    # ---- R3 真跑验证
    for rec in out:
        v = rec["verify"]
        if v["verdict"] == "rejected":
            continue
        try:
            ok, detail = _runtime_check(rec.get("file"), rec.get("kind"))
        except Exception as e:                          # noqa: BLE001
            ok, detail = None, "R3 出错：%s" % e
        v["rounds"].append({"round": "R3-真跑验证",
                            "pass": (ok is not False),
                            "why": detail or "这种检查项无需真跑"})
        if ok is False:
            v["verdict"] = "rejected"
            v["why"] = "R3 否决：" + detail

    # ---- R4 语义复核（只对 high/mid，且不超过 max_llm 条）
    llm_items, id_map = [], {}
    if use_llm:
        cand = [r for r in out
                if r["verify"]["verdict"] != "rejected"
                and r.get("level") in ("high", "mid")
                and r.get("kind") not in _LIGHT_KINDS]
        for i, r in enumerate(cand[:max_llm], start=1):
            src = _src(r.get("file"))
            win, rng = _window(src, r.get("line"), before=8, after=10)
            if not win.strip():
                continue
            llm_items.append({"id": i, "file": r.get("file"),
                              "line": r.get("line"), "kind": r.get("kind"),
                              "msg": r.get("msg") or "", "code": win})
            id_map[i] = r
        if llm_items:
            try:
                verdicts, err = _llm_review(llm_items, rounds=rounds)
            except Exception as e:                      # noqa: BLE001
                verdicts, err = {}, "R4 出错：%s" % e
            for i, r in id_map.items():
                v = r["verify"]
                if err:
                    v["rounds"].append({"round": "R4-语义复核", "pass": True,
                                        "why": "未执行：" + err})
                    continue
                val, why = verdicts.get(i, ("unsure", "模型没给出这一条"))
                v["rounds"].append({"round": "R4-语义复核", "pass": (val == "yes"),
                                    "why": why})
                if val == "no":
                    v["verdict"] = "rejected"
                    v["why"] = "R4 否决：" + why
                elif val == "unsure":
                    if v["verdict"] != "rejected":
                        v["verdict"] = "inconclusive"
                        v["why"] = "R4 说不准：" + why
        else:
            for r in out:
                if r["verify"]["verdict"] != "rejected":
                    r["verify"]["rounds"].append(
                        {"round": "R4-语义复核", "pass": True, "why": "本机没取到可复核的代码片段"})
    else:
        for r in out:
            if r["verify"]["verdict"] != "rejected":
                r["verify"]["rounds"].append(
                    {"round": "R4-语义复核", "pass": True, "why": "本次未启用语义复核"})

    # ---- 定判
    for rec in out:
        v = rec["verify"]
        if v["verdict"] == "rejected":
            pass
        elif v.get("why", "").startswith("R4 说不准"):
            v["verdict"] = "inconclusive"
        else:
            v["verdict"] = "verified"
            v["why"] = "四轮复核全部通过"
        v["seconds"] = round(time.time() - t0, 2)

    stats = {"total": len(out), "verified": 0, "rejected": 0, "inconclusive": 0,
             "seconds": round(time.time() - t0, 2), "llm_used": bool(llm_items)}
    for rec in out:
        k = rec["verify"]["verdict"]
        stats[k] = stats.get(k, 0) + 1
    return out, stats
