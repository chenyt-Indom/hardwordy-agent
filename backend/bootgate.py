"""重启闸门：决定「这一次启动，到底要不要跑代码自检」。

用户要求（2026-11-16，原话）：
  · 「自动迭代更新重启不要进行自检」
  · 「其他情况重启才需要自检」
  · 「关闭到打开也不要自检，否则可能会无限自检下去」

为什么需要这个模块
------------------
原来 bootaudit 是「只要启动就跑」，于是：
  · 自动迭代改完代码 → 提示重启 → 用户点重启 → 又自检 → 若自检
    触发修复/再迭代 → 又要重启 → **又自检**，可以无限套娃；
  · 关掉程序第二天再打开，也算一次启动 → 又扫一遍 100 个文件。

判据只能落磁盘：`pending` 是内存状态，重启后进程换了一个，
内存里什么都没有。所以重启**前**把「这次为什么重启」写进
`data/boot_gate.json`，启动时读一次、读完立刻删掉。

判定规则（2026-11-16 用户第二次更正，以此为准）
----------------------------------------------
  ① 没有标记（或标记太旧）→ 冷启动（关闭后重新打开 / 崩溃后重开）
     → **不自检**
  ② force="audit" → 点了「新版本已就绪」弹窗里的「立即重启」
     → **自检**（这是唯一会自检的重启）
  ③ force="skip" → 抽屉「一键重启」/ 自动迭代横幅「一键重启」
     → **不自检**
  ④ 标记里 auto_iter=True → 自动迭代刚改完代码后的重启
     → **不自检**
  ⑤ 标记没写 force（老标记 / 漏写）→ **不自检**（宁可少扫一遍）

另外加一道熔断：短时间（5 分钟）内已经自检过 3 次就不再自检 ——
这是给「无限自检」兜底的最后一道闸，即使上面的判据哪天出了岔子，
也不会真的转圈。

重启入口语义表（唯一权威，前端 tooltip 也读这一份）
--------------------------------------------------
用户连续两轮纠正「哪个按钮触发自检」都判反了，所以把「按钮 → 接口 →
重启类别 → 是否自检」钉成一张显式表，代码和前端都从这张表取，
不再靠散落在各处的 if/else 记忆。

  · 按钮：前端界面上用户能点的那个东西（文案即 key）
  · 接口：前端点下去实际调用的后端接口
  · 类别：decide() 返回的 kind（写进 last_decision.kind）
  · 自检：这一次启动到底跑不跑代码自检

⚠️ 只有「新版本已就绪」弹窗里的「立即重启」是自检的，其余一律不自检。
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_PATH = os.path.join("data", "boot_gate.json")

# 标记的有效期：重启是秒级的事，超过这个时间说明中间断过（崩溃/关机），
# 当成冷启动处理，不拿一个几小时前的标记来指挥现在的启动。
MARK_TTL = 900.0          # 15 分钟

# 熔断窗口：窗口内自检超过 LIMIT 次就不再自检。
# ⚠️ 别调太小：用户正常「改一处 → 重启看一眼 → 再改一处 → 再重启」很常见，
#    10 分钟 2 次会把这种正常使用误判成死循环。5 分钟 3 次才像真循环。
LOOP_WINDOW = 300.0       # 5 分钟
LOOP_LIMIT = 3


def _blank() -> dict:
    return {"mark": None, "auto_change": None, "recent": [], "last_decision": None}


def _load() -> dict:
    d = _blank()
    try:
        if os.path.exists(_PATH):
            with open(_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k in d:
                    if k in raw:
                        d[k] = raw[k]
    except Exception:                                   # noqa: BLE001
        logger.debug("boot_gate 读取失败，按空处理", exc_info=True)
    return d


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
    except Exception:                                   # noqa: BLE001
        logger.debug("boot_gate 写入失败（不影响重启）", exc_info=True)


# ---------------------------------------------------------------- 写标记
def mark_auto_change(files=None, note: str = "") -> dict:
    """**自动迭代真的改了文件**时调用（iter_runner 里）。

    只记时间戳，不打 pending —— pending 是「等用户确认重启」的 UI 标记，
    两者语义不同，混用会让前端提示错乱。
    """
    d = _load()
    d["auto_change"] = {"ts": time.time(),
                        "at": time.strftime("%m-%d %H:%M:%S"),
                        "files": [str(x)[:120] for x in (files or [])][:12],
                        "note": str(note or "")[:160]}
    _save(d)
    return d["auto_change"]


def mark_restart(reason: str = "", force: str = "") -> dict:
    """重启**前**调用：写下「这次为什么重启」，并判断是不是自动迭代导致的。

    ``force``（2026-11-16 用户第二次更正：「只有「新版本已就绪」的「立即重启」
    才自检，剩下的不用自检」）：
      · ``"audit"`` —— 一定自检（唯一入口：「新版本已就绪」弹窗的「立即重启」）
      · ``"skip"``  —— 一定不自检（抽屉「一键重启」/ 自动迭代横幅「一键重启」）
      · ``""``      —— 没指定 → 按用户要求不自检

    返回落盘内容（方便日志）。任何异常都不往外抛 —— 闸门坏了也不能
    让重启这个主流程失败。
    """
    try:
        d = _load()
        ac = d.get("auto_change") or {}
        try:
            ac_age = time.time() - float(ac.get("ts") or 0)
        except Exception:                               # noqa: BLE001
            ac_age = 1e9
        # 30 分钟内自动迭代改过代码 → 这次重启算「自动迭代更新重启」
        auto_iter = bool(ac) and ac_age < 1800.0
        d["mark"] = {"ts": time.time(),
                     "at": time.strftime("%m-%d %H:%M:%S"),
                     "reason": str(reason or "")[:160],
                     "force": str(force or "")[:12],
                     "auto_iter": auto_iter,
                     "auto_files": list(ac.get("files") or [])[:12] if auto_iter else [],
                     "auto_age": round(ac_age, 1) if auto_iter else None}
        _save(d)
        return d["mark"]
    except Exception as e:                              # noqa: BLE001
        logger.warning("写重启闸门标记失败（不影响重启）：%s", e)
        return {}


# ---------------------------------------------------------------- 读判定
def decide() -> dict:
    """启动时调用一次：告诉 bootaudit 这次要不要跑自检。

    返回 ``{"audit": bool, "why": str, "kind": str}``：
      kind = cold（冷启动）/ auto_iter（自动迭代重启）/ manual（手动重启）/ loop（熔断）
    """
    d = _load()
    mk = d.get("mark") or {}
    now = time.time()

    # ---- 先清掉旧标记（无论结论如何，读一次就作废）
    try:
        d["mark"] = None
    except Exception:                                   # noqa: BLE001
        pass

    kind, why, audit = "", "", False

    if not mk:
        # ★ 2026-11-16 用户要求：「bootaudit 对无标记的冷启动默认不自检」。
        #   没有标记 = 没有任何重启入口写过 mark_restart = 冷启动（关闭后
        #   重新打开 / 崩溃后重开 / 某条重启路径漏写标记）。一律不自检。
        kind, why = "cold", "无重启标记（冷启动或重启入口未写标记），不跑代码自检"
    else:
        try:
            age = now - float(mk.get("ts") or 0)
        except Exception:                               # noqa: BLE001
            age = 1e9
        _force = str(mk.get("force") or "")
        if age > MARK_TTL:
            kind, why = "cold", ("上次重启标记已过期（%.0f 分钟前），按冷启动处理，不跑代码自检"
                                 % (age / 60.0))
        elif _force == "audit":
            # ★ 用户要求（2026-11-16 第二次更正，以这句为准）：
            #   「只有这个重启按下之后才要自检，剩下的不用自检」——
            #   「这个」= 「新版本已就绪」弹窗里的「立即重启」。
            #   ⚠️ 这里必须**先于** auto_iter 判断：force 是用户的显式意图，
            #   不能被「30 分钟内自动迭代改过代码」推翻（原来漏了这个分支，
            #   导致 force="audit" 会落进 auto_iter 被判成不自检）。
            #   熔断照旧兜底：短时间反复自检仍然停。
            rec = [float(x) for x in (d.get("recent") or []) if isinstance(x, (int, float))]
            rec = [x for x in rec if now - x < LOOP_WINDOW]
            if len(rec) >= LOOP_LIMIT:
                kind, why = "loop", ("%d 分钟内已自检 %d 次，为防止无限自检，本次跳过"
                                     % (int(LOOP_WINDOW / 60), len(rec)))
            else:
                kind, why, audit = "ready", "点了「新版本已就绪」的「立即重启」，跑一次代码自检", True
            d["recent"] = rec
        elif _force == "skip":
            # 用户要求（2026-11-16）：抽屉「一键重启」/ 自动迭代横幅「一键重启」
            # 都是「改完代码要生效」的顺手重启，一律不自检。
            kind, why = "quick", "代码改动后的「一键重启」（你要求这类重启不自检）"
        elif mk.get("auto_iter"):
            kind, why = "auto_iter", ("自动迭代改完代码后的重启（改了 %d 个文件），不跑代码自检"
                                      % len(mk.get("auto_files") or []))
        else:
            # 没写 force 的老标记（或哪条重启路径漏写了）→ 按用户要求「剩下的
            # 不用自检」处理。宁可少扫一遍，也不要在无人值守时转圈。
            kind, why = "other", "没有指定要自检的重启，按你的要求不自检"

    if audit:
        try:
            rec = [float(x) for x in (d.get("recent") or []) if isinstance(x, (int, float))]
            rec.append(now)
            d["recent"] = [x for x in rec if now - x < LOOP_WINDOW][-8:]
        except Exception:                               # noqa: BLE001
            d["recent"] = [now]

    d["last_decision"] = {"ts": now, "at": time.strftime("%m-%d %H:%M:%S"),
                          "kind": kind, "audit": bool(audit), "why": why}
    _save(d)
    logger.info("[bootgate] %s → audit=%s（%s）", kind, audit, why)
    return {"audit": bool(audit), "why": why, "kind": kind}


def status() -> dict:
    """给接口/前端看：上次判定结果 + 当前标记。"""
    d = _load()
    return {"last": d.get("last_decision"),
            "mark": d.get("mark"),
            "auto_change": d.get("auto_change"),
            "ttl": MARK_TTL, "loop_window": LOOP_WINDOW, "loop_limit": LOOP_LIMIT}
