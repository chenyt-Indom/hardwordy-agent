# -*- coding: utf-8 -*-
"""闭环评分与自动回退引擎。

★ 2026-11-22 用户原话：
「我需要你构建一套完整的闭环机制，这套机制能够评判每次迭代是进步了还是
 退步了，进退多少都得呈现，以及每次回滚的原因是什么，这些反思都得参考并以
 之为依据进行构建改进计划并在待批准或者推荐里推送，或者在开启信任模式时在
 任务之后自动进行改进，改进过程可以进行自动重启然后自动验证，确保评分以及
 功能完全正常，如果评分或者功能出现不正常，则可以自动完成回滚并记录回滚
 原因然后重新进行改进或迭代，增加新功能时也是如此」

用户 2026-11-22 选定的口径（问过 7 个问题，这里是逐条落地）：
  1) 进退尺子 = **综合评分 0-100**（静态自检 + 回归通过率 + 功能可用性加权）
  2) 回滚阈值 = **只要比改动前低就回滚**（最严，不留容差）
  3) 自动重启 = 接受，后台自动重启并继续验证
  4) 验证深度 = 语法 + 回归 + 冒烟 + 前后评分对比，**全都要**
  5) 回滚粒度 = 先单条回滚，不行再整轮回滚
  6) 推送 = 待批准 + 推荐卡片两处都推，卡片要写清改进原因与好处
  7) 防死循环 = 不限次数，但每次失败都记录并**降低该问题的尝试优先级**，
     每次重试前必须先读一遍失败原因

评分权重（合计 100）：
    静态分 40 —— 代码自检 high/mid/info 条数（越少越高）
    回归分 30 —— regress.run(quick) 的通过率
    功能分 30 —— 语法 10 + 关键符号 10 + HTTP 冒烟 10

⚠️ 设计原则：**任何一项拿不到数据就返回 None，不编 0 分也不编满分**。
   某项为 None 时按比例重分配权重，而不是当 0 分把整轮判死
   —— 「拿不到基线」和「基线很差」是两回事，混在一起会误杀好改动。
"""

import json
import logging
import os
import sys
import threading
import time
import traceback

from . import safeio

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# ⚠️ 2026-11-22 实测：code_audit 存在「首次调用与后续不同」的缓存效应
#   （同一份未改动的代码，第 1 次报 11 条、第 2/3 次报 10 条）。
#   直接拿来算分会让累计分凭空抖动 —— 这里记一个预热标记，首次评分时空跑一遍。
_AUDIT_WARMED = False

# 权重（quality 质量基线，合计 100 —— 这一段是「有上限」的）
W_STATIC = 40
W_REGRESS = 30
W_FUNC = 30

# ★★ 2026-11-22 用户要求：
#   「我希望评分是没有上限的，超过一百分也继续往上加，就好比手机跑分一样，
#     但需要能够看到扣分点并且详细记录上报并反思等体系化协同运作」
#
# 于是把总分拆成**两段相加**，语义完全分开：
#   · quality 质量基线 0~100（有上限）：静态 40 + 回归 30 + 功能 30 加权。
#     回答「这一版有多健康」。扣分点、压分报告、低分日志全部来自这一段。
#   · ability 能力跑分（**无上限**）：按「被真实验证到的能力」累加 ——
#     每通过一个回归用例、每通过一个冒烟端点、每守住一个关键符号、
#     每编译通过一个后端文件，都往上加。加测试、加端点、加模块就涨，
#     删掉就掉，像手机跑分一样能一直往上叠。
#   · total = quality + ability，**不封顶**（质量段永远 ≤100，超出的部分
#     全部来自能力段，所以「超过 100」的含义很明确：能力比基线多）。
#
# ⚠️ 刻意**不按代码行数**给分（那是鼓励灌水）：文件只按「能编译通过」计
#    极低单价（0.03），主体分来自「被验证到的行为」。
ABILITY_PER_TEST = 1.0        # 每个通过的回归用例
ABILITY_PER_SMOKE = 1.5       # 每个通过的冒烟端点
ABILITY_PER_SYMBOL = 2.0      # 每个守住的关键符号
ABILITY_PER_FILE = 0.0        # 2026-11-23：单价归零。
                              # 理由：这是唯一一条「不需要任何行为验证就能涨分」的口子，
                              # 拆一个文件 +0.03，是成本最低的刷分路径。
                              # 广度由 quality 的静态检查体现，不进能力跑分。

# ★★ 2026-11-23 用户问「评分曲线为什么是平的，需要优化吗」—— 根因：
#   上面四项的输入全是**数量**（46 个用例、4 个端点、1 个符号、105 个文件），
#   代码一改不动，这四项就是常数 → ability 恒定、曲线必然水平。
#   实测最近 60 份：total 155.09→157.15，斜率 +0.013/份。
#
#   所以补两项「只在真实变化时才计分」的**增量项**（不改变上面四项的基线）：
#     · fix_landed     —— 某条回归用例从「失败」翻成「通过」→ 计一次
#     · lesson_fixed   —— 某条教训从「未修」转成「已修复(fixed)」→ 计一次
#   代码没真改动时这两项恒为 0（曲线该平就平）；真修好了才涨，
#   这样「涨」才等于「能力在增长」，而不是「资产存量」。
#
#   ⚠️ 都靠 ability_snapshot.json 去重：同一次修复只计一次分，
#      不会因为反复评分而无限累加。教训**复发**会把这笔分扣回去。
ABILITY_PER_FIX = 3.0         # 一条回归用例由失败翻通过
ABILITY_PER_LESSON = 5.0     # 一条教训由未修转为 fixed（且未复发）

# 冒烟端点：挑**只读、轻量、不触发外部请求**的四个。
# ⚠️ 刻意不碰 /api/selfiter/stream（长连接）、/api/net/check（联网）、
#    /api/selftest（会跑全量测试，几秒到几十秒）。
_SMOKE_PATHS = [
    "/api/health",
    "/api/selfiter/settings",
    "/api/config",
    "/api/skills",
]

# 关键符号：改坏了这些，程序等于起不来
_KEY_SYMBOLS = ["app"]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir():
    """累计分账本目录。

    ⚠️ 2026-11-22 修：原来硬编码 `项目根/data/iterloop`，**绕过了
    config.data_root()** —— 后果是设了 MM_DATA_DIR（Docker 部署、便携包
    换目录）之后账本仍写回源码目录，那里可能只读，累计分就永远存不下来；
    而 failattr / memory 都走 config.data()，两边不一致。
    现在统一走 config.data()，拿不到时再退回 _ROOT。
    """
    d = ""
    try:
        from . import config as _cfg
        d = os.path.join(_cfg.data("data"), "iterloop")
    except Exception:                                   # noqa: BLE001
        logger.debug("iterloop 取 config.data 失败，退回项目根", exc_info=True)
    if not d:
        d = os.path.join(_ROOT, "data", "iterloop")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
    return d


def _hist_path():
    return os.path.join(_data_dir(), "history.jsonl")


def _att_path():
    return os.path.join(_data_dir(), "attempts.json")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 三块评分
def static_part():
    """静态分 40：high 每条扣 8、mid 扣 3、info 扣 0.5。

    返回 None 表示**自检跑不起来**（不是「0 个问题」）—— 两者含义完全不同，
    拿不到数据时宁可让这一项不参与加权。
    """
    try:
        from . import code_audit as CA
        global _AUDIT_WARMED
        a = CA.audit() or {}
        if not _AUDIT_WARMED:
            # 首次调用结果不可靠（见 _AUDIT_WARMED 注释），空跑一遍预热
            _AUDIT_WARMED = True
            a = CA.audit() or {}
        # ★ 2026-11-22：优先用「计分口径」（排除 noscore 已知无害项）。
        #   否则会出现「压分明细自己写着『可忽略』，分数却被扣了 0.5」的矛盾。
        c = a.get("counts_scored") or a.get("counts") or {}
        if not c:
            return None
        high = int(c.get("high") or 0)
        mid = int(c.get("mid") or 0)
        info = int(c.get("info") or 0)
        raw = W_STATIC - (high * 8 + mid * 3 + info * 0.5)
        # ★ 2026-11-22 用户要求「自动检查出增分或减分的原因以及代码分别是什么」：
        #   只留条数不够 —— 必须留下**每一条问题的 文件:行:种类**，
        #   才能和上一轮逐条比对，算出「是哪个文件的哪一行让分数掉了」。
        fl = a.get("findings") or []
        detail = []
        for f in fl:
            if not isinstance(f, dict):
                continue
            d = {
                "file": str(f.get("file") or ""),
                "line": int(f.get("line") or 0),
                "kind": str(f.get("kind") or ""),
                "level": str(f.get("level") or ""),
                "msg": str(f.get("msg") or "")[:200],
            }
            if f.get("noscore"):
                d["noscore"] = True
            detail.append(d)
        return {
            "score": round(max(0.0, min(float(W_STATIC), raw)), 2),
            "max": W_STATIC,
            "high": high, "mid": mid, "info": info,
            "findings": detail[:80],
            "finding_total": len(detail),
            "keys": ["%s:%s:%s" % (d["file"], d["line"], d["kind"])
                     for d in detail],
        }
    except Exception:                                   # noqa: BLE001
        logger.debug("静态分算不出来", exc_info=True)
        return None


def regress_part(timeout=180):
    """回归分 30：通过率 × 30。跑不起来/超时返回 None（不参与加权）。

    ⚠️ 2026-11-22 实测踩到的两个坑，必须用 run_disk（子进程）而不是进程内 run()：
      1) 进程内 run() 里有个测试会 **关掉 sys.stdout** —— 跑完一次之后主进程的
         stdout 就废了（第二次 print 直接 ValueError: I/O operation on closed
         file）。放进服务进程里调用会把日志打废。
      2) 进程内跑的是**内存里的旧代码**：patch 写完文件后模块不会自动重载，
         等于在验改动前的代码，「改坏了自动回滚」形同虚设。
    """
    try:
        from . import regress as RG
        runner = getattr(RG, "run_disk", None) or RG.run
        # ★ 2026-11-22 修自激死循环：回归里有个断言会**真调 selfdev_write 写文件**
        #   （test_selfdev_write_guard 的探针 backend/_guard_probe_target.py），
        #   那会 mark_dirty → dirty_ts 刷新 → 20 秒后自动评分线程又跑回归 → 又打标记…
        #   实测该文件被写 172 次、每分钟 1~3 次空转，持续烧 CPU。
        #   评分期间用环境变量告诉 mark_dirty「别打标记」，finally 里**必须**恢复：
        #   不恢复的话一次评分之后整个进程再也不打标记，闸门就永远检测不到退步了。
        _old_nm = os.environ.get("MM_NO_SCORE_MARK")
        os.environ["MM_NO_SCORE_MARK"] = "1"
        try:
            try:
                r = runner(scope="quick", timeout=timeout)
            except TypeError:
                r = runner(scope="quick")
        finally:
            if _old_nm is None:
                os.environ.pop("MM_NO_SCORE_MARK", None)
            else:
                os.environ["MM_NO_SCORE_MARK"] = _old_nm
        total = int(r.get("total") or 0)
        if total <= 0:
            return None
        passed = int(r.get("passed") or 0)
        failed = int(r.get("failed") or 0)
        rate = float(passed) / float(max(1, passed + failed))
        # ★ 2026-11-24 专家意见（L3 三缺之一：没有行为基线 parity）：
        #   回归只验「写过断言的」行为 —— 没写断言的地方被改坏了，它一声不吭。
        #   这里每次评分都顺带跑一遍 parity（纯函数、不联网，约 0.1~0.3 秒）：
        #   · 一条都没变 → 不影响分数；
        #   · 有漂移     → 回归分打 7 折，并把「哪个函数、哪条输入、改前改后」写进明细，
        #                  强制「改行为要留痕」（确属预期改动就 python -m backend.parity capture）；
        #   · 还没钉基线 → 不扣分，只提示去 capture（不能因为没基线就把分扣光）。
        par = None
        try:
            from . import parity as _PA
            _pres = _PA.compare()
            if not _pres.get("missing_baseline"):
                par = {
                    "ok": bool(_pres.get("ok")),
                    "checked": int(_pres.get("checked") or 0),
                    "targets": int(_pres.get("targets") or 0),
                    "changed": len(_pres.get("changed") or []),
                    "added": len(_pres.get("added") or []),
                    "missing": len(_pres.get("missing") or []),
                    "text": _PA.as_text(_pres),
                }
        except Exception:                                   # noqa: BLE001
            logger.debug("行为基线比对失败（不影响回归分）", exc_info=True)
        _score = rate * W_REGRESS
        if par and not par["ok"]:
            _score *= 0.7
        return {
            "score": round(_score, 2),
            "parity": par,
            "max": W_REGRESS,
            "rate": round(rate, 4),
            "total": total, "passed": passed, "failed": failed,
            "skipped": int(r.get("skipped") or 0),
            "seconds": r.get("seconds"),
            "failed_items": [i.get("name") for i in (r.get("failed_items") or [])][:8],
            # ★ 2026-11-23：fix_landed 要判「某条用例由失败翻成通过」，
            #   截断到 8 个会漏判（失败多于 8 条时后半截永远看不见），
            #   而且必须知道「这条用例现在还在不在」—— 否则删掉一条失败用例
            #   会被误判成「修好了」。所以两个名单都给全量。
            "failed_names": [str(i.get("name") or "") for i in (r.get("failed_items") or [])
                             if i.get("name")],
            "all_names": [str(i.get("name") or "") for i in (r.get("items") or [])
                          if i.get("name")],
        }
    except Exception as e:                              # noqa: BLE001
        logger.debug("回归分算不出来: %s", e, exc_info=True)
        return None


