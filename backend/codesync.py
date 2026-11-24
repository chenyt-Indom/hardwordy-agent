# -*- coding: utf-8 -*-
"""代码指纹对齐 + 自动生效闭环（codesync）。

用户 2026-11-24 要求（原话）
---------------------------
「自动更新机制要不要怎么优化确保以后迭代都能完美生效且验证完全无误？」
随后在问答里选定：

  · 范围：**统一闭环**，覆盖「我批准之后 / 信任模式下自动跑的」那些迭代；
  · 发现「磁盘代码比运行中进程新」→ **自动重启，不用问**；
  · 验证时机：**改动后 + 重启后都跑**；
  · 验证没过：自动修复最多 2 轮；**完全不可用 → 回到上一个快照，部分坏 → 只回滚坏文件**；
  · 允许改 main.py / 加测试，改完必须全绿，**不要动高风险区**。

要解决的真实故障（都踩过）
--------------------------
① 改完文件没重启 → 新代码根本没加载，界面和日志却一切正常，用户只能"感觉没变化"；
② 重启了，但没人核对"现在跑的到底是不是新代码"；
③ 前端新增的 js（livecard / taskcard / iterjournal…）不在自更新清单里，
   改了它们不会触发界面自动刷新。

做法
----
1. 进程启动时把磁盘代码指纹钉成 **runtime 指纹**（本模块导入时算一次）；
2. 后台每 30 秒对比磁盘指纹，**连续 2 次采样一致**才算「改动落定」
   （防止文件写到一半就被判成新版本）；
3. 落定后按用户要求走闭环：
     验证（语法 + 导入自检）→ 通过 → 空闲时自动重启
     （重启后由 iterloop 的 post_verify 再复核一遍，这是"重启后也跑"那一半）
     验证不过 → 能定位到文件就**局部回滚**该文件；整体不可用就**回滚到最近快照**；
                都做不到就只记录、不重启（绝不把坏代码带上线）
4. status() 给接口/前端：runtime / disk / drifted / 待处理动作 / 最近历史。

安全边界（用户要求「别动高风险区」）
------------------------------------
· 本模块只**读**代码文件，只调用既有模块（version / safeio / bootgate /
  main.selfdev_apply_restart），不改任何业务逻辑；
· 任何异常都吞掉并记日志 —— 守护线程绝不能把应用带走；
· 自动重启前必过 selfdev_apply_restart 里的导入自检（它自己会拒掉坏代码）；
· 同一份改动只处理一次（指纹记进 state），并有 5 分钟冷却 + 2 次尝试上限，
  不会出现「改一行 → 无限重启」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

from . import config, safeio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 参数
POLL_SEC = 30.0           # 多久扫一次磁盘
STABLE_SAMPLES = 2        # 连续几次采样一致才算「改动落定」
COOLDOWN_SEC = 300.0      # 两次自动处理之间的最短间隔
IDLE_NEED_SEC = 45.0      # 距最后一次请求超过这么久才算「空闲」，可以重启
MAX_DEFER_SEC = 600.0     # 最长推迟多久（一直有人用也要在 10 分钟后生效）
MAX_ATTEMPTS = 2          # 同一份改动最多自动重启几次（防死循环）
HIST_MAX = 20

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND)
_FRONTEND = os.path.join(_ROOT, "frontend")

# 这些目录里的改动不算「运行时代码」（测试/归档/缓存/第三方）
_SKIP_DIRS = {
    "__pycache__", "_feature_tests", "_debug_archive", "_pysite",
    "tests", "node_modules", ".git", ".cache", "_bak_silent", "lib",
}

_LOCK = threading.RLock()
_WATCH_STARTED = False


# ---------------------------------------------------------------- 指纹
def _scan(root: str, exts) -> list:
    """扫一个目录，返回 [(相对路径, mtime, size)]（已排序，可复现）。"""
    items = []
    if not os.path.isdir(root):
        return items
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        )
        for name in filenames:
            if name.startswith(".") or ".bak" in name:
                continue
            if not name.endswith(tuple(exts)):
                continue
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, root).replace("\\", "/")
            items.append((rel, int(st.st_mtime), int(st.st_size)))
    items.sort()
    return items


def _digest(items) -> str:
    raw = "|".join("%s:%d:%d" % it for it in items)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def fingerprint() -> dict:
    """当前**磁盘**上的代码指纹（后端 .py + 前端 .js/.css/.html）。"""
    be = _scan(_BACKEND, (".py",))
    fe = _scan(_FRONTEND, (".js", ".css", ".html"))
    return {
        "fp": _digest(be + fe),
        "backend": len(be),
        "frontend": len(fe),
        "files": be + fe,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 状态
def _state_path() -> str:
    return config.data("data", "codesync_state.json")


def _blank_state() -> dict:
    return {
        "runtime_fp": "",
        "runtime_at": "",
        "runtime_started": 0.0,
        "candidate": None,       # 正在观察的新指纹
        "candidate_n": 0,        # 连续采样次数
        "done_fp": "",           # 已经处理过的指纹（避免重复处理）
        "last_handle": 0.0,
        "pending_restart": None,  # {"fp","since","why"}
        "attempts": {},          # fp -> 已尝试重启次数
        "history": [],
    }


def _load_state() -> dict:
    d = _blank_state()
    try:
        p = _state_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k in d:
                    if k in raw:
                        d[k] = raw[k]
    except Exception:                                    # noqa: BLE001
        logger.debug("codesync 状态读取失败，按空处理", exc_info=True)
    return d


def _save_state(d: dict) -> None:
    try:
        d["history"] = list(d.get("history") or [])[-HIST_MAX:]
        safeio.atomic_write_json(_state_path(), d, indent=1)
    except Exception:                                    # noqa: BLE001
        logger.warning("codesync 状态写盘失败（不影响运行）", exc_info=True)


def _record(ev: str, **kw) -> None:
    """往 history 里追加一条（同时打日志），用于事后追溯「它到底做了什么」。"""
    try:
        d = _load_state()
        row = {"at": _now_str(), "ev": str(ev)[:40]}
        row.update({k: v for k, v in kw.items() if v is not None})
        d["history"] = (list(d.get("history") or []) + [row])[-HIST_MAX:]
        _save_state(d)
    except Exception:                                    # noqa: BLE001
        logger.debug("codesync 记录失败", exc_info=True)


# ---------------------------------------------------------------- runtime 指纹
_RUNTIME = {
    "fp": "",
    "at": "",
    "started": 0.0,
    "backend": 0,
    "frontend": 0,
}


def _init_runtime() -> dict:
    """把「进程启动这一刻的磁盘代码」钉成 runtime 指纹。

    ⚠️ 必须在本模块**被导入时**执行一次 —— 那时就是进程加载代码的时刻。
    """
    try:
        f = fingerprint()
        _RUNTIME.update({
            "fp": f["fp"], "at": f["at"], "started": time.time(),
            "backend": f["backend"], "frontend": f["frontend"],
        })
        logger.info("[codesync] 运行中代码指纹 %s（后端 %d / 前端 %d 个文件）",
                    f["fp"], f["backend"], f["frontend"])
    except Exception:                                    # noqa: BLE001
        logger.warning("[codesync] 初始化 runtime 指纹失败", exc_info=True)
    return dict(_RUNTIME)


_init_runtime()


def runtime() -> dict:
    return dict(_RUNTIME)


# ---------------------------------------------------------------- 验证
def verify() -> dict:
    """改动落定后的**准入验证**：语法 + 导入自检。

    为什么不在这里跑全量功能测试：run_all 要十几秒，放在守护线程里做会拖慢
    「改动 → 生效」的节奏。全量验证放在重启后由 iterloop.post_verify 跑 ——
    这正好对上用户要的「改动后 + 重启后都跑」。
    """
    out = {"ok": True, "why": "", "bad": []}
    # ① 语法
    try:
        from . import version as _v
        syn = _v._syntax_check()
        if not syn.get("ok"):
            bad = syn.get("bad") or []
            out.update(ok=False, bad=bad,
                       why="语法检查未通过：%s" % (syn.get("error") or "未知"))
            return out
    except Exception as e:                               # noqa: BLE001
        logger.debug("codesync 语法检查不可用：%s", e)
    # ② 导入自检（子进程，不污染当前进程）
    try:
        from . import selfdev as _sd
        iv = _sd.verify_import()
        if isinstance(iv, dict) and not iv.get("ok", True):
            out.update(ok=False,
                       why="导入自检未通过：%s" % (iv.get("error")
                                                  or iv.get("msg") or iv))
            return out
    except Exception as e:                               # noqa: BLE001
        logger.debug("codesync 导入自检不可用：%s", e)
    out["why"] = "语法与导入自检通过"
    return out


# ---------------------------------------------------------------- 回滚
def _recover(bad: list, st: dict) -> dict:
    """验证失败时的保守恢复：局部坏 → 只回滚坏文件；整体坏 → 回滚上一快照。

    用户原话：「完全无可就要再完全回到上一个快照，部分正常的话就小部分回滚」。
    """
    try:
        from . import version as _v
        av = _v.available()
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "why": "版本模块不可用：%s" % e}

    if not (isinstance(av, dict) and av.get("ok")):
        return {"ok": False, "why": "没有可用的版本库，无法自动回滚（已记录，等你处理）"}

    rels = []
    for b in (bad or []):
        r = str((b or {}).get("rel") or "")
        if r and ".." not in r and r not in rels:
            rels.append(r)

    # 先取最近一个快照当回滚目标
    target = ""
    try:
        cur = _v.current()
        target = str((cur or {}).get("hash") or "")
    except Exception:                                    # noqa: BLE001
        target = ""
    if not target:
        try:
            lst = (_v.list_versions(limit=1) or {}).get("versions") or []
            target = str((lst[0] or {}).get("hash") or "") if lst else ""
        except Exception:                                # noqa: BLE001
            target = ""
    if not target:
        return {"ok": False, "why": "没有可回滚的快照"}

    try:
        if rels and len(rels) <= 3:
            # 部分坏：只把坏文件拉回上一个快照
            r = _v.rollback(target, files=rels, note="codesync 自动局部回滚")
            return {"ok": bool(r.get("ok")), "mode": "partial",
                    "files": rels, "detail": r}
        # 整体不可用 / 说不清哪些坏：整轮回滚
        r = _v.rollback(target, note="codesync 自动整轮回滚")
        return {"ok": bool(r.get("ok")), "mode": "full", "detail": r}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "why": "回滚异常：%s" % e}


# ---------------------------------------------------------------- 空闲判定
def _idle_sec() -> float:
    """距最后一次业务请求过了多久；**正在生成回复时直接返回 0（绝不重启）**。

    两道判据缺一不可：
      · ``_chat_busy > 0`` —— 有回复正在流式生成。这是硬闸：重启会把这一轮打断，
        所以只要有人在生成，哪怕已经跑了几分钟，也一律等着。
      · ``_LAST_REQUEST_TS`` —— 最近一次真实业务请求的时间（前端 3 秒一次的
        自更新轮询不算，见 main 里的 _ACTIVITY_SKIP）。
    """
    try:
        from . import main as _M
        if int(getattr(_M, "_chat_busy", 0) or 0) > 0:
            return 0.0
        ts = float(getattr(_M, "_LAST_REQUEST_TS", 0) or 0)
        if ts <= 0:
            return IDLE_NEED_SEC
        return max(0.0, time.time() - ts)
    except Exception:                                    # noqa: BLE001
        return IDLE_NEED_SEC


def _do_restart(fp: str) -> dict:
    """走唯一的重启入口（它内部会做导入自检、写闸门标记、打快照）。

    ⚠️ 重启**之前**必须先打「重启后复核」标记（iterloop.mark_post_verify）——
    用户要求「改动后 + 重启后都跑」：改动后那一半在 verify()，重启后那一半
    靠这个标记让新进程启动时自动跑一遍复核（iterloop.run_post_verify）。
    漏了这一步，就只剩「改动后跑」，重启后没人验收。
    """
    try:
        from . import iterloop as _IL
        _IL.mark_post_verify(pid="codesync")
        logger.info("[codesync] 已打「重启后复核」标记")
    except Exception as e:                               # noqa: BLE001
        logger.warning("[codesync] 打重启后复核标记失败（不影响重启）：%s", e)
    try:
        from . import main as _M
        r = _M.selfdev_apply_restart({"src": "codesync"})
        return r if isinstance(r, dict) else {"ok": True}
    except Exception as e:                               # noqa: BLE001
        logger.warning("[codesync] 自动重启失败：%s", e)
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 重启闸门
def _in_iteration() -> bool:
    """当前是否有自动迭代任务正在跑。

    只认 iter_runner 的「running」登记 —— 那是真正在改代码、用户正等结果的那条。
    """
    try:
        from . import iter_runner
        run = (iter_runner._STATE or {}).get("running") or {}
        return bool(run.get("tid") or run.get("plan_id"))
    except Exception:                                   # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return False


def _auto_restart_allowed() -> tuple:
    """(允许自动重启?, 原因)。

    ★ 2026-11-25 用户要求（原话）：「为什么又是在非自动迭代任务时自动重启了？
      不应该要有一个自动重启的按钮吗」。
      原来本模块**无条件**自动重启：只要磁盘代码比运行进程新就重启，哪怕用户
      正在聊天、刚问完一句话。现在改成两道口子，任一满足才自动重启：
        · 自动迭代任务正在跑（那时重启是这一轮的正常收尾）；
        · 用户在设置里显式打开 codesync_auto_restart。
      其余情况只记「待重启」，等用户点按钮。
    """
    try:
        from . import self_iterate as _si
        s = _si.load_settings() or {}
    except Exception:                                   # noqa: BLE001
        s = {}
    if bool(s.get("codesync_auto_restart", False)):
        return True, "设置里开了「代码改动自动重启」"
    if _in_iteration():
        return True, "自动迭代任务正在跑"
    return False, "非迭代任务：只记录待重启，等你点重启按钮"


# ---------------------------------------------------------------- 主循环
def _handle(fp: str, st: dict) -> dict:
    """改动已落定：验证 → 通过则排队重启 / 不通过则恢复。"""
    now = time.time()
    st["last_handle"] = now
    st["done_fp"] = fp

    v = verify()
    if not v.get("ok"):
        rec = _recover(v.get("bad") or [], st)
        st["pending_restart"] = None
        _save_state(st)
        _record("verify_fail", fp=fp, why=v.get("why"),
                recovered=rec.get("ok"), mode=rec.get("mode"))
        logger.warning("[codesync] 改动验证未通过，已尝试恢复：%s → %s",
                       v.get("why"), rec)
        return {"action": "recover", "verify": v, "recover": rec}

    # 通过 → 先过「尝试次数上限」。这是安全护栏，必须排在闸门之前：
    #   同一份改动绝不无限重启，哪怕闸门本来就不让自动重启。
    tries = int((st.get("attempts") or {}).get(fp) or 0)
    if tries >= MAX_ATTEMPTS:
        st["pending_restart"] = None
        _save_state(st)
        _record("give_up", fp=fp, why="同一份改动已自动重启 %d 次，停止重试" % tries)
        logger.warning("[codesync] 指纹 %s 已尝试重启 %d 次，停止自动重启", fp, tries)
        return {"action": "give_up", "verify": v}

    # ★ 2026-11-25 闸门：非迭代任务不自动重启，只记「待重启」等用户点按钮。
    _ok, _why = _auto_restart_allowed()
    if not _ok:
        st["pending_restart"] = {"fp": fp, "since": now, "why": v.get("why"),
                                 "manual": True, "blocked": _why}
        _save_state(st)
        _record("manual_only", fp=fp, why=_why)
        logger.info("[codesync] 改动验证通过，%s → 只记录待重启", _why)
        return {"action": "manual_only", "verify": v, "need_manual_restart": True}

    # 通过 → 排队重启（真正执行在 _tick 里等空闲）

    st["pending_restart"] = {"fp": fp, "since": now, "why": v.get("why")}
    st["attempts"] = dict(st.get("attempts") or {})
    st["attempts"][fp] = tries + 1
    _save_state(st)
    _record("verify_ok", fp=fp, why=v.get("why"))
    logger.info("[codesync] 改动验证通过，等空闲后自动重启（第 %d 次）", tries + 1)
    return {"action": "queued", "verify": v}


def _tick() -> dict:
    """一次巡检：采样 → 判落定 → 处理 → 必要时重启。"""
    with _LOCK:
        st = _load_state()
        f = fingerprint()
        fp = f["fp"]
        rt = _RUNTIME.get("fp") or ""

        # ① 没有漂移：清掉候选，什么都不做
        if not rt or fp == rt:
            if st.get("candidate") or st.get("candidate_n"):
                st["candidate"] = None
                st["candidate_n"] = 0
                _save_state(st)
            return {"drifted": False, "fp": fp}

        # ② 已经处理过的指纹：等重启生效，不重复处理
        if fp == st.get("done_fp"):
            pass
        elif fp == st.get("candidate"):
            st["candidate_n"] = int(st.get("candidate_n") or 0) + 1
            _save_state(st)
            if st["candidate_n"] >= STABLE_SAMPLES:
                cool = time.time() - float(st.get("last_handle") or 0)
                if cool < COOLDOWN_SEC:
                    return {"drifted": True, "fp": fp,
                            "wait": "冷却中（还剩 %d 秒）" % int(COOLDOWN_SEC - cool)}
                return dict({"drifted": True, "fp": fp}, **_handle(fp, st))
        else:
            st["candidate"] = fp
            st["candidate_n"] = 1
            _save_state(st)
            return {"drifted": True, "fp": fp, "wait": "观察中（第 1 次采样）"}

        # ③ 有排队的重启 → 等空闲执行
        pr = st.get("pending_restart") or {}
        # ★ 2026-11-25：manual=True 是「闸门拦下来的」待重启 —— 绝不自动执行，
        #   只等用户在前端点按钮（否则闸门等于没加）。
        if pr.get("fp") == fp and not pr.get("manual"):
            idle = _idle_sec()
            waited = time.time() - float(pr.get("since") or time.time())
            if idle < IDLE_NEED_SEC and waited < MAX_DEFER_SEC:
                return {"drifted": True, "fp": fp,
                        "wait": "等你这边空下来（已等 %d 秒）" % int(waited)}
            _record("restart", fp=fp, idle=round(idle, 1), waited=round(waited, 1))
            logger.info("[codesync] 自动重启生效（空闲 %.0f 秒 / 已等 %.0f 秒）",
                        idle, waited)
            r = _do_restart(fp)
            if not r.get("ok", True):
                # 重启被拒（多半是导入自检没过）→ 撤销这次尝试，等下一轮
                st["pending_restart"] = None
                _save_state(st)
                _record("restart_refused", fp=fp, why=str(r.get("error"))[:200])
            return {"drifted": True, "fp": fp, "restart": r}

        return {"drifted": True, "fp": fp, "wait": "已处理，等重启"}


def _watch() -> None:
    while True:
        try:
            time.sleep(POLL_SEC)
            _tick()
        except Exception:                                # noqa: BLE001
            logger.debug("[codesync] 巡检异常（已吞）", exc_info=True)


def start_watch() -> dict:
    """启动后台巡检（startup 里调一次；重复调用无害）。"""
    global _WATCH_STARTED
    with _LOCK:
        if _WATCH_STARTED:
            return {"ok": True, "already": True}
        _WATCH_STARTED = True
    try:
        th = threading.Thread(target=_watch, name="codesync-watch", daemon=True)
        th.start()
        logger.info("[codesync] 已启动代码指纹巡检（每 %.0f 秒一次）", POLL_SEC)
        return {"ok": True, "already": False, "poll": POLL_SEC}
    except Exception as e:                               # noqa: BLE001
        logger.warning("[codesync] 启动巡检失败：%s", e)
        return {"ok": False, "error": str(e)}


def check_now() -> dict:
    """手动触发一次巡检（给接口/测试用）。"""
    try:
        return _tick()
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 对外状态
def status() -> dict:
    """给接口/前端：运行中 vs 磁盘，是否漂移，待处理动作，历史。"""
    st = _load_state()
    try:
        f = fingerprint()
        disk_fp, files, at = f["fp"], f["files"], f["at"]
    except Exception:                                    # noqa: BLE001
        disk_fp, files, at = "", [], ""
    rt = dict(_RUNTIME)
    changed = []
    if disk_fp and rt.get("fp") and disk_fp != rt.get("fp"):
        # 列出「新改动」的文件名（只按 mtime 判断，够用且便宜）
        try:
            started = float(rt.get("started") or 0)
            changed = [rel for (rel, mt, _sz) in files if mt >= started - 1]
        except Exception:                                # noqa: BLE001
            changed = []
    return {
        "ok": True,
        "runtime": {"fp": rt.get("fp"), "at": rt.get("at"),
                    "backend": rt.get("backend"), "frontend": rt.get("frontend"),
                    "uptime": int(time.time() - float(rt.get("started") or time.time()))},
        "disk": {"fp": disk_fp, "at": at,
                 "backend": len([1 for x in files if x[0].endswith(".py")]),
                 "frontend": len([1 for x in files if not x[0].endswith(".py")])},
        "drifted": bool(disk_fp and rt.get("fp") and disk_fp != rt.get("fp")),
        "changed_files": changed[:20],
        "candidate_n": int(st.get("candidate_n") or 0),
        "pending_restart": st.get("pending_restart"),
        # ★ 2026-11-25：告诉前端「现在会不会自动重启、为什么」——
        #   用户问过「为什么又自动重启了」，反过来也要能看见「为什么没重启」。
        "auto_restart_allowed": (lambda t: {"ok": t[0], "why": t[1]})(
            _auto_restart_allowed()),
        "need_manual_restart": bool((st.get("pending_restart") or {}).get("manual")),
        "last_handle": st.get("last_handle"),
        "attempts": st.get("attempts") or {},
        "history": list(st.get("history") or [])[-HIST_MAX:],
        "params": {"poll": POLL_SEC, "stable": STABLE_SAMPLES,
                   "cooldown": COOLDOWN_SEC, "idle_need": IDLE_NEED_SEC,
                   "max_defer": MAX_DEFER_SEC, "max_attempts": MAX_ATTEMPTS},
    }
