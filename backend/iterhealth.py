# -*- coding: utf-8 -*-
"""自迭代体系健康度体检 —— 一次看清 7 个子系统是不是真的在协同运作。

★ 2026-11-22 用户原话：
「你自己给我想一想新功能，不需要太复杂，然后给我验证整套机制是否完全正常
 运行且体系化协同运作？」

为什么需要它（这是踩过的真实坑）：
  记忆层、跨模型传承、失败归因、语义检索、累计评分、回滚重规划、重启复核
  —— 这 7 块各自都能跑通自测，但**没有一个地方能回答「它们合起来到底在不在
  转」**。实测遇到过两次：接线全绿、日志一片正常，可真实环境里归因表是 0 字节、
  累计分账本压根不存在（建表之后一轮迭代都没跑过）。只读代码是看不出来的。

所以这里用三态，不用布尔：
  ok      有数据、指标正常
  degrade 已接线但指标不达标（向量降级 / 原因写得太笼统 / 累计分在跌）
  idle    已接线但真实环境还没跑过 —— 不是坏，是空，需要先跑一轮

设计约束：
  · 每一项独立 try，一个子系统炸了不能连累其他项；
  · **只读**，绝不触发评分/回归/反思（那些要几十秒，体检必须秒回）；
  · 不编数字：拿不到就标 degrade 并写清原因。
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

OK = "ok"
DEGRADE = "degrade"
IDLE = "idle"

_STATE_ZH = {OK: "正常", DEGRADE: "需关注", IDLE: "待运行"}
_STATE_ICON = {OK: "✅", DEGRADE: "⚠️", IDLE: "⏸"}


def _item(key, name, state, value="", evidence="", hint=""):
    """一个子系统的一条体检结果。"""
    return {
        "key": key,
        "name": name,
        "state": state,
        "state_zh": _STATE_ZH.get(state, state),
        "icon": _STATE_ICON.get(state, "·"),
        "value": str(value or ""),
        "evidence": str(evidence or ""),
        "hint": str(hint or ""),
    }


# --------------------------------------------------------------- ① 长期记忆
def _c_memory():
    try:
        from . import memory as _M
        txt = str(_M.get_long() or "")
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if not lines:
            return _item("memory", "长期记忆", IDLE, "0 行",
                         "长期记忆还是空的",
                         "正常聊几轮后会写入；这不是故障")
        return _item("memory", "长期记忆", OK,
                     "%d 行 / %d 字" % (len(lines), len(txt)),
                     "已落盘，可供跨会话继承")
    except Exception as e:                                  # noqa: BLE001
        return _item("memory", "长期记忆", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 memory.get_long()")


# --------------------------------------------------- ② 跨模型传承（记忆迁移）
def _c_transfer():
    try:
        from . import memtransfer as _MT
        s = _MT.stats()
        items = int(s.get("items") or 0)
        models = [str(m) for m in (s.get("models") or [])]
        multi = int(s.get("multi_model_items") or 0)
        if items == 0:
            return _item("transfer", "跨模型传承", IDLE, "0 条",
                         "继承包为空", "brief() 内部会自动 sync，聊一轮就有了")
        if not models:
            return _item("transfer", "跨模型传承", DEGRADE,
                         "%d 条 / 0 个模型" % items,
                         "有记忆条目，但没记下是哪个模型写的",
                         "检查 sync() 调用处有没有传 model")
        return _item("transfer", "跨模型传承", OK,
                     "%d 条 / %d 个模型 / %d 条多模型确认"
                     % (items, len(models), multi),
                     "写入过的模型：%s" % "、".join(models[:5]))
    except Exception as e:                                  # noqa: BLE001
        return _item("transfer", "跨模型传承", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 memtransfer.stats()")


# ------------------------------------------------------------ ③ 失败归因表
def _c_failattr(days=30):
    """不只看条数，还要看「写得够不够详细」—— 这是用户明确要求的。"""
    try:
        from . import failattr as _FA
        st = _FA.stats(days=int(days))
        rows = _FA.load(0) or []
        total = int(st.get("total") or 0)
        if total == 0:
            return _item("failattr", "失败归因表", IDLE, "0 条",
                         "近 %d 天没有失败记录" % int(days),
                         "跑一轮自迭代后开始积累；一直为 0 说明失败路径没被记到")
        n = len(rows)
        tb = sum(1 for r in rows if str(r.get("traceback") or "").strip())
        ctx = sum(1 for r in rows if str(r.get("context") or "").strip())
        vague = sum(1 for r in rows if r.get("too_vague"))
        rb = sum(1 for r in rows if str(r.get("why_rollback") or "").strip())
        lesson = sum(1 for r in rows if str(r.get("lesson") or "").strip())
        tb_rate = tb / float(n) if n else 0.0
        vague_rate = vague / float(n) if n else 0.0
        detail = ("%d 条｜堆栈 %d｜上下文 %d｜回滚原因 %d｜教训 %d"
                  % (n, tb, ctx, rb, lesson))
        # 判定：一半以上带堆栈、且写得笼统的不超过 3 成，才算「详细」
        if tb_rate < 0.5 or vague_rate > 0.3:
            return _item("failattr", "失败归因表", DEGRADE, detail,
                         "详细度不足：堆栈覆盖 %.0f%%，笼统原因占 %.0f%%"
                         % (tb_rate * 100, vague_rate * 100),
                         "检查调用处有没有传 traceback_text / why_failed 写太短")
        return _item("failattr", "失败归因表", OK, detail,
                     "高频坑：%s"
                     % ("、".join("%s×%d" % (x.get("kind_zh"), x.get("count"))
                                 for x in (st.get("repeat") or [])[:3]) or "无"))
    except Exception as e:                                  # noqa: BLE001
        return _item("failattr", "失败归因表", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 failattr.stats()")


# ------------------------------------------------------- ④ 语义相似检索
def _c_vec():
    try:
        from . import failvec as _FV
        s = _FV.status()
        rec = int(s.get("records") or 0)
        cached = int(s.get("cached_vectors") or 0)
        ready = bool(s.get("vec_ready"))
        model = str(s.get("model") or "")
        if rec <= 0:
            return _item("vec", "语义相似检索", IDLE, "无历史记录",
                         "归因表为空，没有可比对的对象",
                         "先积累几条失败记录再回来看")
        if not ready:
            return _item("vec", "语义相似检索", DEGRADE,
                         "%d 条记录 / 降级为字面匹配" % rec,
                         "向量后端没起来：%s" % (s.get("reason") or "未知"),
                         "降级仍可用（阈值已按实测下调），但召回不如向量")
        return _item("vec", "语义相似检索", OK,
                     "%d 条记录 / %d 条向量已缓存 / %s" % (rec, cached, model),
                     "混合权重 vec=%.2f cov=%.2f，阈值 %.3f"
                     % (float((s.get("weights") or {}).get("vec") or 0),
                        float((s.get("weights") or {}).get("cov") or 0),
                        float(s.get("min_score") or 0)))
    except Exception as e:                                  # noqa: BLE001
        return _item("vec", "语义相似检索", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 failvec.status()")


# ------------------------------------------------------------ ⑤ 累计评分
def _c_score():
    try:
        from . import iterloop as _IL
        st = _IL.stats()
        led = st.get("cumulative") or {}
        led_rounds = int(led.get("rounds") or 0)      # 账本才是轮数的权威来源
        cum = led.get("cum")
        hs = _IL.history(60)
        score_rounds = [h for h in hs if str(h.get("kind") or "") == "score_round"]
        if led_rounds == 0:
            return _item("score", "累计评分", IDLE, "0 轮",
                         "还没有提交过任何一轮",
                         "下一轮自迭代收尾时会提交第一笔")
        avg = st.get("avg_delta")
        imp = int(st.get("improved") or 0)
        reg = int(st.get("regressed") or 0)
        rb = int(st.get("rollbacks") or 0)
        detail = ("累计 %s｜%d 轮（进步 %d / 退步 %d / 回滚 %d）"
                  % (cum, led_rounds, imp, reg, rb))
        # 一致性：账本 rounds 必须与 history 里的「综合分轮」条数相等。
        # 实测踩过两次：① 清账本时没同步清 history.jsonl；② commit_round 压根
        # 不写 history（账本在涨、看板永远 0 轮）。两边打架时体检必须报出来，
        # 否则「累计分」看着正常、看板却是空的。
        if led_rounds <= 60 and led_rounds != len(score_rounds):
            return _item("score", "累计评分", DEGRADE, detail,
                         "账本 rounds=%d，但 history 里只有 %d 条综合分记录 —— 两边不一致"
                         % (led_rounds, len(score_rounds)),
                         "清账本要同步清 history.jsonl；否则检查 commit_round 有没有写 history")
        if avg is not None and float(avg) < -1.0:
            return _item("score", "累计评分", DEGRADE, detail,
                         "平均每轮 %+.2f 分，整体在退步" % float(avg),
                         "看 score.log 里的扣分原因，优先修 high")
        return _item("score", "累计评分", OK, detail,
                     "平均每轮 %s｜最好 %s｜最差 %s"
                     % ("%+.2f" % float(avg) if avg is not None else "—",
                        st.get("best"), st.get("worst")))
    except Exception as e:                                  # noqa: BLE001
        return _item("score", "累计评分", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 iterloop.stats()")


# --------------------------------------------------- ⑥ 回滚后自动重规划
def _c_replan():
    try:
        from . import self_iterate as _si
        p = _si._replan_budget_path()
        if not os.path.isfile(p):
            return _item("replan", "回滚后重规划", IDLE, "从未触发",
                         "还没有回滚后重规划的记录",
                         "这是正常的：只有改动被回滚时才会走这条路")
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        plans = [k for k in d.keys() if not str(k).startswith("_")]
        last = float(d.get("_last") or 0)
        ago = ("%.0f 分钟前" % ((time.time() - last) / 60.0)) if last else "未知"
        return _item("replan", "回滚后重规划", OK,
                     "%d 个计划触发过｜上次 %s" % (len(plans), ago),
                     "预算闸门在生效（同计划限次 + 全局冷却，防死循环）")
    except Exception as e:                                  # noqa: BLE001
        return _item("replan", "回滚后重规划", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 _replan_budget_path()")


# ------------------------------------------------------- ⑦ 重启后复核
def _c_postverify():
    try:
        from . import iterloop as _IL
        pending = bool(_IL.post_verify_pending())
        hs = _IL.history(50)
        did = [h for h in hs if str(h.get("kind") or "") == "post_verify"]
        if pending:
            return _item("postverify", "重启后复核", OK, "有待复核标记",
                         "重启后会自动再量一次分（后台线程）")
        if did:
            return _item("postverify", "重启后复核", OK,
                         "已复核 %d 次" % len(did),
                         "最近一次：%s" % (did[0].get("iso") or "—"))
        return _item("postverify", "重启后复核", IDLE, "暂无记录",
                     "还没发生过「改完自动重启」",
                     "自动重启后会标记并复核；没标记过就是空")
    except Exception as e:                                  # noqa: BLE001
        return _item("postverify", "重启后复核", DEGRADE, "读不到",
                     "%s: %s" % (type(e).__name__, e),
                     "检查 iterloop.post_verify_pending()")


# ------------------------------------------------- ⑧ 接线检查（源码级）
_WIRING = [
    ("memory", "记忆注入对话", "main.py", "_MT.brief("),
    ("attr", "归因注入反思", "self_iterate.py", "_failattr_brief("),
    ("vec", "语义检索接入", "self_iterate.py", "_FV.similar("),
    ("score", "迭代前量基线", "self_iterate.py", "_sc_before"),
    ("score", "累计分提交", "self_iterate.py", "commit_round"),
    ("replan", "回滚后重规划", "self_iterate.py", "_schedule_replan"),
    ("postverify", "启动后复核钩子", "main.py", "_loop_post_verify"),
]


def _c_wiring():
    """源码级确认「调用点还在」—— 数据为空时，这是唯一能区分「没跑过」和「断了」的办法。"""
    base = os.path.dirname(os.path.abspath(__file__))
    miss = []
    okn = 0
    for _key, label, fname, needle in _WIRING:
        path = os.path.join(base, fname)
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
        except Exception:                                   # noqa: BLE001
            miss.append("%s（读不到 %s）" % (label, fname))
            continue
        if needle in src:
            okn += 1
        else:
            miss.append("%s（%s 里找不到 %s）" % (label, fname, needle))
    total = len(_WIRING)
    if miss:
        return _item("wiring", "调用链接线", DEGRADE,
                     "%d/%d 通" % (okn, total),
                     "断点：%s" % "；".join(miss[:4]),
                     "接线断了的话，数据为空就永远填不上")
    return _item("wiring", "调用链接线", OK, "%d/%d 通" % (okn, total),
                 "7 个子系统的关键调用点全部在位")


# ------------------------------------------------------------------ 汇总
def check(days=30):
    """跑一遍全体系体检。只读、秒回，不触发评分/回归。"""
    t0 = time.time()
    items = [
        _c_memory(),
        _c_transfer(),
        _c_failattr(days=days),
        _c_vec(),
        _c_score(),
        _c_replan(),
        _c_postverify(),
        _c_wiring(),
    ]
    cnt = {OK: 0, DEGRADE: 0, IDLE: 0}
    for it in items:
        cnt[it["state"]] = cnt.get(it["state"], 0) + 1
    # ⚠️ 判「体系有没有在转」必须排除接线项：wiring 只要代码没被删就恒为 ok，
    #    把它算进分母会让「数据项全空」被误判成「体系正常」。实测踩过。
    data_items = [it for it in items if it["key"] != "wiring"]
    data_ok = [it for it in data_items if it["state"] == OK]
    data_idle = [it for it in data_items if it["state"] == IDLE]
    if cnt[DEGRADE]:
        overall = DEGRADE
        verdict = "%d 项需要关注，其余正常或待运行" % cnt[DEGRADE]
    elif not data_ok:
        overall = IDLE
        verdict = ("全部待运行 —— 接线 %d/%d 通，但真实环境里还没跑过任何一轮"
                   % (len(_WIRING), len(_WIRING)))
    elif data_idle:
        overall = OK
        verdict = ("体系在转：%d 项有数据、%d 项待运行（待运行不是故障）"
                   % (len(data_ok), len(data_idle)))
    else:
        overall = OK
        verdict = "全部 %d 项有数据且正常，体系协同运作" % len(data_ok)
    return {
        "ok": True,
        "overall": overall,
        "overall_zh": _STATE_ZH.get(overall, overall),
        "verdict": verdict,
        "counts": cnt,
        "items": items,
        "days": int(days),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.time() - t0, 3),
    }


def text(days=30):
    """压成一段人读简报（给卡片 / 日志 / 终端用）。"""
    r = check(days=days)
    lines = ["自迭代体系体检 · %s · %s" % (r["iso"], r["overall_zh"]),
             r["verdict"], ""]
    for it in r["items"]:
        line = "%s %-8s %s" % (it["icon"], it["name"], it["value"])
        if it["evidence"]:
            line += "｜" + it["evidence"]
        lines.append(line)
        if it["state"] == DEGRADE and it["hint"]:
            lines.append("    ↳ %s" % it["hint"])
    lines.append("")
    lines.append("耗时 %.2f 秒（只读，未触发评分/回归）" % r["seconds"])
    return "\n".join(lines)