def _syntax_ok():
    """编译 backend 下所有 .py。

    返回 (ok, 坏掉的相对路径列表, 参与编译的文件总数)。
    ⚠️ 2026-11-22 加第三个返回值：能力跑分要按「编译通过的文件数」给极低单价
    的广度分（见 ability_part 的注释），需要知道总数。
    """
    bad = []
    base = os.path.join(_ROOT, "backend")
    try:
        names = sorted(os.listdir(base))
    except Exception:                                   # noqa: BLE001
        return False, ["<backend 目录读不到>"], 0
    total = 0
    for n in names:
        if not n.endswith(".py"):
            continue
        # ★ 2026-11-22 修：临时脚本 / 探针脚本不算「产品代码」，不计入能力分。
        #   能力跑分按「编译通过的文件数 × 0.03」给广度分，而排查时留下的
        #   `_tmp_xxx.py` / `_probe_xxx.py` 也在这个目录里，会让分数虚高；
        #   清理它们时分数反而掉 0.03/个（实测删 3 个掉 0.09，被判退步）。
        #   这类文件本来就不是产品代码，一律排除。
        if n.startswith(("_tmp", "_probe")):
            continue
        total += 1
        p = os.path.join(base, n)
        try:
            with open(p, encoding="utf-8") as fh:
                compile(fh.read(), p, "exec")
        except SyntaxError as e:
            bad.append("%s:%s" % (n, e.lineno))
        except Exception:                               # noqa: BLE001
            bad.append(n)
    return (not bad), bad, total


_PROBE_CODE = r'''
import sys, json
sys.path.insert(0, r"%(root)s")
out = {"symbols_ok": False, "symbols_bad": [], "smoke": []}
try:
    from backend import main as M
    out["symbols_ok"] = all(hasattr(M, s) for s in %(syms)r)
    out["symbols_bad"] = [s for s in %(syms)r if not hasattr(M, s)]
    from fastapi.testclient import TestClient
    cli = TestClient(M.app)
    for p in %(paths)r:
        try:
            r = cli.get(p)
            code = int(getattr(r, "status_code", 0) or 0)
            out["smoke"].append({"path": p, "code": code, "ok": 200 <= code < 300})
        except Exception as e:
            out["smoke"].append({"path": p, "code": 0, "ok": False,
                                 "error": "%%s: %%s" %% (type(e).__name__, e)})
except Exception as e:
    out["import_error"] = "%%s: %%s" %% (type(e).__name__, e)
print("@@PROBE@@" + json.dumps(out, ensure_ascii=False))
'''


def _subproc_probe(timeout=150):
    """在**子进程**里从磁盘重新加载 backend.main，再打冒烟。

    ⚠️ 2026-11-22 修关键缺陷：原来在主进程里 `from . import main` 拿的是
    **已经加载在内存里的旧模块** —— 刚改完代码去验证，测的却是改动前的代码，
    等于「验证」这一环完全失效。必须起子进程让 Python 从磁盘重新导入。

    返回 (symbols_ok, symbols_bad, smoke_ok, smoke_detail)。
    """
    import subprocess
    code = _PROBE_CODE % {
        "root": _ROOT.replace("\\", "\\\\"),
        "syms": _KEY_SYMBOLS,
        "paths": _SMOKE_PATHS,
    }
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout, cwd=_ROOT)
    except Exception as e:                              # noqa: BLE001
        msg = "%s: %s" % (type(e).__name__, e)
        return False, [msg], False, [{"path": "<子进程>", "ok": False, "error": msg}]
    line = ""
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("@@PROBE@@"):
            line = ln[len("@@PROBE@@"):]
    if not line:
        err = (p.stderr or "")[-400:] or "子进程没有返回探针结果"
        return False, [err], False, [{"path": "<子进程>", "ok": False, "error": err}]
    try:
        d = json.loads(line)
    except Exception as e:                              # noqa: BLE001
        return False, ["探针结果解析失败: %s" % e], False, []
    sym_ok = bool(d.get("symbols_ok"))
    sym_bad = list(d.get("symbols_bad") or [])
    if d.get("import_error"):
        sym_bad = sym_bad + [str(d["import_error"])[:300]]
    smoke = list(d.get("smoke") or [])
    sm_ok = bool(smoke) and all(x.get("ok") for x in smoke)
    return sym_ok, sym_bad, sm_ok, smoke


def func_part():
    """功能分 30 = 语法 10 + 关键符号 10 + 冒烟 10。"""
    syn_ok, syn_bad, py_files = _syntax_ok()
    sym_ok, sym_bad, sm_ok, sm_detail = _subproc_probe()
    score = 0.0
    if syn_ok:
        score += 10
    if sym_ok:
        score += 10
    if sm_ok:
        score += 10
    elif sm_detail:
        # 冒烟部分失败 → 按成功比例给分（不搞全有全无）
        good = sum(1 for d in sm_detail if d.get("ok"))
        score += 10.0 * good / float(max(1, len(sm_detail)))
    return {
        "score": round(score, 2),
        "max": W_FUNC,
        "syntax_ok": syn_ok, "syntax_bad": syn_bad[:6],
        "symbols_ok": sym_ok, "symbols_bad": sym_bad[:6],
        "smoke_ok": sm_ok, "smoke": sm_detail,
        "py_files": int(py_files or 0),
    }


def _ability_snap_path():
    return os.path.join(_data_dir(), "ability_snapshot.json")


_SNAP_WARN_SESSION = "rsi_snapshot"     # 落盘到 data/actlog/rsi_snapshot.jsonl
_SNAP_WARN_GAP = 3600.0                 # 同一类问题 1 小时最多进账本 1 条
_SNAP_WARN_SEEN = {"ts": 0.0}


def _actlog_module():
    """拿到 actlog 模块；拿不到返回 None（iterloop 要能独立导入）。"""
    try:
        from . import actlog as _al
        return _al
    except Exception:                                   # noqa: BLE001
        logger.debug("iterloop 取 actlog 失败（快照告警不落账本）", exc_info=True)
    for _name in ("actlog", "backend.actlog"):
        try:
            return __import__(_name, fromlist=["*"])
        except Exception:                               # noqa: BLE001
            continue
    return None


def _note_snap_problem(msg, key="ability_snapshot"):
    """ability_snapshot 读/写真出问题时**留痕**：warning + actlog 事件。

    为什么需要它（2026-11-23 修的 silent-except 扣分点）：这两处原来是
    `except ...: pass`，异常被完全吞掉。后果不只是自检扣一分 ——
    「快照读不到 → 增量分每轮都从零算」这种故障原本在日志和账本里
    **一点现场都没有**，只能看到分数莫名不动。

    口径：
      * FileNotFoundError（首次运行没有快照）= 正常控制流，只留 debug，不当故障；
      * 其它异常（JSON 截断 / 权限 / 磁盘满）= 真故障，warning + 账本事件，
        且同一类问题 1 小时内只进账本 1 条，避免评分每轮刷爆账本。
    任何异常都吞掉（记日志这件事绝不能把评分弄挂），但吞之前先写 debug。
    """
    text = str(msg or "")[:200]
    try:
        logger.warning("ability_snapshot[%s]: %s", key, text)
    except Exception:                                   # noqa: BLE001
        return
    now = time.time()
    try:
        if now - float(_SNAP_WARN_SEEN.get("ts") or 0.0) < _SNAP_WARN_GAP:
            return                       # 节流：账本轮不上限，宁可少记也别刷爆
        _SNAP_WARN_SEEN["ts"] = now
    except Exception:                                   # noqa: BLE001
        logger.debug("ability_snapshot 告警节流失效（照常记录）", exc_info=True)
    al = _actlog_module()
    if al is None:
        return
    try:
        al.append_watch_event(_SNAP_WARN_SESSION,
                              {"kind": "rsi_snapshot", "key": str(key)[:80],
                               "state": "warn", "note": text})
    except Exception:                                   # noqa: BLE001
        logger.debug("ability_snapshot 告警写账本失败（不影响评分）", exc_info=True)


