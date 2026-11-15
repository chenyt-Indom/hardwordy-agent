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
    """
    low = str(code or "").lower()
    for pat in _DANGER:
        p = pat.lower()
        if ".*" in p:
            # 正则型模式（open(.*'w）
            try:
                if re.search(p, low):
                    return pat
            except re.error:
                continue
        elif p in low:
            return pat
    return ""


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


def write_test(title, code, note=""):
    """把一条断言写成 backend/_feature_tests/<slug>.py。返回 (ok, path_or_why)。

    ⚠️ 已存在同名文件时**覆盖**——同一条功能被反复立项时，断言应该是最新那版。
    """
    ok, why = check_code(code)
    if not ok:
        return False, why
    p = _test_path(title)
    header = ('"""新功能验收断言：%s\n\n%s\n\n由自主立项自动生成，回归会自动加载它。\n'
              '出问题时直接删掉这个文件即可，不影响其他用例。\n"""\n\n'
              % (title, (note or "").strip()))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(header + str(code).strip() + "\n", encoding="utf-8")
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


def unpark_test(title):
    """把挂起的断言恢复启用（动作执行成功后调用）。返回 True 表示已恢复。"""
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


def verify_one(title, timeout=40):
    """在**子进程**里真跑一条断言。返回 {"ok":bool,"why":str,"seconds":float}。

    为什么必须子进程：刚写盘的新代码在当前进程里还是旧模块，进程内跑等于
    在验改动前的代码（这个坑在 regress.run_disk 里已经踩过一次）。
    """
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
