# -*- coding: utf-8 -*-
"""RSI 一键体检 —— **账本值 vs 实时重算值并列**，不一致就标黄。

★ 用户反复问的两句话（这条功能就是为它们做的）：
    「RSI 是不是完全正常？」
    「曲线图是不是没更新？我怎么看着不动 / 对不上？」

为什么需要它（真实痛点，不是猜的）
----------------------------------
现在看板上只显示**一个**数：`/api/selfiter/rsi` 里的 `index.score`（当场重算）
和曲线上的点（上次采样落盘的账本值）是**两个不同的东西**，但界面上它们长得
一模一样，用户无法判断「曲线最后那个点是不是当前代码的真实值」。
每次问起来都只能临时跑一次 run_python 去对比，既慢又不可复现。

所以这里把两个值**并排算出来、并排给出去**，并显式判定一致 / 不一致：
  · ledger_last —— 账本值：`rsi_history.jsonl` 里最后一条采样（= 曲线图那个点）
  · live        —— 实时重算值：`lessonloop.rsi_index(cache_sec=0)`（= 现在这套代码）
只要 |live - ledger| > 阈值（默认 2 分），就 consistent=False，前端卡片标黄
并提示「账本落后，建议重算」。

理论依据（借鉴，不是硬套）
--------------------------
三级验证协议（Toward Reproducible Cross-Backend Compatibility for Deep Learning
中的 Three-Tier Verification）把一致性拆成三层：张量级接近度 → 激活对齐 →
任务级指标。这里同构地拆成三层，任何一层对不上都能指名道姓：
  ① 总分接近度    |live.score - ledger.score| <= tol        —— 任务级指标
  ② 分项接近度    每个分项（动作成功率 / 落地率 / 静态质量…）各自比对 —— 张量级
  ③ 新鲜度对齐    账本最后一点的时间距现在多久（stale）      —— 激活/时间对齐
① 决定 consistent；② 作为 part_drift 明细**如实报出**（不参与判定，避免权重变更
导致的假黄）；③ 单独给 stale，提示「曲线还没跟上这一轮」。

设计约束（和 iterhealth.py 同一套口径）
---------------------------------------
  · **只读**：绝不采样、绝不写 rsi_history.jsonl、绝不动账本（体检不能改变被体检的对象）；
  · 每一项独立 try：某一侧算不出来也要给出另一侧 + 原因，不能整个接口 500；
  · 不编数字：拿不到就是 None，前端显示「—」，绝不补 0（补 0 会被误读成掉分）。
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 阈值：低于它就认为「账本值与实时重算值一致」。
# 2 分是刻意选的口径 —— rsi_index 里动作成功率权重 0.40，多算 / 少算一条动作
# 大约动 0.5~1.5 分（实测账本里相邻两点常见跳动），把阈值压到 1 分会天天误报黄。
DEFAULT_TOL = 2.0
# 账本点超过 30 分钟没更新就提示 stale（迭代一轮通常几分钟，30 分钟足够宽松）。
DEFAULT_MAX_AGE_SEC = 1800.0
# 分项比对阈值：只用于明细里的 drift 标记，不参与 consistent 判定
# （权重/口径调整会让分项变而总分不变，那不该判成「账本坏了」）。
PART_TOL = 0.02


def _num(x):
    """转成 2 位小数的 float；拿不到 / 非法 / NaN 一律 None（绝不补 0）。"""
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return round(v, 2)
    except Exception:                                       # noqa: BLE001
        return None


def _iso(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except Exception:                                       # noqa: BLE001
        return ""


def _read_rows(path):
    """读 rsi_history.jsonl。坏行跳过（历史里确实有半行 / 调试残留）。"""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                           # noqa: BLE001
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except FileNotFoundError:
        return []
    except Exception:                                       # noqa: BLE001
        logger.debug("读 rsi_history 失败", exc_info=True)
    return rows


def _ledger_side(rows, now, max_age_sec):
    """账本值 = 曲线图上最后那个点（最后一条采样记录）。"""
    last = rows[-1] if rows else None
    if not last:
        return {"exists": False, "ts": "", "t": None, "score": None,
                "speed": None, "src": "", "parts": {}, "age_sec": None,
                "samples": 0, "stale": True,
                "note": "账本还没有采样点（曲线是空的）"}
    t = _num(last.get("ts"))
    age = round(now - float(t), 1) if t is not None else None
    raw_parts = last.get("parts")
    parts = {}
    if isinstance(raw_parts, dict):
        for k, v in raw_parts.items():
            nv = _num(v)
            if nv is not None:
                parts[str(k)] = nv
    return {"exists": True,
            "ts": str(last.get("iso") or _iso(t) or ""),
            "t": t,
            "score": _num(last.get("score")),
            "speed": _num(last.get("speed")),
            "src": str(last.get("src") or ""),
            "parts": parts,
            "age_sec": age,
            "samples": len(rows),
            # age 算不出来也按 stale 处理：时间不明 = 不敢说它新鲜
            "stale": bool(age is None or age > float(max_age_sec)),
            "note": ""}


def _live_side(now):
    """实时重算值 = 现在这套代码当场算出来的 rsi_index（cache_sec=0，不吃缓存）。"""
    from . import lessonloop as LL
    li = LL.rsi_index(cache_sec=0) or {}
    parts = {}
    for p in (li.get("parts") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        v = _num(p.get("value"))
        if name is not None and v is not None:
            parts[str(name)] = v
    speed = None
    try:
        speed = _num((LL.rsi_speed() or {}).get("score"))
    except Exception:                                       # noqa: BLE001
        logger.debug("实时加速指数算不出来（不影响主判定）", exc_info=True)
    return {"ts": _iso(now), "t": now,
            "score": _num(li.get("score")),
            "speed": speed,
            "src": "live",
            "parts": parts,
            "window": str(li.get("window") or ""),
            "coverage": _num(li.get("coverage")),
            "insufficient": [str(x) for x in (li.get("insufficient") or [])],
            "note": ""}


def _part_rows(ledger_parts, live_parts, tol=PART_TOL):
    """第 ② 层：逐分项比对（只报明细，不参与 consistent 判定）。"""
    out = []
    for name in sorted(set(ledger_parts) | set(live_parts)):
        a = ledger_parts.get(name)
        b = live_parts.get(name)
        diff = None
        if a is not None and b is not None:
            diff = round(float(b) - float(a), 4)
        out.append({"name": name, "ledger": a, "live": b, "diff": diff,
                    "drift": bool(diff is not None and abs(diff) > float(tol))})
    return out


def check(tol=None, max_age_sec=None, now=None):
    """算一次体检。返回 dict（**永不抛**给调用方，异常包成 ok=False）。"""
    now = float(now if now is not None else time.time())
    try:
        _tol = float(tol) if tol is not None else DEFAULT_TOL
    except Exception:                                       # noqa: BLE001
        _tol = DEFAULT_TOL
    _tol = max(0.0, _tol)
    try:
        _max_age = float(max_age_sec) if max_age_sec is not None else DEFAULT_MAX_AGE_SEC
    except Exception:                                       # noqa: BLE001
        _max_age = DEFAULT_MAX_AGE_SEC

    led = {"exists": False, "ts": "", "t": None, "score": None, "speed": None,
           "src": "", "parts": {}, "age_sec": None, "samples": 0, "stale": True,
           "note": ""}
    live = {"ts": _iso(now), "t": now, "score": None, "speed": None, "src": "live",
            "parts": {}, "window": "", "coverage": None, "insufficient": [],
            "note": ""}
    errors = {}

    try:
        from . import lessonloop as LL
        rows = _read_rows(LL._rsi_hist_path())
        led = _ledger_side(rows, now, _max_age)
    except Exception as e:                                  # noqa: BLE001
        errors["ledger"] = "%s: %s" % (type(e).__name__, e)
        led["note"] = "账本读不出来：%s" % errors["ledger"]
    try:
        live = _live_side(now)
    except Exception as e:                                  # noqa: BLE001
        errors["live"] = "%s: %s" % (type(e).__name__, e)
        live["note"] = "实时重算失败：%s" % errors["live"]

    parts = _part_rows(led.get("parts") or {}, live.get("parts") or {})
    diff = None
    if led.get("score") is not None and live.get("score") is not None:
        diff = round(float(live["score"]) - float(led["score"]), 2)
    speed_diff = None
    if led.get("speed") is not None and live.get("speed") is not None:
        speed_diff = round(float(live["speed"]) - float(led["speed"]), 2)

    # ---- 判定（保守：只要有一侧拿不到，就**不能**说「一致」）----
    reasons = []
    if not led.get("exists"):
        consistent = False
        reasons.append("账本还没有采样点：曲线图是空的，跑一轮迭代收尾就会写入")
    elif led.get("score") is None or live.get("score") is None:
        consistent = False
        reasons.append("有一侧分数拿不到（账本 %s / 实时 %s），无法确认一致"
                       % (led.get("score"), live.get("score")))
    elif diff is None:
        consistent = False
        reasons.append("差值算不出来，无法确认一致")
    elif abs(diff) > _tol:
        consistent = False
        reasons.append("账本 %.1f 分（%s）vs 实时重算 %.1f 分，差 %+.2f 分，超过阈值 %.1f"
                       % (float(led["score"]), led.get("ts") or "时间未知",
                          float(live["score"]), float(diff), _tol))
    else:
        consistent = True

    drift = [p["name"] for p in parts if p.get("drift")]
    metrics = {
        "tol": _tol,
        "max_age_sec": _max_age,
        "ledger_score": led.get("score"),
        "live_score": live.get("score"),
        "diff": diff,
        "abs_diff": (abs(diff) if diff is not None else None),
        "speed_diff": speed_diff,
        "samples": led.get("samples") or 0,
        "ledger_age_sec": led.get("age_sec"),
        "stale": bool(led.get("stale")),
        "parts": parts,
        "part_drift": drift,
        "window": live.get("window") or "",
        "coverage": live.get("coverage"),
        "src": led.get("src") or "",
    }

    if consistent:
        verdict = "一致"
        hint = ("账本值与实时重算值相差 %+.2f 分（阈值 %.1f）—— 曲线图上那个数"
                "就是当前代码的真实值，不用重算。" % (float(diff or 0.0), _tol))
    elif not led.get("exists"):
        verdict = "账本空"
        hint = ("曲线图还没有任何采样点。跑一轮自迭代（收尾会自动补一个点）"
                "之后再看就有可比对象了。")
    else:
        verdict = "账本落后"
        hint = ("账本落后，建议重算：曲线最后一点是 %s（%s前），当前代码重算是 %.1f 分。"
                % (led.get("ts") or "时间未知",
                   _ago(led.get("age_sec")), float(live.get("score") or 0.0)))
    if consistent and metrics["stale"]:
        hint += " ⚠️ 不过账本最后一点已经是 %s前，曲线可能还没跟上这一轮。" \
                % _ago(led.get("age_sec"))
    if drift:
        hint += " （分项有 %d 项与账本不同：%s）" % (len(drift), "、".join(drift[:4]))

    return {"ok": not errors or (led.get("exists") and live.get("score") is not None),
            "ts": _iso(now),
            "t": now,
            "ledger_last": led,
            "live": live,
            "metrics": metrics,
            "consistent": bool(consistent),
            "can_compare": bool(led.get("score") is not None
                                and live.get("score") is not None),
            "reasons": reasons,
            "verdict": verdict,
            "hint": hint,
            "errors": errors}


def _ago(sec):
    """秒 → 人话（前端也有一份，但后端的 hint 自己也要能读）。"""
    try:
        s = float(sec)
    except Exception:                                       # noqa: BLE001
        return "时间未知"
    if s < 0:
        return "刚刚"
    if s < 90:
        return "%.0f 秒" % s
    if s < 5400:
        return "%.0f 分钟" % (s / 60.0)
    if s < 172800:
        return "%.1f 小时" % (s / 3600.0)
    return "%.1f 天" % (s / 86400.0)


def health_line(tol=None, max_age_sec=None):
    """一行结论（给 hint / 卡片标题用）。只读。"""
    r = check(tol=tol, max_age_sec=max_age_sec)
    s = "RSI 体检：%s" % r.get("verdict")
    ls = (r.get("ledger_last") or {}).get("score")
    lv = (r.get("live") or {}).get("score")
    if ls is not None and lv is not None:
        s += "（账本 %s / 实时 %s）" % (ls, lv)
    return s
