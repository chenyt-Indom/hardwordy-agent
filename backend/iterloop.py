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
ABILITY_PER_FILE = 0.03       # 每个编译通过的后端文件（广度，弱权重）

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
        return {
            "score": round(rate * W_REGRESS, 2),
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
    except FileNotFoundError:
        pass
    except Exception:                                   # noqa: BLE001
        logger.debug("读 ability_snapshot 失败，按空快照处理", exc_info=True)
    return {"failed": [], "fixed": [], "lessons": []}


def _save_ability_snap(snap):
    try:
        with open(_ability_snap_path(), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
        return True
    except Exception:                                   # noqa: BLE001
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
        for _row in (_fix_landed_rows(rg), _lesson_fixed_rows()):
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
            tmp = _ledger_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, _ledger_path())
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

    bits = []
    for k, label in (("static", "静态"), ("regress", "回归"), ("func", "功能")):
        d = per.get(k)
        if d:
            bits.append("%s%+.2f" % (label, float(d)))
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

    return {"per_part": per, "new_codes": new_codes[:40],
            "fixed_codes": fixed_codes[:40], "suspects": suspects,
            "changed_files": changed[:20], "regress_added": rg_added[:8],
            "regress_fixed": rg_fixed[:8], "func_added": fn_added[:8],
            "func_fixed": fn_fixed[:8], "smoke_bad": smoke_bad[:8],
            "summary": summary[:1200]}


def commit_round(before=None, after=None, actions=None, rollback=None,
                 note="", pid="", rebased=False):
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
    d["cum"] = cum
    d["rounds"] = int(d.get("rounds") or 0) + 1
    d["last"] = rec
    evs = list(d.get("events") or [])
    evs.append({"iso": rec["iso"], "cum": cum, "delta": delta,
                "verdict": verdict, "summary": ex["summary"][:300]})
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
                 "flat": "持平"}.get(verdict, "未知"),
                (" 计划 " + str(pid)) if pid else "")]
    lines.append("    原因：" + ex["summary"])
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
    _append_log(lines)
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
                    "rollback": rec["rollback"], "note": rec["note"],
                    "actions": [{"id": str(a.get("id") or ""),
                                 "title": str(a.get("title") or "")[:80],
                                 "ok": bool(a.get("ok"))}
                                for a in (actions or [])][:20],
                }, ensure_ascii=False) + "\n")
    except Exception:                                   # noqa: BLE001
        logger.debug("写进退历史失败", exc_info=True)
    return {"cum": cum, "delta": delta, "verdict": verdict,
            "cum_before": cum_before, "explain": ex}


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
    except Exception:                                   # noqa: BLE001
        return False


def take_post_verify():
    """取走标记（取完即删，避免反复触发）。"""
    p = _post_verify_path()
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:                                   # noqa: BLE001
        return None
    try:
        os.remove(p)
    except OSError:                                     # noqa: BLE001
        pass
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
            return {"ok": False, "error": "重启后评分拿不到", "pid": pid}
        d = load_ledger()
        prev = (d.get("last") or {}).get("after")
        delta = None if prev is None else round(float(total) - float(prev), 2)
        verdict = "ok" if (delta is None or delta >= 0) else "worse"
        rec = {"iso": _now(), "ts": time.time(), "plan_id": pid,
               "kind": "post_restart", "total": total, "prev": prev,
               "delta": delta, "verdict": verdict}
        evs = list(d.get("events") or [])
        evs.append({"iso": rec["iso"], "cum": d.get("cum"), "delta": delta,
                    "verdict": verdict,
                    "summary": "重启后复核 %s（重启前 %s）" % (total, prev)})
        d["events"] = evs[-50:]
        d["post_verify"] = rec
        _save_ledger(d)
        _append_log(["[%s] 重启后复核 总分 %s（重启前 %s，%s）%s"
                     % (rec["iso"], total, prev,
                        ("%+.2f" % delta) if delta is not None else "无基线",
                        (" 计划 " + pid) if pid else "")])
        return {"ok": verdict == "ok", "total": total, "prev": prev,
                "delta": delta, "cum": d.get("cum"), "verdict": verdict,
                "pid": pid}
    except Exception as e:                              # noqa: BLE001
        logger.warning("重启后复核失败", exc_info=True)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "pid": pid}


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
                except Exception:                       # noqa: BLE001
                    continue
    except Exception:                                   # noqa: BLE001
        return []
    return out[-int(n):][::-1]


# ---------------------------------------------------------------- 失败次数与优先级
def _load_att():
    try:
        with open(_att_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def _save_att(d):
    try:
        with _LOCK:
            tmp = _att_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, _att_path())
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
        "rollbacks": rb,
        "avg_delta": round(sum(ds) / len(ds), 2) if ds else None,
        "best": max(ds) if ds else None,
        "worst": min(ds) if ds else None,
        "last": hs[0] if hs else None,
    }