def _load_ability_snap():
    """读增量项的去重快照。

    结构：
      failed   —— 上次评分时还在失败的用例名
      fixed    —— 已计过「翻通过」分的用例名
      lessons  —— 已计过「转 fixed」分的教训 kind
    """
    try:
        with open(_ability_snap_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return {"failed": [str(x) for x in (d.get("failed") or [])],
                    "fixed": [str(x) for x in (d.get("fixed") or [])],
                    "lessons": [str(x) for x in (d.get("lessons") or [])]}
    except FileNotFoundError:                           # noqa: BLE001
        # 首次运行还没有快照文件 —— 这是正常控制流，不是被吞掉的异常。
        # 显式标 noqa 说明「有意忽略」，绝不写成 `pass`（自检会记成 silent-except）；
        # 也只留 debug 不打 warning：每装一次就 warn 一次纯属噪音。
        logger.debug("读 ability_snapshot：还没有快照文件，按空快照起步（%s）",
                     _ability_snap_path())
        return {"failed": [], "fixed": [], "lessons": []}
    except Exception as e:                              # noqa: BLE001
        # ★ 真读坏了（JSON 截断 / 权限 / 磁盘）不能只留一行 debug：
        #   进 warning + actlog 事件，才有现场可查（silent-except 的另一半修法）。
        _note_snap_problem("读 ability_snapshot 失败，按空快照处理: %s: %s"
                           % (type(e).__name__, e))
    return {"failed": [], "fixed": [], "lessons": []}


def _save_ability_snap(snap):
    try:
        with open(_ability_snap_path(), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
        return True
    except Exception as e:                              # noqa: BLE001
        # 写不进去的后果比读失败更隐蔽：去重快照丢了 → 同一批修复会被反复计分。
        # 所以这里也留痕（warning + 账本），而不只是写一行 debug。
        _note_snap_problem("写 ability_snapshot 失败: %s: %s" % (type(e).__name__, e))
        logger.debug("写 ability_snapshot 失败", exc_info=True)
        return False


def _fix_landed_rows(rg):
    """增量项①：回归用例「失败 → 通过」才算的能力增长。

    为什么需要它：原有 regress_pass 是「通过数 × 单价」，用例总数不变时
    它就是常数，代码改得再好也不动（实测 46 条一直是 46，曲线因此水平）。

    只认**翻转**：上次失败、这次通过、且这条用例现在还在
    （**删掉用例不算修好**）→ 计 ABILITY_PER_FIX，并记进快照防重复计分。
    反过来，已计过分又失败（复发）→ 倒扣，允许修好后重新计。
    """
    if not isinstance(rg, dict):
        return None
    all_now = [str(n) for n in (rg.get("all_names") or []) if n]
    if not all_now:
        return None                    # 拿不到逐条结果 → 不判，别瞎记
    cur_fail = [str(n) for n in (rg.get("failed_names") or []) if n]
    cur_fail_set, all_set = set(cur_fail), set(all_now)
    snap = _load_ability_snap()
    prev_fail = list(snap.get("failed") or [])
    done = list(snap.get("fixed") or [])
    newly = [n for n in prev_fail
             if n not in cur_fail_set and n in all_set and n not in done]
    relapse = [n for n in cur_fail if n in done]
    done = [n for n in done if n not in set(relapse)]
    done.extend(newly)
    snap["failed"], snap["fixed"] = cur_fail, done
    _save_ability_snap(snap)
    # ★ 2026-11-23 实测修正：**按累计已修复条数**计分，不是按「本次新增」。
    #   原来写的是 (新增 - 复发) × 单价 → 复跑验证时暴露真问题：
    #   修好那一次 +3、下一份评分立刻掉回原值，曲线是**尖峰**不是台阶，
    #   等于「修好的能力下一分钟就不算数了」，和「能力只会累加」的语义相反。
    #   改成累计后：修好就一直算，复发时 done 被剔除 → 自动倒扣，且天然不重复计。
    return {"key": "fix_landed", "label": "失败用例翻通过",
            "count": len(done), "unit": ABILITY_PER_FIX,
            "score": round(len(done) * ABILITY_PER_FIX, 2),
            "detail": {"newly_fixed": newly[:8], "relapse": relapse[:8],
                       "fixed_total": len(done)},
            "note": "按累计已修复条数计分（修好就保持，复发自动倒扣）；删用例不算修好"}


def _fix_regressed_rows(rg):
    """增量项③（**只报告、不扣分**）：本轮有多少条「曾经修好」的用例又红了。

    ⚠️⚠️ 2026-11-23 实测结论（探针 backend/_tmp_probe_fix.py 跑过）：
        **倒扣已经由 _fix_landed_rows 承担了**。实测时序：
            t1 失败 → fix_landed 0.0
            t1 翻绿 → fix_landed 3.0（newly_fixed=[t1]）
            t1 复发 → fix_landed 0.0（relapse=[t1]，done 被剔除）
        施工单 T1-2 写「fix_landed 目前只发不退」，那是基于「按本次新增计分」
        那版实现的判断；现行代码（第 515-519 行注释）已经改成**按累计计分**，
        复发会自动从 done 里剔除、分数自动退回。

    所以这里**故意不扣分**（unit/score 恒为 0）：再扣一次就是双扣，
    一条用例复发会扣 6 分，把曲线压得比实际能力更难看，反而误导判据。

    保留它的用途：① 满足施工单验收（hasattr 为 True 且已接入 ability_part）；
    ② 让「本轮有没有复发」在 rows 里直接可见，不用去翻 snapshot 文件。

    ⚠️ 必须排在 _fix_landed_rows **之前**执行：后者会把复发的用例从
       snapshot 的 fixed 里剔掉，之后再读就读不到「曾经修好」的痕迹了。
    """
    if not isinstance(rg, dict):
        return None
    cur_fail = [str(n) for n in (rg.get("failed_names") or []) if n]
    if not cur_fail:
        return None
    snap = _load_ability_snap()
    credited = set(str(x) for x in (snap.get("fixed") or []))
    if not credited:
        return None
    lost = sorted(credited & set(cur_fail))
    if not lost:
        return None
    return {"key": "fix_regressed",
            "label": "修复回退（仅报告，扣分已由 fix_landed 承担）",
            "count": len(lost), "unit": 0.0, "score": 0.0,
            "detail": {"lost": lost[:8]},
            "note": "倒扣由 fix_landed 的累计机制执行；此处只报告，避免一条用例扣两次"}


def _lesson_fixed_rows():
    """增量项②：教训从「未修」转成「已修复(fixed)」才算的能力增长。

    复用 lessonloop.audit() 现成的 fixed 判定（探针命中 + 观察窗内未复发），
    不另造状态。同一条教训只计一次分；**复发退回时倒扣**，
    防止「反复自封修好」刷分。
    """
    try:
        from . import lessonloop as LL
        a = LL.audit() or {}
    except Exception:                                   # noqa: BLE001
        logger.debug("读教训清单失败，本次不判 lesson_fixed", exc_info=True)
        return None
    if not a.get("ok") or not int(a.get("total") or 0):
        return None                    # 没有教训 / 拿不到清单 → 不判，别乱扣
    fixed_now = [str(r.get("kind") or "") for r in (a.get("fixed") or [])
                 if r.get("kind")]
    fixed_set = set(fixed_now)
    snap = _load_ability_snap()
    done = list(snap.get("lessons") or [])
    newly = [k for k in fixed_now if k not in done]
    relapse = [k for k in done if k not in fixed_set]
    done = [k for k in done if k in fixed_set]
    done.extend(newly)
    snap["lessons"] = done
    _save_ability_snap(snap)
    # ★ 与 fix_landed 同一处修正（2026-11-23）：按**累计**已转 fixed 的教训计分。
    #   按「本次新增」算的话，教训转 fixed 那一次 +5、下一份评分又归零 —— 曲线抖动。
    return {"key": "lesson_fixed", "label": "教训转为已修复",
            "count": len(done), "unit": ABILITY_PER_LESSON,
            "score": round(len(done) * ABILITY_PER_LESSON, 2),
            "detail": {"newly_fixed": newly[:8], "relapse": relapse[:8],
                       "fixed_total": len(done)},
            "note": "按累计已修复教训计分（转 fixed 就保持，复发自动倒扣）"}


def ability_part(rg=None, fn=None):
    """能力跑分（**无上限**）—— 只累加「真实被验证到」的能力。

    每一条都带 count / unit / score，所以「为什么涨了、涨在哪一项」
    可以直接读出来，和压分报告一样能详细记录与上报。
    """
    rg = rg if isinstance(rg, dict) else {}
    fn = fn if isinstance(fn, dict) else {}
    rows = []
    if rg:
        n = int(rg.get("passed") or 0)
        rows.append({"key": "regress_pass", "label": "回归用例通过",
                     "count": n, "unit": ABILITY_PER_TEST,
                     "score": round(n * ABILITY_PER_TEST, 2)})
    smoke = [s for s in (fn.get("smoke") or []) if isinstance(s, dict)]
    if smoke:
        n = sum(1 for s in smoke if s.get("ok"))
        rows.append({"key": "smoke_pass", "label": "冒烟端点通过",
                     "count": n, "unit": ABILITY_PER_SMOKE,
                     "score": round(n * ABILITY_PER_SMOKE, 2)})
    if fn:
        n = len(_KEY_SYMBOLS) if fn.get("symbols_ok") else 0
        rows.append({"key": "symbol_ok", "label": "关键符号守住",
                     "count": n, "unit": ABILITY_PER_SYMBOL,
                     "score": round(n * ABILITY_PER_SYMBOL, 2)})
        n = int(fn.get("py_files") or 0)
        if n:
            rows.append({"key": "file_compile", "label": "后端文件编译通过",
                         "count": n, "unit": ABILITY_PER_FILE,
                         "score": round(n * ABILITY_PER_FILE, 2)})
    # ★ 2026-11-23：上面四项都是「存量」（数量×单价，代码不动就不动），
    #   曲线因此必然水平。这里补两项**只在真实变化时计分**的增量。
    #   加锁：后台评分线程与手动评分可能同时进来，两个增量都要写同一个快照。
    with _LOCK:
        # ⚠️ 顺序有意义：_fix_regressed_rows 必须在 _fix_landed_rows 之前跑，
        #    否则后者已经把复发的用例从 snapshot 里剔掉，读不到「曾经修好」的痕迹。
        #    （它只报告不扣分，见函数注释：扣分已由 fix_landed 累计机制承担。）
        for _row in (_fix_regressed_rows(rg), _fix_landed_rows(rg), _lesson_fixed_rows()):
            if _row:
                rows.append(_row)
    return {
        "score": round(sum(float(r["score"]) for r in rows), 2),
        "max": None, "unbounded": True, "rows": rows,
        "note": ("无上限：存量项随被验证到的能力累加（删测试/端点会掉分）；"
                 "增量项 fix_landed / lesson_fixed 只在真实修好时计分，复发倒扣"),
    }


def write_last_score(s):
    """把「当前代码的绝对分数」落盘，供面板显示「当前版本」。

    ★ 2026-11-22 用户要求「以后每次结束之后都得在第一时间更新评分」。
      之前这段逻辑写死在 main.py 的 /api/selfiter/loop/score 里，只有手动
      点「量一下」才更新；后台自动评分（scoregate）没法复用，面板就一直
      显示上一次手动量的旧分。抽成函数后两处共用同一份格式。

    ⚠️ 这里**只写当前绝对值**，不碰累计分账本 —— 账本仍由重启闸门提交，
       避免同一批改动被记两轮进退。
    """
    if not isinstance(s, dict):
        return ""
    try:
        d = _data_dir()
        os.makedirs(d, exist_ok=True)
        _st = s.get("static") or {}
        _rg = s.get("regress") or {}
        _fn = s.get("func") or {}
        _ab = s.get("ability") or {}
        p = os.path.join(d, "last_score.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"total": s.get("total"),
                       "quality": s.get("quality"),
                       "ability": _ab.get("score"),
                       "ability_rows": _ab.get("rows"),
                       "unbounded": True,
                       "iso": s.get("iso"),
                       "static": _st.get("score"), "static_max": _st.get("max"),
                       "high": _st.get("high"), "mid": _st.get("mid"),
                       "regress": _rg.get("score"), "regress_max": _rg.get("max"),
                       "func": _fn.get("score"), "func_max": _fn.get("max"),
                       "seconds": s.get("seconds")},
                      fh, ensure_ascii=False, indent=1)
        return p
    except Exception:                                       # noqa: BLE001
        logger.debug("写 last_score.json 失败", exc_info=True)
        return ""


def score(full=True):
    """综合评分 = quality 质量基线(0~100) + ability 能力跑分(无上限)。

    full=False 时跳过回归（回归要几秒，用于快速探针）；
    任一分项为 None 时按比例重分配权重，不当 0 分处理。
    """
    t0 = time.time()
    st = static_part()
    rg = regress_part() if full else None
    fn = func_part()
    parts = [(st, W_STATIC), (rg, W_REGRESS), (fn, W_FUNC)]
    got = [(v, w) for v, w in parts if v is not None]
    if not got:
        return {"total": None, "ts": time.time(), "iso": _now(),
                "static": st, "regress": rg, "func": fn,
                "note": "三项全部拿不到，无法评分"}
    wsum = sum(w for _, w in got)
    # ⚠️ 分项 score 已经是「该分项内的绝对分」（如 35.5/40），
    #    这里必须先归一化到 0~1 再加权 —— 直接乘权重会把分数放大成上千
    #    （实测踩过：静态 35.5 配权重 40 得出 total=3314）。
    acc = sum((float(v["score"]) / float(v.get("max") or w)) * w
              for v, w in got) / float(wsum) * 100.0
    quality = round(acc, 2)
    # ★ 能力跑分与质量基线**相加**，total 不封顶（见文件头常量处的口径说明）
    ab = ability_part(rg, fn)
    total = round(quality + float(ab.get("score") or 0.0), 2)
    return {
        "total": total,
        "quality": quality,
        "ability": ab,
        "unbounded": True,
        "ts": time.time(),
        "iso": _now(),
        "static": st, "regress": rg, "func": fn,
        "weights_used": {"static": W_STATIC if st else 0,
                         "regress": W_REGRESS if rg else 0,
                         "func": W_FUNC if fn else 0},
        "seconds": round(time.time() - t0, 2),
        "full": bool(full),
    }


# ---------------------------------------------------------------- 进退判定
def judge(before, after):
    """比较改动前后两次评分，给出「进步/退步/持平」和进退多少。

    用户 2026-11-22 选的是**最严口径**：只要比改动前低就判退步。
    """
    b = (before or {}).get("total")
    a = (after or {}).get("total")
    if b is None or a is None:
        return {"verdict": "unknown", "delta": None, "pct": None,
                "before": b, "after": a,
                "note": "有一次评分拿不到，无法判断进退（不回滚，仅记录）"}
    delta = round(float(a) - float(b), 2)
    pct = round(delta / float(b) * 100.0, 2) if b else None
    verdict = "improved" if delta > 0 else ("regressed" if delta < 0 else "flat")
    # 分项进退：让用户看到是哪一块掉了
    per = {}
    for k in ("static", "regress", "func"):
        vb = ((before or {}).get(k) or {}).get("score")
        va = ((after or {}).get(k) or {}).get("score")
        if vb is None or va is None:
            per[k] = None
        else:
            per[k] = round(float(va) - float(vb), 2)
    return {"verdict": verdict, "delta": delta, "pct": pct,
            "before": b, "after": a, "per_part": per,
            "should_rollback": verdict == "regressed"}


# ------------------------------------------------- 累计分（从 0 起，无上限）
# ★ 2026-11-22 用户原话：
#   「评分机制应该从0开始叠加，而不是有一个分数上限，这样才可以更直观的看出
#    跑分增加了多少或减少了多少，并且需要能够自动检查出增分或减分的原因以及
#    代码分别是什么，然后需要详细记录在日志上，反思也需要重点参考这个方面」
#
# 口径：
#   · score() 那套 0-100 **只当测量尺**（量这一轮的绝对水平，便于横向比较）；
#   · 用户看的是**累计分**：每轮把 delta 累加上去，从 0 起，不封顶，
#     允许为负（一直退步就一直往下掉），这样「进退多少」是直接可读的数字。
#   · 每轮同时产出 explain：哪个文件哪一行、什么问题，导致加分还是减分。

def _ledger_path():
    return os.path.join(_data_dir(), "ledger.json")


def _ledger_jsonl_path():
    return os.path.join(_data_dir(), "ledger.jsonl")


def _score_log_path():
    return os.path.join(_data_dir(), "score.log")


def load_ledger():
    """读累计分账本。读不到返回全 0 的初始态（不抛）。"""
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            d.setdefault("cum", 0.0)
            d.setdefault("rounds", 0)
            d.setdefault("events", [])
            d.setdefault("started", _now())
            return d
    except Exception:                                   # noqa: BLE001
        logger.debug("读累计账本失败", exc_info=True)
    return {"cum": 0.0, "rounds": 0, "started": _now(),
            "base": None, "last": None, "events": []}


def _save_ledger(d):
    try:
        with _LOCK:
            safeio.atomic_write_json(_ledger_path(), d, indent=1)
    except Exception:                                   # noqa: BLE001
        logger.debug("写累计账本失败", exc_info=True)


def _append_log(lines):
    """把人类可读的进退明细追加到 score.log（只追加，不覆盖）。"""
    try:
        with _LOCK:
            with open(_score_log_path(), "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("写评分日志失败", exc_info=True)


def _static_findings(part):
    """从 static 分项里取 findings 明细（兼容老记录，没有就返回空）。"""
    fl = (part or {}).get("findings")
    if not isinstance(fl, list):
        return []
    return [f for f in fl if isinstance(f, dict)]


def _fkey(f):
    return "%s:%s:%s" % (f.get("file") or "", f.get("line") or 0,
                         f.get("kind") or "")


def explain_delta(before, after, actions=None):
    """算出「为什么加分 / 为什么减分」，并定位到具体文件与行。

    用户 2026-11-22：「需要能够自动检查出增分或减分的原因以及代码分别是什么」。

    做法是**逐条比对**前后两次的静态问题清单：
      · 改动后新出现的问题 → 扣分原因（带 file:line:kind）
      · 改动后消失的问题   → 加分原因
    再把「新问题所在文件」与「本轮实际改过的文件」求交集，交集就是
    最可疑的代码位置（suspects）—— 这才是「代码分别是什么」的答案。
    """
    actions = actions or []
    b_fl = _static_findings((before or {}).get("static"))
    a_fl = _static_findings((after or {}).get("static"))
    b_map = {_fkey(f): f for f in b_fl}
    a_map = {_fkey(f): f for f in a_fl}
    added = [a_map[k] for k in a_map if k not in b_map]
    removed = [b_map[k] for k in b_map if k not in a_map]

    # 本轮实际改过哪些文件（execute_approved 的结果里有 files）
    changed = []
    for r in actions:
        for f in (r.get("files") or []):
            f = str(f or "").replace("\\", "/").lstrip("./")
            if f and f not in changed:
                changed.append(f)

    def _hit(f):
        """这条问题是不是落在本轮改过的文件里。"""
        fp = str(f.get("file") or "").replace("\\", "/").lstrip("./")
        if not fp:
            return False
        base = fp.split("/")[-1]
        for c in changed:
            cb = c.split("/")[-1]
            if fp == c or fp.endswith("/" + c) or c.endswith("/" + fp) or base == cb:
                return True
        return False

    def _code(f, effect):
        return {"file": str(f.get("file") or ""), "line": int(f.get("line") or 0),
                "kind": str(f.get("kind") or ""), "level": str(f.get("level") or ""),
                "msg": str(f.get("msg") or "")[:160],
                "effect": effect, "in_changed": _hit(f)}

    new_codes = [_code(f, "扣分") for f in added]
    fixed_codes = [_code(f, "加分") for f in removed]

    # 按文件归并扣分点
    by_file = {}
    for c in new_codes:
        by_file.setdefault(c["file"] or "<未知文件>", []).append(c)
    suspects = []
    for k, v in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
        suspects.append({
            "file": k, "count": len(v),
            "lines": [x["line"] for x in v][:12],
            "kinds": sorted(set(x["kind"] for x in v)),
            "levels": sorted(set(x["level"] for x in v)),
            "in_changed": any(x["in_changed"] for x in v),
        })
    suspects = suspects[:8]

    # 回归 / 功能两块
    rg_b = (before or {}).get("regress") or {}
    rg_a = (after or {}).get("regress") or {}
    rg_added = [x for x in (rg_a.get("failed_items") or [])
                if x not in (rg_b.get("failed_items") or [])]
    rg_fixed = [x for x in (rg_b.get("failed_items") or [])
                if x not in (rg_a.get("failed_items") or [])]
    fn_b = (before or {}).get("func") or {}
    fn_a = (after or {}).get("func") or {}
    fn_added = [x for x in (fn_a.get("syntax_bad") or [])
                if x not in (fn_b.get("syntax_bad") or [])]
    fn_fixed = [x for x in (fn_b.get("syntax_bad") or [])
                if x not in (fn_a.get("syntax_bad") or [])]
    smoke_bad = [s.get("path") for s in (fn_a.get("smoke") or [])
                 if not s.get("ok")]

    per = {}
    for k in ("static", "regress", "func"):
        vb = ((before or {}).get(k) or {}).get("score")
        va = ((after or {}).get(k) or {}).get("score")
        per[k] = None if (vb is None or va is None) else round(float(va) - float(vb), 2)

    # ★ 2026-11-24 修：total = quality(static/regress/func) + ability(无上限)。
    #   原来 per 只比 quality 三项，能力分涨了时三项全是 0，summary 就写成
    #   「没有可归因的变化」—— 账本 delta≠0 却查不到原因。实测账本 17:30 +8.00、
    #   17:42 +4.00、18:11 +3.00 连续三条都是这个形态（涨的全是 ability 里的
    #   fix_landed / lesson_fixed 计数）。这里把 ability 逐项 diff 补进来，
    #   归因才闭合：涨在哪一项、从几条到几条，一眼能读出来。
    ab_b = ((before or {}).get("ability") or {})
    ab_a = ((after or {}).get("ability") or {})
    ab_map = {}
    for _side, _src in (("b", ab_b), ("a", ab_a)):
        for _r in (_src.get("rows") or []):
            if isinstance(_r, dict) and _r.get("key"):
                _cell = ab_map.setdefault(str(_r["key"]),
                                          {"label": str(_r.get("label") or _r["key"]),
                                           "unit": _r.get("unit")})
                _cell[_side] = _r
    ab_diff = []
    for _k, _v in ab_map.items():
        _rb, _ra = (_v.get("b") or {}), (_v.get("a") or {})
        _sb, _sa = float(_rb.get("score") or 0.0), float(_ra.get("score") or 0.0)
        _d = round(_sa - _sb, 2)
        if _d:
            ab_diff.append({"key": _k, "label": _v.get("label"), "delta": _d,
                            "count_before": _rb.get("count"),
                            "count_after": _ra.get("count"),
                            "score_before": _sb, "score_after": _sa,
                            "unit": _v.get("unit")})
    ab_diff.sort(key=lambda x: -abs(float(x["delta"])))

    bits = []
    for k, label in (("static", "静态"), ("regress", "回归"), ("func", "功能")):
        d = per.get(k)
        if d:
            bits.append("%s%+.2f" % (label, float(d)))
    for x in ab_diff[:4]:
        bits.append("能力·%s%+.2f（%s→%s）"
                    % (x["label"], x["delta"],
                       x["count_before"], x["count_after"]))
    # 兜底：ability 合计变了但逐项没差（老记录缺 rows 时）—— 也不能说「没变化」
    if not ab_diff:
        _ab_d = round(float(ab_a.get("score") or 0.0) - float(ab_b.get("score") or 0.0), 2)
        if _ab_d:
            bits.append("能力合计%+.2f" % _ab_d)
    if new_codes:
        top = suspects[0] if suspects else None
        if top:
            bits.append("新增 %d 处问题，主要在 %s 行 %s"
                        % (len(new_codes), top["file"],
                           ",".join(str(x) for x in top["lines"][:5])))
        else:
            bits.append("新增 %d 处问题" % len(new_codes))
    if fixed_codes:
        bits.append("修掉 %d 处问题" % len(fixed_codes))
    if rg_added:
        bits.append("回归新失败：" + "、".join(str(x)[:30] for x in rg_added[:4]))
    if fn_added:
        bits.append("语法新坏：" + "、".join(str(x)[:30] for x in fn_added[:4]))
    if smoke_bad:
        bits.append("接口不通：" + "、".join(str(x)[:30] for x in smoke_bad[:4]))
    summary = "；".join(bits) if bits else "没有可归因的变化"

    return {"per_part": per, "ability_diff": ab_diff[:8],
            "new_codes": new_codes[:40],
            "fixed_codes": fixed_codes[:40], "suspects": suspects,
            "changed_files": changed[:20], "regress_added": rg_added[:8],
            "regress_fixed": rg_fixed[:8], "func_added": fn_added[:8],
            "func_fixed": fn_fixed[:8], "smoke_bad": smoke_bad[:8],
            "summary": summary[:1200]}


# ══ 施工单第 8 章 · 双轨：账本（cum）与尺子（hwa_bench）并存，永不相加 ══
# 账本回答「我干了多少活」，尺子回答「我变成了什么」。混成一个数，两个都答不准。
BENCH_SCORES = os.environ.get("HWA_BENCH_SCORES", r"D:\hwa_bench\scores.jsonl")


def read_bench_last():
    """只读基准最后一行。读不到就返回 None —— 不补 0，不估算，不猜。

    ⚠️ 施工单 8.4 的硬要求：补 0 会让曲线掉到谷底（关机一天就以为退化了），
    估算等于编数据，比不记更坏。界面显示成「—」才是诚实的。
    """
    try:
        with open(BENCH_SCORES, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if not lines:
            return None
        # HB-SPEC-002 第 2 章：void 不进分母，也不该被当成「最近一次成绩」。
        # 取最后一条非 void 的记录；全是 void 就返回 None（界面显示 —）。
        rec = None
        for ln in reversed(lines):
            try:
                cand = json.loads(ln)
            except Exception:                            # noqa: BLE001
                continue
            if isinstance(cand, dict) and cand.get("verdict") != "void":
                rec = cand
                break
        if rec is None:
            return None
        # bench.py 写的是 iso 字符串，没有 ts —— 两种都认，别算出离谱的年龄
        ts = rec.get("ts")
        if ts is None:
            try:
                ts = time.mktime(time.strptime(str(rec.get("iso") or ""),
                                               "%Y-%m-%dT%H:%M:%S"))
            except Exception:                            # noqa: BLE001
                ts = None
        rec["_age_h"] = (round((time.time() - float(ts)) / 3600, 1)
                         if ts else None)
        return rec
    except Exception:                                    # noqa: BLE001
        return None


def read_bench_series(limit=120):
    """尺子**序列**（只读，给双轴曲线用）。读不到就返回 []，绝不补点。

    ⚠️ 施工单 8.5「拿不到尺子 → 显示 —，不画点」：断线比假点诚实。
    """
    out = []
    try:
        with open(BENCH_SCORES, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                        # noqa: BLE001
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("verdict") == "void":
                    continue          # void 不进分母，也不画点
                out.append({"iso": str(r.get("iso") or ""),
                            "pass": r.get("pass"), "total": r.get("total"),
                            "pct": r.get("pct")})
    except Exception:                                    # noqa: BLE001
        return []
    return out[-int(limit or 120):]


def _bench_rows():
    """读尺子的**全部**记录（只读；坏行跳过，不猜、不补）。"""
    out = []
    try:
        with open(BENCH_SCORES, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                        # noqa: BLE001
                    continue
                if isinstance(r, dict) and r.get("pct") is not None \
                        and r.get("verdict") != "void":
                    out.append(r)
    except Exception:                                    # noqa: BLE001
        return []
    return out


def bench_verdict(window=6):
    """**以尺子为准**的进退判定（施工单第 8 章的落地版）。

    为什么必须有它：账本记的是「我干了多少活」，尺子量的是「我变成了什么」。
    2026-11-24 实测出现过典型形态 —— 账本一轮 +4.00，尺子却从 14/15 掉到
    12/15。只看账本会得出「在进步」的结论，那等于自己给自己打分。

    所以这里把「尺子有没有涨」算成**独立结论**，并显式标出「账本涨、尺子
    没动」这种刷分形态（grind），供反思 / 面板 / 卡片直接引用。

    返回字段：
      ok        读到尺子了吗
      last/prev 最近两次的 {iso, pass, total, pct}
      delta     最近一次相对上一次的百分点变化（没有上一次 = None）
      trend     up / down / flat / unknown（±5 个百分点以内算 flat，抗抖动）
      failing   最近一次**真实失分**的题（id + 原因）—— 这才是下一步该修的
      infra     其中属于「请求层失败」（断网 / 超时）的题，不算能力问题
      grind     账本在最近 window 轮累加为正、而尺子没涨 → 疑似刷分
      ledger_sum 同一窗口内账本 delta 的累加（只用于对照，不与尺子相加）
    """
    rows = _bench_rows()
    out = {"ok": False, "why": "", "last": None, "prev": None, "delta": None,
           "trend": "unknown", "failing": [], "infra": [], "grind": False,
           "ledger_sum": 0.0}
    if not rows:
        out["why"] = "读不到尺子（%s）" % BENCH_SCORES
        return out
    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    out["ok"] = True
    out["last"] = {"iso": str(last.get("iso") or ""), "pass": last.get("pass"),
                   "total": last.get("total"), "pct": last.get("pct")}
    if prev:
        out["prev"] = {"iso": str(prev.get("iso") or ""), "pass": prev.get("pass"),
                       "total": prev.get("total"), "pct": prev.get("pct")}
        try:
            d = round(float(last.get("pct")) - float(prev.get("pct")), 1)
            out["delta"] = d
            out["trend"] = "up" if d >= 5 else ("down" if d <= -5 else "flat")
        except Exception:                                # noqa: BLE001
            pass

    detail = last.get("detail") or {}
    # ★ 2026-11-24：把原始明细带出去 —— 留出集要按题号分组算分，
    #   只留 failing 列表是不够的（通过的题也得算进分母）。
    out["detail"] = detail if isinstance(detail, dict) else {}
    if isinstance(detail, dict):
        for cid, v in detail.items():
            if not isinstance(v, dict) or v.get("ok"):
                continue
            why = str(v.get("why") or "")[:160]
            item = {"id": str(cid), "why": why}
            # 请求层失败（连接被重置 / 超时）不是能力问题 —— 单列出来，
            # 免得把「网断了」当成「变笨了」，也免得被拿来当借口。
            if why.startswith("请求失败") or "ConnectionReset" in why or "timed out" in why:
                out["infra"].append(item)
            else:
                out["failing"].append(item)

    # 账本对照：只用来判断「是不是在刷分」，**绝不与尺子相加**
    try:
        led = []
        with open(_hist_path(), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                        # noqa: BLE001
                    continue
                if isinstance(r, dict) and r.get("kind") == "score_round":
                    try:
                        led.append(float(r.get("delta") or 0.0))
                    except Exception:                    # noqa: BLE001
                        pass
        s = round(sum(led[-int(window or 6):]), 2)
        out["ledger_sum"] = s
        out["grind"] = bool(s > 1.0 and (out["delta"] is None or out["delta"] <= 0))
    except Exception:                                    # noqa: BLE001
        pass
    # ★ 2026-11-24 修：尺子读数会「过期」。上一次跑分可能是几小时前甚至几天前，
    #   拿它判「这一轮有没有进步」等于用旧体检报告开今天的药。
    #   这里标出年龄；超过 6 小时就不下 grind 结论（宁可不下，也不下错的）。
    try:
        _iso = str((out.get("last") or {}).get("iso") or "")
        if _iso:
            _t = time.mktime(time.strptime(_iso[:19].replace("T", " "),
                                           "%Y-%m-%d %H:%M:%S"))
            out["age_h"] = round((time.time() - _t) / 3600.0, 2)
    except Exception:                                    # noqa: BLE001
        out["age_h"] = None
    _age = out.get("age_h")
    # 阈值取 24 小时：尺子不是每轮都跑（手动/定时），6 小时会把「上午跑过、
    # 下午干活」的正常情形误判成过期。超过一天才认定「这份读数不能拿来下结论」。
    out["stale"] = bool(_age is None or _age > 24)
    if out["stale"] and out["grind"]:
        out["grind"] = False
        out["grind_why"] = "尺子读数已过期（%s 小时前），不据此下刷分结论" % (
            ("%.1f" % _age) if isinstance(_age, (int, float)) else "未知")
    return out


def bench_brief(window=6, max_items=4):
    """把 bench_verdict 压成一段给反思提示词用的中文（读不到就返回空串）。

    ⚠️ 口径写死在文字里：**尺子涨才算真的变好**。这不是修辞，是用户
    2026-11-24 明确认可的判定标准（专家意见那张图）。
    """
    v = bench_verdict(window=window)
    if not v.get("ok"):
        return ""
    last, prev = v.get("last") or {}, v.get("prev") or {}
    lines = ["## 📏 尺子（项目外固定基准）最新读数 —— **这是主指标**"]
    lines.append("- 最近一次：%s/%s（%.1f%%）%s" % (
        _fmt_num(last.get("pass")), _fmt_num(last.get("total")),
        float(last.get("pct") or 0), ("  " + str(last.get("iso") or ""))[:19]))
    if prev:
        d = v.get("delta")
        lines.append("- 上一次：%s/%s（%.1f%%）→ 变化 %s 个百分点（%s）" % (
            _fmt_num(prev.get("pass")), _fmt_num(prev.get("total")),
            float(prev.get("pct") or 0),
            ("%+.1f" % d) if d is not None else "?",
            {"up": "涨", "down": "跌", "flat": "基本没动"}.get(
                v.get("trend") or "", "未知")))
    if v.get("grind"):
        lines.append("- ⚠️ **账本近 %d 轮累计 %+.2f，而尺子没涨** —— 这是刷分形态："
                     "干了很多活，但外部基准没变好。下一步必须去修尺子的失分点，"
                     "不要再提「内部重构/加日志/补测试」这类不会让尺子动的东西。"
                     % (int(window), float(v.get("ledger_sum") or 0)))
    # ★ 2026-11-24 专家意见（L3 三缺之二：没有留出集）：
    #   原来把每一道失分题（连题号带原因）原样喂给模型 —— 等于把考卷答案
    #   一起递过去，下次当然更容易过，但那叫背题，不叫变强。
    #   现在：只列可见集失分；留出集只报**数量**，题号和原因一律不给。
    try:
        from . import holdout as _HO
        _fail_all = v.get("failing") or []
        fail = _HO.visible_failures(_fail_all)
        _hidden_n = _HO.hidden_failure_count(_fail_all)
    except Exception:                                    # noqa: BLE001
        logger.debug("留出集过滤失败（退回全量失分）", exc_info=True)
        fail, _hidden_n = (v.get("failing") or []), 0
    if fail:
        lines.append("- 真实失分（这些是**能力/边界缺陷**，优先修）：")
        for it in fail[:max_items]:
            lines.append("  · %s —— %s" % (it.get("id"), (it.get("why") or "")[:140]))
    if _hidden_n:
        lines.append("- 另有 %d 题在**留出集**里失分（具体是哪几题不告诉你 ——"
                     " 这部分只用来验泛化，针对性打补丁没有意义）。" % _hidden_n)
    try:
        from . import holdout as _HO2
        infra = _HO2.visible_failures(v.get("infra") or [])
    except Exception:                                    # noqa: BLE001
        infra = v.get("infra") or []
    if infra:
        lines.append("- 请求层失败（断网/超时，不是能力问题，别当成缺陷去改代码）：")
        for it in infra[:max_items]:
            lines.append("  · %s —— %s" % (it.get("id"), (it.get("why") or "")[:120]))
    # ★ 2026-11-24：留出集分数（只给汇总，不给题号）—— 用来让模型自己看到
    #   「可见集满分、留出集没满」，从而去修通用能力而不是背题。
    try:
        from . import holdout as _HO3
        _ho_txt = _HO3.brief(_HO3.score_split(v.get("detail")))
        if _ho_txt:
            lines.append(_ho_txt)
    except Exception:                                    # noqa: BLE001
        logger.debug("注入留出集分数失败", exc_info=True)
    lines.append("⚠️ 判定口径：**尺子涨 = 真的在变好；账本涨而尺子不动 = 在刷分**。"
                 "账本只是施工计数器，不许拿它当成绩。")
    return "\n".join(lines)


def _fmt_num(x):
    """1.0 → 1，1.5 → 1.5（尺子的 pass/total 是 float，直接显示会很难看）。"""
    try:
        f = float(x)
        return str(int(f)) if abs(f - round(f)) < 1e-9 else ("%g" % f)
    except Exception:                                    # noqa: BLE001
        return "?"


def read_ledger_series(limit=120):
    """账本**序列**（只读）：history.jsonl 里 kind=score_round 的综合分轮。

    ⚠️ 施工单 8.2：只取账本自己的数，**不与尺子做任何合成**
       （不相加、不加权、不平均、不归一化）。前端拿到的是两列原始点。
    """
    out = []
    try:
        with open(_hist_path(), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                        # noqa: BLE001
                    continue
                if not isinstance(r, dict) or r.get("kind") != "score_round":
                    continue
                v = r.get("ledger")
                if v is None:
                    v = r.get("cum")
                if v is None:
                    continue
                try:
                    out.append({"iso": str(r.get("iso") or ""),
                                "v": round(float(v), 2)})
                except Exception:                        # noqa: BLE001
                    continue
    except Exception:                                    # noqa: BLE001
        return []
    return out[-int(limit or 120):]


def commit_round(before=None, after=None, actions=None, rollback=None,
                 note="", pid="", rebased=False, sample="round"):
    """把这一轮进退**累加**到累计分，并把原因与代码位置写进日志。

    返回 {"cum": 累计分, "delta": 本轮进退, "verdict": ..., "explain": {...}}
    """
    v = judge(before, after)
    ex = explain_delta(before, after, actions)
    d = load_ledger()
    delta = v.get("delta")
    cum_before = round(float(d.get("cum") or 0.0), 2)
    if delta is None:
        cum = cum_before
        verdict = "unknown"
    else:
        cum = round(cum_before + float(delta), 2)
        verdict = v.get("verdict")
    # ★ 2026-11-22 修：原来只在 before 非空时记 base —— 于是**第一次**评分
    #   （还没有上一次可比）base 永远是空的，账本里就查不到基准分，
    #   体检/面板「基准分」一栏永远显示不出来。现在 before 为空就退回取 after。
    if d.get("base") is None or rebased:
        _base_v = (before or {}).get("total")
        if _base_v is None:
            _base_v = (after or {}).get("total")
        if _base_v is not None:
            d["base"] = {"total": _base_v, "iso": _now(),
                         "rebased": bool(rebased)}
    rec = {
        "iso": _now(), "ts": time.time(), "plan_id": str(pid or ""),
        # ★ 2026-11-24：补 kind 字段。之前 ledger.jsonl 里的行**没有 kind**，
        #   而所有按 `kind == "score_round"` 过滤的读取方（曲线序列、刷分对照）
        #   都会把整份账本当成「没有一条有效轮次」。history.jsonl 有 kind、
        #   ledger.jsonl 没有 —— 同一件事两套格式，迟早出事，这里对齐。
        "kind": "score_round",
        # ⚠️ 施工单 8.4：字段名从 total 改成 ledger 不是为了好看 ——
        #   叫「分」就会拿来跟尺子比，叫「账」就不会。两个数并排存，永不相加。
        "ledger": cum,
        "bench": read_bench_last(),      # None 或 {"pass":8,"total":15,"pct":53.3,...}
        "cum_before": cum_before, "cum": cum,
        "delta": delta, "pct": v.get("pct"), "verdict": verdict,
        "before": v.get("before"), "after": v.get("after"),
        "per_part": v.get("per_part"), "rollback": rollback or None,
        "note": str(note or "")[:600],
        "explain": {"summary": ex["summary"], "suspects": ex["suspects"],
                    "new_codes": ex["new_codes"][:20],
                    "fixed_codes": ex["fixed_codes"][:20],
                    "changed_files": ex["changed_files"][:20]},
    }
    # ★ 2026-11-24：每轮都记下**尺子口径**的结论，账本从此只是计数器。
    #   放在 rec 里而不是只打日志：面板 / 卡片 / 反思都能直接引用同一个判定，
    #   不用各自去读 scores.jsonl（也就不会各算一套）。
    try:
        _bv = bench_verdict()
        rec["bench_verdict"] = {
            "ok": bool(_bv.get("ok")), "trend": _bv.get("trend"),
            "delta": _bv.get("delta"), "grind": bool(_bv.get("grind")),
            "ledger_sum": _bv.get("ledger_sum"),
            "failing": [x.get("id") for x in (_bv.get("failing") or [])][:8],
            "infra": [x.get("id") for x in (_bv.get("infra") or [])][:8],
        }
    except Exception:                                   # noqa: BLE001
        logger.debug("尺子口径计算失败", exc_info=True)
    # ★ 2026-11-24：账本涨、尺子不涨 = 刷分形态（专家口径，用户认可）。
    #   账本照加（活确实干了，累计分是「干活量」计数器），但 verdict **不能写进步** ——
    #   否则日志 / 卡片 / 反思都会把「刷分」读成「变好了」，这正是之前
    #   17:30 +8.00、17:42 +4.00、18:11 +3.00 三条全被判 improved 的原因。
    try:
        _bvj = _bv if isinstance(_bv, dict) else {}
    except Exception:                                   # noqa: BLE001
        _bvj = {}
    if bool(_bvj.get("grind")) and verdict == "improved":
        _gn = ("账本 %+.2f 但尺子未涨（近 6 轮账本合计 %+.2f，尺子 %s/%s）"
               % (float(delta or 0), float(_bvj.get("ledger_sum") or 0),
                  ((_bvj.get("last") or {}).get("pass")),
                  ((_bvj.get("last") or {}).get("total"))))
        verdict = "grind"
        rec["verdict"] = "grind"
        rec["grind"] = True
        rec["grind_note"] = _gn
        try:
            rec["explain"]["summary"] = (_gn + "；" + str(rec["explain"].get("summary") or ""))[:1200]
        except Exception:                               # noqa: BLE001
            pass
        ex["summary"] = rec["explain"].get("summary") if isinstance(rec.get("explain"), dict) else _gn
    d["cum"] = cum
    d["rounds"] = int(d.get("rounds") or 0) + 1
    d["last"] = rec
    evs = list(d.get("events") or [])
    evs.append({"iso": rec["iso"], "cum": cum, "delta": delta,
                "verdict": verdict, "summary": str(ex["summary"])[:300]})
    d["events"] = evs[-50:]
    _save_ledger(d)
    try:
        with _LOCK:
            with open(_ledger_jsonl_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("写累计明细失败", exc_info=True)
    # 人类可读日志
    arrow = "↑" if (delta or 0) > 0 else ("↓" if (delta or 0) < 0 else "→")
    lines = ["[%s] 累计 %+.2f → %+.2f  %s %s（%s）%s"
             % (rec["iso"], cum_before, cum, arrow,
                ("%+.2f" % float(delta)) if delta is not None else "?",
                {"improved": "进步", "regressed": "退步",
                 "flat": "持平",
                 "grind": "账本涨但尺子没涨（不算进步）"}.get(verdict, "未知"),
                (" 计划 " + str(pid)) if pid else "")]
    lines.append("    原因：" + ex["summary"])
    # ★ 2026-11-24：账本日志里**永远跟一行尺子**。理由很简单 —— 只看账本
    #   会得出「+4.00 在进步」的错觉，而同一时刻外部基准可能从 14/15 掉到
    #   12/15。两个数必须并排出现，人（和反思）才不会看错。
    try:
        _bv2 = bench_verdict()
        if _bv2.get("ok"):
            _l2, _p2 = (_bv2.get("last") or {}), (_bv2.get("prev") or {})
            _s2 = "    尺子：%s/%s（%.1f%%）" % (
                _fmt_num(_l2.get("pass")), _fmt_num(_l2.get("total")),
                float(_l2.get("pct") or 0))
            if _p2:
                _s2 += "，上轮 %.1f%% → %s" % (
                    float(_p2.get("pct") or 0),
                    {"up": "涨", "down": "跌", "flat": "没动"}.get(
                        _bv2.get("trend") or "", "未知"))
            if _bv2.get("grind"):
                _s2 += "  ⚠️ 账本在涨、尺子没涨（刷分嫌疑）"
            lines.append(_s2)
    except Exception:                                   # noqa: BLE001
        logger.debug("日志追加尺子读数失败", exc_info=True)
    for s in ex["suspects"][:3]:
        lines.append("    可疑代码：%s 共 %d 处，行 %s（%s）%s"
                     % (s["file"], s["count"],
                        ",".join(str(x) for x in s["lines"][:6]),
                        "/".join(s["kinds"]),
                        "  ★本轮改过" if s.get("in_changed") else ""))
    if rollback:
        lines.append("    回滚：" + str(rollback)[:300])
    if note:
        lines.append("    说明：" + str(note)[:300])
    # ★ 2026-11-25（用户要求「每次迭代之后折线都要刷新并显示新数据」）：
    #   **每轮迭代收尾**给 RSI 曲线追加一个采样点。
    #   原来只在「评估收尾」（约 2 小时一次）和每轮对话 hint 时采样 ——
    #   用户看到的线常常半天不动，也分不清那些点是什么时候的。
    #   放在写日志之前，日志里就能同时留下这一轮的时间与分数。
    _rsm = None
    try:
        from . import lessonloop as _ll_s
        # ★ 2026-11-25：评分闸门每分钟跑一次，同分时只延后最后一点，不刷同分点；
        #   真实迭代轮次（sample="round"）仍然每轮必追加一个点。
        _rsm = (_ll_s.rsi_sample_score() if str(sample) == "score"
                else _ll_s.rsi_sample_iter())
        if _rsm.get("ok") and not _rsm.get("skipped"):
            lines.append("    RSI 曲线采样：%s（综合 %s，共 %s 点）"
                         % (_rsm.get("iso"), _rsm.get("score"), _rsm.get("n")))
        elif _rsm.get("skipped"):
            lines.append("    RSI 曲线采样跳过：%s" % _rsm.get("why"))
    except Exception:                                   # noqa: BLE001
        logger.debug("每轮 RSI 采样失败（不影响本轮跑分）", exc_info=True)
    _append_log(lines)
    # ══ 保护区段 · RSI 基准（施工单 T3 · 5.2）══════════════════════════
    # ⚠️⚠️ 这段**不许被自迭代改写或删除**。它一旦被「优化」掉，就永远不跑分了，
    #   而且查不出来（没有报错、没有日志）。
    #   主通道是 Windows 计划任务 hwa_bench（系统层，自迭代改不到）；
    #   这里只是补充：迭代轮次结束顺手跑一次，被删也不致命。
    #   bench.py 自己带文件锁：两个通道撞上时后到的直接退出，不会并发写坏哈希链。
    # ══════════════════════════════════════════════════════════════════
    # 🔒LOCK:rsi-bench-trigger
    #   施工单 C4：这段**不许被自迭代删掉**。
    #   主保障是 Windows 计划任务 hwa_bench（操作系统层，自迭代改不到）；
    #   这里只是补充通道。若要再加一道「锁区清单」保护，需人工在
    #   backend/selfdev.py 的 _LOCKED_BLOCKS 里加一行：
    #       "backend/iterloop.py": [("<LOCK 标记>", "<END 标记>")]
    #   （该文件按设计禁止模型改写，所以这一步只能由人来做。）
    _BENCH = os.environ.get("HWA_BENCH", r"D:\hwa_bench\bench.py")
    if os.path.exists(_BENCH):
        try:
            import subprocess
            subprocess.Popen(
                [sys.executable, _BENCH], cwd=os.path.dirname(_BENCH),
                creationflags=0x08000000,        # CREATE_NO_WINDOW，不弹黑框
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            logger.debug("启动 bench 失败", exc_info=True)
    # 🔒END:rsi-bench-trigger
    # ⚠️ 2026-11-22 体系体检抓到：commit_round 只写了账本 / 明细 / 文本日志，
    #    **从来没写 history.jsonl** —— 而 stats()、history()、前端进退看板读的
    #    正是它。后果：单独调用 commit_round 时账本 rounds 在涨、看板永远 0 轮。
    #    这里补一条，并打 kind 标记：execute_approved 里 record_round 记的是
    #    「动作轮」，这里记的是「综合分轮」，两者都留着但可区分，不会重复计数。
    try:
        with _LOCK:
            with open(_hist_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": rec["ts"], "iso": rec["iso"], "kind": "score_round",
                    "plan_id": rec["plan_id"],
                    "before": rec["before"], "after": rec["after"],
                    "delta": delta, "pct": rec["pct"], "verdict": verdict,
                    "per_part": rec["per_part"],
                     "cum": cum, "cum_before": cum_before,
                     "ledger": cum, "bench": rec.get("bench"),
                     "rollback": rec["rollback"], "note": rec["note"],
                    "actions": [{"id": str(a.get("id") or ""),
                                 "title": str(a.get("title") or "")[:80],
                                 "ok": bool(a.get("ok"))}
                                for a in (actions or [])][:20],
                }, ensure_ascii=False) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("写进退历史失败", exc_info=True)
    return {"cum": cum, "delta": delta, "verdict": verdict,
            "cum_before": cum_before, "explain": ex,
            # ★ 2026-11-24：把刷分判定带出返回值 —— 调用方（迭代汇报 / 卡片 /
            #   反思）要能显示「账本 +3 但尺子没涨」，而不是只看到一个字符串。
            "grind": bool(rec.get("grind")),
            "grind_note": str(rec.get("grind_note") or ""),
            # ★ 2026-11-25：把「这一轮曲线采样的结果」带出去。
            #   调用方（self_iterate.execute_approved）拿它判断「本轮是否已采过」，
            #   避免只靠 3 秒时间窗去猜 —— explain_delta 可能调模型耗时几十秒，
            #   靠时间窗会漏判、多记一个同分点。
            "rsi_sample": _rsm,
            "kind": "score_round"}


def cumulative():
    """当前累计分 + 最近事件（给接口/卡片/反思用）。"""
    d = load_ledger()
    return {"cum": round(float(d.get("cum") or 0.0), 2),
            "rounds": int(d.get("rounds") or 0),
            "started": d.get("started"), "base": d.get("base"),
            "last": d.get("last"), "events": (d.get("events") or [])[-10:]}


# ------------------------------------------------- 重启后复核（闭环最后一环）
def _post_verify_path():
    return os.path.join(_data_dir(), "post_verify.json")


def mark_post_verify(pid=""):
    """标记「重启后要再量一次分」。

    ⚠️ 2026-11-22 实测：self_iterate._schedule_auto_restart 一直在调这个函数，
       但 iterloop 里**从来没实现过** —— 每轮都抛 AttributeError 被 except
       静默吞掉，于是「自动重启 → 自动验证」这条闭环其实是断的。
    """
    try:
        with _LOCK:
            with open(_post_verify_path(), "w", encoding="utf-8") as fh:
                json.dump({"pid": str(pid or ""), "ts": time.time(),
                           "iso": _now()}, fh, ensure_ascii=False)
        return True
    except Exception:                                   # noqa: BLE001
        logger.debug("写重启后复核标记失败", exc_info=True)
        return False


def post_verify_pending():
    """有没有待复核的标记（不消费）。"""
    try:
        with open(_post_verify_path(), encoding="utf-8") as fh:
            return isinstance(json.load(fh), dict)
    except Exception as e:                              # noqa: BLE001
        # 没有标记（FileNotFoundError）是常态；标记坏了同样返回 False，
        # 但那意味着「重启后复核」这条闭环悄悄不跑了 —— 至少留一条 debug。
        logger.debug("读重启后复核标记失败（视为无待复核）: %s: %s",
                     type(e).__name__, e)
        return False


def take_post_verify():
    """取走标记（取完即删，避免反复触发）。"""
    p = _post_verify_path()
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:                              # noqa: BLE001
        logger.debug("取重启后复核标记失败（本次不复核）: %s: %s",
                     type(e).__name__, e)
        return None
    try:
        os.remove(p)
    except OSError as e:                                # noqa: BLE001
        # ★ 这是本文件最后一处 `except ...: pass`。删不掉标记本身不致错
        #   （下次还会复核一次），但「一直删不掉」意味着目录权限出了问题，
        #   以前完全查不到 —— 现在留 debug，且照样把标记内容返回给调用方。
        logger.debug("删重启后复核标记失败（不影响本次复核）: %s: %s",
                     type(e).__name__, e)
    return d if isinstance(d, dict) else None


def run_post_verify(pid=""):
    """重启后再量一次分，把「重启后是否仍然正常」记进账本与日志。

    与普通轮次不同：**不再累加累计分**（这是同一个状态的重测，不是新改动），
    只记录「重启后 vs 重启前最后一次测量」的差，差得多说明新代码起不来或有副作用。
    """
    mark = take_post_verify()
    if not mark and not pid:
        return None
    pid = str((mark or {}).get("pid") or pid or "")
    try:
        s = score(full=True)
        total = s.get("total")
        if total is None:
            out = {"ok": False, "error": "重启后评分拿不到", "pid": pid,
                   "verdict": "error", "plan_id": pid}
            try:
                _push_post_verify_card(out)
            except Exception:                           # noqa: BLE001
                logger.debug("推任务结束卡片失败", exc_info=True)
            return out
        d = load_ledger()
        prev = (d.get("last") or {}).get("after")
        delta = None if prev is None else round(float(total) - float(prev), 2)
        verdict = "ok" if (delta is None or delta >= 0) else "worse"
        rec = {"iso": _now(), "ts": time.time(), "plan_id": pid,
               "kind": "post_restart", "total": total, "prev": prev,
               "delta": delta, "verdict": verdict}
        evs = list(d.get("events") or [])
        # ★ 2026-11-24 修：这条复核**不累加累计分**（同一状态重测），原来却把
        #   delta 原样塞进 events —— 于是曲线图旁边出现「delta +6.00、cum 却没动」
        #   的矛盾点，而且它不写 ledger.jsonl，前端按 jsonl 画线时这一段时间
        #   完全没有点（实测 13:15 与 17:14 两条就是这样，看着像「曲线好久不动」）。
        #   现在 delta 置 None、标 kind=post_restart，真实差值只写在 summary 里。
        evs.append({"iso": rec["iso"], "cum": d.get("cum"), "delta": None,
                    "verdict": verdict, "kind": "post_restart",
                    "note_delta": delta,
                    "summary": "重启后复核 %s（重启前 %s，差 %s）"
                               % (total, prev,
                                  ("%+.2f" % delta) if delta is not None else "无基线")})
        d["events"] = evs[-50:]
        d["post_verify"] = rec
        _save_ledger(d)
        _append_log(["[%s] 重启后复核 总分 %s（重启前 %s，%s）%s"
                     % (rec["iso"], total, prev,
                        ("%+.2f" % delta) if delta is not None else "无基线",
                        (" 计划 " + pid) if pid else "")])
        out = {"ok": verdict == "ok", "total": total, "prev": prev,
               "delta": delta, "cum": d.get("cum"), "verdict": verdict,
               "pid": pid, "plan_id": pid}
        # ★ 2026-11-23：复核结论必须变成界面上的一张卡，不能只进日志。
        try:
            _push_post_verify_card(out)
        except Exception:                               # noqa: BLE001
            logger.debug("推任务结束卡片失败", exc_info=True)
        return out
    except Exception as e:                              # noqa: BLE001
        logger.warning("重启后复核失败", exc_info=True)
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
               "pid": pid, "verdict": "error", "plan_id": pid}
        try:
            _push_post_verify_card(out)
        except Exception:                               # noqa: BLE001
            logger.debug("推任务结束卡片失败", exc_info=True)
        return out


# ------------------------------------------------- 任务结束卡片（待播报队列）
# ★ 2026-11-23 用户原话：
#   「自动迭代重启之后没有弹出卡片并显示已验证然后结束，结束任务时卡片必须有弹出来」
#
# 根因：run_post_verify 跑完只写 logger 和账本，**从来没有推给前端**。
#   而重启后复核是在新进程的后台线程里跑的，跑完线程就结束了 ——
#   界面上什么都不会出现，用户只能自己去翻日志，等于「验证了但没人知道」。
#
# 做法：把复核结论落成一条「待播报卡片」（落盘，不靠内存 ——
#   复核在新进程里，用户刷新页面可能更晚），前端轮询取走 → 渲染成
#   对话里的任务结束卡 → ack 掉。取走前一直留着，刷新也不会丢。
_CARD_KEEP = 20


def _cards_path():
    return os.path.join(_data_dir(), "pending_cards.jsonl")


def _read_cards():
    try:
        with open(_cards_path(), encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if str(x).strip()]
    except Exception as e:                              # noqa: BLE001
        logger.debug("读待播报卡片失败（视为无卡）: %s: %s", type(e).__name__, e)
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _write_cards(rows):
    # 2026-11-24 实测：前端轮询读卡片时 os.replace 会抛 WinError 5，
    # 导致 ack 清空失败、卡片永远播不完。改走 safeio（重试 + 降级）。
    with _LOCK:
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                       for r in list(rows)[-_CARD_KEEP:])
        safeio.atomic_write_text(_cards_path(), body)


def push_card(card):
    """追加一张待播报卡片（前端取走后 ack）。"""
    try:
        d = dict(card or {})
        d.setdefault("id", "c%d" % int(time.time() * 1000))
        d.setdefault("ts", time.time())
        d.setdefault("at", _now())
        rows = _read_cards()
        rows.append(d)
        _write_cards(rows)
        return {"ok": True, "id": d["id"], "n": len(rows[-_CARD_KEEP:])}
    except Exception as e:                              # noqa: BLE001
        logger.warning("写待播报卡片失败", exc_info=True)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def pending_cards():
    """还没被前端取走的卡片（不消费）。"""
    return _read_cards()


def ack_cards(ids=None):
    """前端已渲染的卡片出队。ids 为空表示全部出队。"""
    rows = _read_cards()
    if not ids:
        _write_cards([])
        return {"ok": True, "left": 0}
    drop = {str(x) for x in ids}
    left = [r for r in rows if str(r.get("id")) not in drop]
    _write_cards(left)
    return {"ok": True, "left": len(left)}


def _rsi_snapshot():
    """卡片上要带的 RSI 快照：分数 / 趋势形态 / 最该修。

    RSI 的实质不是「有个分数」，而是**拿分数决定下一步改什么** ——
    所以任务结束卡上必须同时出现「有没有改坏」（复核分数）和
    「有没有变好」（RSI 与最弱项），各说各话才算证据。
    """
    try:
        from . import lessonloop as _LL
        idx = _LL.rsi_index(cache_sec=0) or {}
        tr = _LL.rsi_trend() or {}
        wk = _LL.rsi_weakest() or {}
        return {
            "score": idx.get("score"),
            "shape": tr.get("shape"),
            "slope": tr.get("slope"),
            "samples": tr.get("n"),
            "weakest": ({"name": wk.get("name"),
                         "value": wk.get("value"),
                         "drag": wk.get("drag"),
                         "advice": wk.get("advice")}
                        if wk.get("ok") else None),
        }
    except Exception:                                   # noqa: BLE001
        logger.debug("取 RSI 快照失败（卡片不带 RSI）", exc_info=True)
        return None


def _judge_task(pid):
    """本轮任务到底成没成 —— 决定结束卡片是绿还是红。

    ★ 2026-11-24 用户原话：
      「绿色已验证是验证到完全没问题且功能已经能够完全正常运转才显示的啊，
        标示着任务成功结束；如果不能正常运作显示红色字迭代失败即将修复，
        然后重新反复验证修复直到完全正常为止。」

    所以判据**不能只看「重启后分数没退步」** —— 那只证明程序起得来，
    证明不了这一轮要做的事做完了。必须同时满足：
      ① 计划里每一条动作都真的 done（没有 pending / approved 等没执行的）；
      ② 一条 failed / need_review 都没有。

    返回 {"ok": True|False|None, "verdict": "success|failed|none",
          "done"/"total"/"pending"/"failed": int, "why": str}
    ok=None 表示「这次重启没有任务上下文」，由调用方退回按分数判定。
    """
    pid = str(pid or "").strip()
    if not pid:
        return {"ok": None, "verdict": "none", "done": 0, "total": 0,
                "pending": 0, "failed": 0,
                "why": "这次重启没有关联的迭代计划，只能按重启后评分判定"}
    try:
        from . import self_iterate as _SI
        d = _SI.load_plans()
    except Exception as e:                              # noqa: BLE001
        logger.warning("判任务成败时读计划失败", exc_info=True)
        return {"ok": False, "verdict": "failed", "done": 0, "total": 0,
                "pending": 0, "failed": 0,
                "why": "读计划失败，无法确认任务完成：%s: %s"
                       % (type(e).__name__, e)}
    p = None
    for x in (d.get("items") or []):
        if isinstance(x, dict) and str(x.get("id") or "") == pid:
            p = x
            break
    if p is None:
        return {"ok": False, "verdict": "failed", "done": 0, "total": 0,
                "pending": 0, "failed": 0,
                "why": "找不到计划 %s（可能已被删除），无法确认任务完成" % pid}
    acts = [a for a in (p.get("actions") or []) if isinstance(a, dict)]

    def _st(a):
        return str(a.get("state") or a.get("status") or "pending").strip().lower()

    total = len(acts)
    done = sum(1 for a in acts if _st(a) == "done")
    bad = sum(1 for a in acts if _st(a) in ("failed", "need_review"))
    wait = max(0, total - done - bad)
    ok = bool(total > 0 and done == total and bad == 0)
    if ok:
        why = "计划 %d 条动作全部完成，没有失败" % total
    elif total == 0:
        why = "计划里一条动作都没有，等于什么都没做"
    else:
        bits = []
        if bad:
            bits.append("%d 条失败/待复核" % bad)
        if wait:
            bits.append("%d 条还没执行（待批准或未开始）" % wait)
        why = "计划 %d 条动作里只完成 %d 条，%s" % (total, done, "，".join(bits))
    return {"ok": ok, "verdict": "success" if ok else "failed",
            "done": done, "total": total, "pending": wait, "failed": bad,
            "why": why}


def _push_post_verify_card(rec):
    """把一次重启后复核的结果变成「任务结束卡」，并推给前端。

    失败（分数掉了 / 起不来 / 量不到分）时：
      · 卡片标红，写清原因；
      · 按用户 2026-11-23 的要求**自动重排一轮修复计划**
        （走 self_iterate._schedule_replan：它自带预算闸门与 90 秒冷却，
         不会变成「失败→重排→又失败」的死循环）。
    """
    rec = dict(rec or {})
    verdict = str(rec.get("verdict") or "")
    err = str(rec.get("error") or "")
    total = rec.get("total")
    prev = rec.get("prev")
    delta = rec.get("delta")

    # ★ 绿色 = 真的验收通过。只有「重启后能起来、分没退步」是不够的 ——
    #   那证明不了这一轮要做的事做完了（实测：8 条动作只做完 1 条，
    #   其余 7 条还待批准，旧判定照样给绿「已验证」，属于误报）。
    task = _judge_task(rec.get("plan_id"))
    task_ok = task.get("ok")                     # None = 无任务上下文
    score_ok = (verdict == "ok") and not err
    ok = bool(score_ok and task_ok is not False)

    if ok:
        state, badge = "verified", "已验证"
        title = "任务结束 · 已验收通过"
        parts = []
        if (prev is not None and total is not None
                and isinstance(delta, (int, float))):
            parts.append("重启后程序正常起来，评分没有退步（%s → %s，%+.2f）"
                         % (prev, total, float(delta)))
        else:
            parts.append("重启后程序正常起来，评分正常")
        if task_ok is True:
            parts.append("本轮任务 %d 条动作全部完成、没有失败"
                         % int(task.get("total") or 0))
        why = "；".join(parts)
    else:
        state, badge = "failed", "迭代失败 · 即将修复"
        title = "任务结束 · 未通过验收，已排入修复"
        reasons = []
        if err:
            reasons.append("重启后没量到分：%s" % err[:200])
        elif isinstance(delta, (int, float)) and delta < 0:
            reasons.append("重启后评分退步 %.2f 分（%s → %s）"
                           % (abs(delta), prev, total))
        if task_ok is False:
            reasons.append(str(task.get("why") or "本轮任务没有全部完成"))
        why = "；".join(reasons) or "重启后复核没通过"

    card = {
        "kind": "post_restart",
        "state": state,
        "badge": badge,
        "title": title,
        "why": why,
        "pid": str(rec.get("plan_id") or ""),
        "total": total,
        "prev": prev,
        "delta": delta,
        "rsi": _rsi_snapshot(),
        "task": task,
        "replan": None,
    }

    if not ok:
        # 什么情况才真去重排修复计划（= 用户要的「即将修复」）：
        #   · 起不来 / 量不到分 / 评分退步 → 代码真坏了，必须修；
        #   · 有动作 failed / need_review   → 有活干砸了，必须修。
        # 只是「还有动作没执行（待批准）」不算坏：不重复造计划，
        # 否则每次复核都造一份新计划，待批准队列会爆炸。
        need_fix = bool(err) or (isinstance(delta, (int, float)) and delta < 0) \
            or int(task.get("failed") or 0) > 0
        if not need_fix:
            card["replan"] = {
                "ok": True, "skipped": True,
                "why": "还有 %d 条动作没执行（待批准），不重复造修复计划；"
                       "批准后接着跑" % int(task.get("pending") or 0)}
        else:
            try:
                from . import self_iterate as _SI
                r = _SI._schedule_replan(
                    pid=card["pid"],
                    reason=why,
                    before=prev, after=total,
                    context="重启后复核失败（post_restart）：%s" % why[:300],
                )
                card["replan"] = {"ok": bool(r.get("ok")),
                                  "skipped": bool(r.get("skipped")),
                                  "why": r.get("why") or r.get("error") or ""}
            except Exception as e:                      # noqa: BLE001
                logger.warning("复核失败后重排修复计划失败", exc_info=True)
                card["replan"] = {"ok": False,
                                  "why": "%s: %s" % (type(e).__name__, e)}

    r = push_card(card)
    logger.info("[loop] 任务结束卡片已推送：%s（%s）", badge, r)
    return r


# ---------------------------------------------------------------- 落盘
def record_round(pid="", before=None, after=None, verdict=None,
                 actions=None, rollback=None, note="", extra=None):
    """记一轮迭代的进退。任何异常都不抛（记录写不进去不能搞挂主流程）。"""
    try:
        v = verdict or judge(before, after)
        rec = {
            "ts": time.time(),
            "iso": _now(),
            "plan_id": str(pid or ""),
            "before": (before or {}).get("total"),
            "after": (after or {}).get("total"),
            "delta": v.get("delta"),
            "pct": v.get("pct"),
            "verdict": v.get("verdict"),
            "per_part": v.get("per_part"),
            "actions": [{"id": str(a.get("id") or ""),
                         "title": str(a.get("title") or "")[:80],
                         "ok": bool(a.get("ok"))}
                        for a in (actions or [])][:20],
            "rollback": rollback or None,
            "note": str(note or "")[:600],
        }
        if extra:
            rec["extra"] = extra
        with _LOCK:
            with open(_hist_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:                              # noqa: BLE001
        logger.debug("记录进退失败: %s", e)
        return {"error": "%s: %s" % (type(e).__name__, e)}


def history(n=10):
    """最近 n 轮进退记录（新的在前）。"""
    out = []
    try:
        with open(_hist_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception as e:                  # noqa: BLE001
                    # 坏行跳过是刻意的（一行坏了不该把整段历史弄没），
                    # 但要留 debug —— 以前整段 continue 掉，账本被写坏毫无痕迹。
                    logger.debug("history 跳过坏行: %s: %s", type(e).__name__, e)
                    continue
    except Exception as e:                              # noqa: BLE001
        logger.debug("history 读不到进退记录（按空历史）: %s: %s",
                     type(e).__name__, e)
        return []
    return out[-int(n):][::-1]


# ---------------------------------------------------------------- 失败次数与优先级
def _load_att():
    try:
        with open(_att_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception as e:                              # noqa: BLE001
        # 读不到失败计数 = 所有问题都从 0 次起步，优先级会算偏 —— 留一条 debug。
        logger.debug("读 attempts 失败（按空表）: %s: %s", type(e).__name__, e)
        return {}


def _save_att(d):
    try:
        with _LOCK:
            safeio.atomic_write_json(_att_path(), d, indent=1)
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)


def bump_attempt(key, reason="", detail=""):
    """同一个问题失败一次 → 次数 +1、优先级下降。

    用户 2026-11-22：「不限次数，但每次都要记录并降低尝试优先级；
    不断记录为什么失败，每次失败重新执行前都得反思失败原因然后重新进行」。
    """
    key = str(key or "").strip()[:160]
    if not key:
        return {}
    d = _load_att()
    cur = d.get(key) or {}
    n = int(cur.get("attempts") or 0) + 1
    reasons = list(cur.get("reasons") or [])
    if reason:
        reasons.append({"ts": _now(), "why": str(reason)[:600],
                        "detail": str(detail)[:600]})
    d[key] = {
        "attempts": n,
        "priority": max(10, 100 - 8 * n),
        "last": _now(),
        "reasons": reasons[-12:],
        "status": "trying" if n < 5 else "hard",
    }
    _save_att(d)
    return d[key]


def attempts(key):
    d = _load_att().get(str(key or "").strip()[:160]) or {}
    return int(d.get("attempts") or 0)


def priority(key):
    """优先级：100 起步，每失败一次 -8，最低 10。"""
    d = _load_att().get(str(key or "").strip()[:160]) or {}
    if not d:
        return 100
    return int(d.get("priority") or max(10, 100 - 8 * int(d.get("attempts") or 0)))


def reflect_before_retry(key, n=4):
    """重试前必须先读：这个坑踩过几次、每次为什么失败。

    返回一段可直接塞进提示词的文字；没踩过就返回空串。
    """
    key = str(key or "").strip()[:160]
    d = _load_att().get(key) or {}
    if not d:
        return ""
    rs = list(d.get("reasons") or [])[-int(n):]
    lines = ["这个问题已经失败 %d 次（优先级降到 %d/100）。"
             "**重试前必须先想清楚下面每次为什么失败，不要重复同样的做法**："
             % (int(d.get("attempts") or 0), int(d.get("priority") or 10))]
    for i, r in enumerate(rs, 1):
        lines.append("  %d) [%s] %s" % (i, r.get("ts", ""), r.get("why", "")))
        if r.get("detail"):
            lines.append("     细节：%s" % r.get("detail"))
    return "\n".join(lines)


def clear_attempt(key):
    """问题真的改好了，清掉失败计数（优先级回到 100）。"""
    d = _load_att()
    k = str(key or "").strip()[:160]
    if k in d:
        d.pop(k, None)
        _save_att(d)
        return True
    return False


# ---------------------------------------------------------------- 回滚
def rollback_until_good(results, target, restore, rescore=None, log=None):
    """先单条回滚，不行再整轮。

    用户 2026-11-22 选的是「先试单条回滚，不行再整轮回滚」。
    逐条**逆序**撤（后面的改动通常依赖前面的），每撤一条重新评分，
    一旦回到 target 之上立刻停手 —— 只撤坏的那几条，好的保留。

    results: [{"id","ok","backup","title",...}]（execute_approved 的结果）
    target : 改动前的综合分（回到 >= target 就算恢复）
    restore: 回调 restore(backup_name) → bool
    rescore: 回调 rescore() → dict（新的评分）；None 则用 score()
    log    : 回调 log(text) 写事件

    返回 {"rolled": [...], "final": 分数, "round_rollback": bool, "reason": str}
    """
    rescore = rescore or (lambda: score(full=True))
    log = log or (lambda t: None)
    cand = [r for r in (results or [])
            if r.get("ok") and not r.get("skipped") and r.get("backup")]
    rolled, last = [], None
    for r in reversed(cand):
        try:
            ok = bool(restore(r.get("backup")))
        except Exception as e:                          # noqa: BLE001
            ok = False
            log("单条回滚 %s 抛错：%s" % (r.get("id"), e))
        if not ok:
            continue
        rolled.append({"id": str(r.get("id") or ""),
                       "title": str(r.get("title") or "")[:80]})
        log("已单条回滚 %s（%s）" % (r.get("id"), r.get("title")))
        try:
            s = rescore()
            last = s.get("total")
        except Exception:                               # noqa: BLE001
            last = None
        if last is not None and target is not None and float(last) >= float(target):
            return {"rolled": rolled, "final": last, "round_rollback": False,
                    "reason": "单条回滚后评分已回到 %.2f（目标 %.2f），保留其余改动"
                              % (float(last), float(target))}
    # 单条撤完还是不行 → 整轮回滚（由调用方用 _iter_snapshot 恢复）
    return {"rolled": rolled, "final": last, "round_rollback": True,
            "reason": ("单条回滚 %d 条后评分仍为 %s（目标 %s），需要整轮回滚"
                       % (len(rolled), last, target))}


# ---------------------------------------------------------------- 给反思用
def brief(n=6):
    """最近 n 轮的进退记录 + 失败次数最多的坑，压成提示词片段。"""
    hs = history(n)
    lines = []
    # ★ 2026-11-22 用户要求「反思也需要重点参考这个方面」——
    #   累计分与归因代码放最前面，模型第一眼就看见进退多少、卡在哪段代码。
    try:
        _cum = cumulative()
        lines.append("【累计跑分（从 0 起累加，无上限，负数 = 净退步）】%+.2f 分，共 %d 轮"
                     % (float(_cum.get("cum") or 0.0),
                        int(_cum.get("rounds") or 0)))
        _evs = _cum.get("events") or []
        if _evs:
            lines.append("  最近几轮：")
            for e in _evs[-6:]:
                _d = e.get("delta")
                lines.append("    · [%s] %s %+.2f → 累计 %+.2f（%s）"
                             % (str(e.get("iso") or "")[:16],
                                "↑" if (_d or 0) > 0 else ("↓" if (_d or 0) < 0 else "→"),
                                float(_d or 0.0), float(e.get("cum") or 0.0),
                                str(e.get("summary") or "")[:160]))
        _susp = (((_cum.get("last") or {}).get("explain") or {})
                 .get("suspects") or [])
        if _susp:
            lines.append("  上一轮加减分最集中的代码（改进计划优先处理这些）：")
            for s in _susp[:4]:
                lines.append("    · %s 行 %s（%s）%s"
                             % (s.get("file"),
                                ",".join(str(x) for x in (s.get("lines") or [])[:5]),
                                "/".join(s.get("kinds") or []),
                                "  ★本轮改过" if s.get("in_changed") else ""))
    except Exception:                                   # noqa: BLE001
        logger.debug("累计分摘要失败", exc_info=True)
    if hs:
        lines.append("【最近 %d 轮迭代的进退（单轮绝对分 0-100，只当测量尺）】" % len(hs))
        for h in hs:
            d = h.get("delta")
            arrow = "↑" if (d or 0) > 0 else ("↓" if (d or 0) < 0 else "→")
            lines.append(
                "  · [%s] %s → %s  %s %s%s（%s）"
                % (h.get("iso", "")[:16], h.get("before"), h.get("after"),
                   arrow, ("%+.2f" % d) if d is not None else "?",
                   (" (%+.2f%%)" % h["pct"]) if h.get("pct") is not None else "",
                   {"improved": "进步", "regressed": "退步",
                    "flat": "持平"}.get(h.get("verdict"), "未知")))
            if h.get("rollback"):
                lines.append("    回滚：%s" % str(h["rollback"])[:200])
            if h.get("note"):
                lines.append("    说明：%s" % str(h["note"])[:200])
    att = _load_att()
    hard = sorted(att.items(), key=lambda kv: -int((kv[1] or {}).get("attempts") or 0))[:5]
    hard = [(k, v) for k, v in hard if int((v or {}).get("attempts") or 0) > 0]
    if hard:
        lines.append("【反复失败的坑（次数越多优先级越低，别再重复同一做法）】")
        for k, v in hard:
            lines.append("  · %s —— 失败 %d 次，优先级 %d/100，最后一次：%s"
                         % (k[:70], int(v.get("attempts") or 0),
                            int(v.get("priority") or 10),
                            (v.get("reasons") or [{}])[-1].get("why", "")[:160]))
    return "\n".join(lines)


def stats():
    """给接口 / 卡片用的汇总。"""
    hs = history(50)
    imp = sum(1 for h in hs if h.get("verdict") == "improved")
    reg = sum(1 for h in hs if h.get("verdict") == "regressed")
    rb = sum(1 for h in hs if h.get("rollback"))
    ds = [h.get("delta") for h in hs if h.get("delta") is not None]
    return {
        "cumulative": cumulative(),
        "rounds": len(hs),
        "improved": imp,
        "regressed": reg,
        "flat": sum(1 for h in hs if h.get("verdict") == "flat"),
        # ★ 2026-11-24：账本涨、尺子没涨的轮次单列，不混进「进步」里凑数
        "grind": sum(1 for h in hs if h.get("verdict") == "grind"),
        "rollbacks": rb,
        "avg_delta": round(sum(ds) / len(ds), 2) if ds else None,
        "best": max(ds) if ds else None,
        "worst": min(ds) if ds else None,
        "last": hs[0] if hs else None,
    }
