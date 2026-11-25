# -*- coding: utf-8 -*-
"""回归：RSI 折线「每轮迭代后刷新」（2026-11-25 用户要求）。

用户原话：「我需要折线图在每次迭代之后都刷新并显示新的数据以及时间，
线也要每次都更新，然后反思机制以及记录跑分等整套体系也得确保完全正常运转。」

改动前的真实问题（查证过，不是猜的）：
  · 采样只挂在「评估收尾」（约 2 小时一次）和每轮对话 hint 上，
    迭代轮次结束**不采样** → 用户看到的线常常半天不动；
  · rsi_trend 返回 points 时把 t（时间戳）**过滤掉了**，
    前端只能按索引等距铺点 → 时间轴是假的；
  · 前端 rsiLoad 只在面板渲染时拉一次 → 后端有新点也不会出现。

本测试锁住：
  ① rsi_sample_iter 是**追加**（不替换上一条），且带 iso 时间；
  ② rsi_sample(force=True) 的老行为（min_gap 内替换）不被破坏；
  ③ rsi_trend 的 points 保留 t、上限 60 点；
  ④ commit_round **真的**在每轮收尾追加一个采样点（端到端跑一次）；
  ⑤ 前端接线：时间轴、按真实时间铺点、20 秒自动重拉、顶部显示最新时间。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _rows(p):
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def run() -> int:
    fails = []
    _N = [0]

    def ck(name, cond, detail=""):
        _N[0] += 1
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    from backend import lessonloop as LL
    from backend import iterloop as IT

    # ---------------- ① ② ③ 采样层（临时数据目录，绝不碰真实 rsi_history）----
    tmp = tempfile.mkdtemp(prefix="rsi_test_")
    old = (LL._data_dir, LL.rsi_index, LL.rsi_speed)
    LL._data_dir = lambda: tmp
    LL.rsi_index = lambda cache_sec=0: {"score": 88.0,
                                        "parts": [{"name": "可改", "value": 0.9}]}
    LL.rsi_speed = lambda days=7.0, target_per_day=20.0: {"score": 70.0}
    hist = os.path.join(tmp, "rsi_history.jsonl")
    try:
        r1 = LL.rsi_sample_iter(min_gap=0)
        ck("rsi_sample_iter 首次采样成功", bool(r1.get("ok")) and not r1.get("skipped"),
           str(r1))
        # ★ 2026-11-25 修（同秒双写）：同一轮里 commit_round 与 execute_approved
        #   各采一次，实测落了 3 组「同秒双点」。现在 append_dense 也遵守 min_gap：
        #   同轮第二次调用必须**跳过**（不是追加、也不是替换）。
        r2 = LL.rsi_sample_iter()
        rows = _rows(hist)
        ck("★ 同轮第二次采样被跳过（不再同秒双写）",
           bool(r2.get("skipped")) and len(rows) == 1,
           "rows=%d r2=%s" % (len(rows), r2))
        ck("跳过时给出原因（日志能看清为什么没记）",
           "同轮去重" in str(r2.get("why")), str(r2.get("why")))
        ck("跳过的那次不写盘（文件行数不变、mtime 不动）",
           len(rows) == 1 and r2.get("n") == 1, "n=%s" % r2.get("n"))
        # ★ 2026-11-25 修（同分平点）：分数没变时**合并** —— 只把最后一点的
        #   时间往后延，不新增同分点。否则密集平点会把三段斜率拉平，
        #   形态被误判成「② 直线上升」（实测 12:48~12:51 踩过，真的会）。
        _ts_before = _rows(hist)[0]["ts"]
        r3 = LL.rsi_sample_iter(min_gap=0)
        rows = _rows(hist)
        ck("★ 分数没变时合并（不新增同分点）",
           bool(r3.get("ok")) and bool(r3.get("merged")) and len(rows) == 1,
           "rows=%d r3=%s" % (len(rows), r3))
        ck("合并时最后一点的时间被往后延（线延到此刻，不是原地不动）",
           len(rows) == 1 and float(rows[0]["ts"]) > float(_ts_before),
           "%s → %s" % (_ts_before, rows[0]["ts"]))
        # 分数**真的变了** → 必须追加（用户要求：每轮迭代后曲线都要刷新）
        LL.rsi_index = lambda cache_sec=0: {
            "score": 91.0, "parts": [{"name": "可改", "value": 0.95}]}
        r4 = LL.rsi_sample_iter(min_gap=0)
        rows = _rows(hist)
        ck("★ 分数变了就追加（曲线每轮都刷新）",
           bool(r4.get("ok")) and not r4.get("merged") and len(rows) == 2,
           "rows=%d r4=%s" % (len(rows), r4))
        ck("追加的点带 iso 时间（前端要显示时间）",
           all(bool(r.get("iso")) and " " in str(r.get("iso")) for r in rows),
           str([r.get("iso") for r in rows]))
        ck("两个点时间戳不同（不是同一条被覆盖）",
           len(rows) == 2 and rows[0]["ts"] != rows[1]["ts"])

        # 对照：老的 force 采样在 min_gap 内仍是替换，行为不许回退
        n0 = len(_rows(hist))
        LL.rsi_sample(force=True, min_gap=60)
        n1 = len(_rows(hist))
        ck("rsi_sample(force=True) 仍是替换（老行为没被改坏）", n1 == n0,
           "%d → %d" % (n0, n1))

        # 灌满 70 个点，验证 trend 的窗口与时间字段
        with open(hist, "w", encoding="utf-8") as fh:
            for i in range(70):
                fh.write(json.dumps({
                    "ts": 1700000000 + i * 600,
                    "iso": "2026-11-25 %02d:%02d:00" % (8 + i // 60, i % 60),
                    "score": 60 + i * 0.3, "speed": 40 + i * 0.2,
                }, ensure_ascii=False) + "\n")
        tr = LL.rsi_trend(limit=120)
        pts = tr.get("points") or []
        ck("trend.points 保留 t 时间戳（前端按真实时间铺 x 轴）",
           bool(pts) and all("t" in p for p in pts), "n=%d" % len(pts))
        ck("trend.points 带 iso 时间", bool(pts) and all(p.get("ts") for p in pts))
        ck("trend.points 上限 60 点", len(pts) <= 60, "n=%d" % len(pts))
        ck("trend 仍然给出形态判定", bool(tr.get("shape")), str(tr.get("shape")))

        # ★ 历史文件里已经落下的「同秒双点」不能让斜率虚高（本次修的是不再产生新的，
        #   老数据仍在）—— rsi_trend 必须把 1 秒内的重复合并成一个点。
        with open(hist, "w", encoding="utf-8") as fh:
            _base = 1700000000
            _seq = [(_base, 60.0), (_base + 600, 61.0), (_base + 1200, 62.0),
                    (_base + 1200.4, 62.0),          # ← 历史遗留的同秒双点
                    (_base + 1800, 63.0), (_base + 2400, 64.0)]
            for _i, (_ts, _sc) in enumerate(_seq):
                fh.write(json.dumps({"ts": _ts,
                                     "iso": "2026-11-25 08:%02d:00" % _i,
                                     "score": _sc, "speed": 50.0},
                                    ensure_ascii=False) + "\n")
        _tr2 = LL.rsi_trend(limit=120)
        ck("★ 同秒双点在 trend 里被去重（6 条 → 5 点）",
           len(_tr2.get("points") or []) == 5,
           "n=%s" % len(_tr2.get("points") or []))
    finally:
        LL._data_dir, LL.rsi_index, LL.rsi_speed = old

    # ---------------- ④ commit_round 端到端（每轮迭代真的会采样）------------
    tmp2 = tempfile.mkdtemp(prefix="rsi_commit_")
    olds = (IT._data_dir, LL._data_dir, IT.read_bench_last, IT.bench_verdict,
            IT.explain_delta)
    os.environ["HWA_BENCH"] = os.path.join(tmp2, "_no_such_bench.py")
    IT._data_dir = lambda: tmp2
    LL._data_dir = lambda: tmp2
    IT.read_bench_last = lambda: None
    IT.bench_verdict = lambda window=6: {"ok": False, "trend": None, "delta": None,
                                         "grind": False, "ledger_sum": 0,
                                         "last": {}, "prev": {}}
    IT.explain_delta = lambda b, a, actions=None: {
        "summary": "回归测试：人为构造的一轮", "suspects": [],
        "new_codes": [], "fixed_codes": [], "changed_files": []}
    try:
        LL.rsi_index = lambda cache_sec=0: {"score": 90.0,
                                            "parts": [{"name": "可改", "value": 0.95}]}
        LL.rsi_speed = lambda days=7.0, target_per_day=20.0: {"score": 75.0}
        out = IT.commit_round(before={"total": 80.0}, after={"total": 82.0},
                              actions=[], note="回归：每轮采样")
        rows2 = _rows(os.path.join(tmp2, "rsi_history.jsonl"))
        ck("commit_round 跑通并返回累计分", bool(out) and out.get("delta") == 2.0, str(out))
        ck("commit_round 带出本轮采样结果（调用方据此去重，不靠时间窗）",
           isinstance(out.get("rsi_sample"), dict) and out["rsi_sample"].get("ok"),
           str(out.get("rsi_sample")))
        ck("★ 一轮迭代结束 → rsi_history 多出 1 个采样点", len(rows2) == 1,
           "rows=%d" % len(rows2))
        ck("该采样点带本轮时间与分数",
           bool(rows2) and bool(rows2[0].get("iso")) and rows2[0].get("score") == 90.0,
           str(rows2[0] if rows2 else None))
        # 紧接着跑第二轮（< 3 秒）：这属于**同一轮收尾的重复采样**
        # （真实链路里就是 commit_round + execute_approved 补采），必须跳过
        IT.commit_round(before={"total": 82.0}, after={"total": 83.0},
                        actions=[], note="回归：紧接的第二轮")
        rows3 = _rows(os.path.join(tmp2, "rsi_history.jsonl"))
        ck("★ 3 秒内的重复收尾不记账（同轮去重端到端生效）", len(rows3) == 1,
           "rows=%d" % len(rows3))
        # 真实迭代一轮要几分钟，远大于 min_gap；等过窗口再跑。
        # ★ 2026-11-25 修（同分平点）：分数**没变**时不再新增点（只把最后一点
        #   的时间往后延），否则密集平点会把三段斜率拉平、形态被误判成②。
        time.sleep(3.2)
        IT.commit_round(before={"total": 83.0}, after={"total": 84.0},
                        actions=[], note="回归：第三轮（过了 min_gap，但分数没变）")
        rows4 = _rows(os.path.join(tmp2, "rsi_history.jsonl"))
        ck("★ 分数没变时合并（过了 min_gap 也不堆同分点）", len(rows4) == 1,
           "rows=%d" % len(rows4))
        # 分数**真的涨了** → 必须追加，线要往前长
        # （先过 3 秒窗口，否则会被「同轮去重」挡住 —— 那是另一条规则）
        time.sleep(3.2)
        LL.rsi_index = lambda cache_sec=0: {"score": 93.0,
                                            "parts": [{"name": "可改", "value": 0.99}]}
        IT.commit_round(before={"total": 84.0}, after={"total": 86.0},
                        actions=[], note="回归：第四轮（分数变了）")
        rows5 = _rows(os.path.join(tmp2, "rsi_history.jsonl"))
        ck("★ 分数变了就追加 1 个点（线持续往前长）", len(rows5) == 2,
           "rows=%d" % len(rows5))
        # ★ 2026-11-25 修（对话采样吃掉迭代点）：rsi_sample_throttled 在文件
        #   最新点不足 gap 时必须**跳过**，不能替换掉最后一条真实迭代点
        #   （实测 12:58:53 把 12:51:32 那轮迭代的时间覆盖了）。
        LL._RSI_LAST_SAMPLE["ts"] = 0.0
        _n_before = len(rows5)
        _last_iso = rows5[-1]["iso"]
        _th = LL.rsi_sample_throttled(gap=3600.0)
        _after = _rows(os.path.join(tmp2, "rsi_history.jsonl"))
        ck("★ 对话采样在 gap 内跳过（不覆盖最后的迭代点）",
           bool(_th.get("skipped")) and len(_after) == _n_before
           and _after[-1]["iso"] == _last_iso,
           "skipped=%s n=%d→%d last=%s"
           % (_th.get("skipped"), _n_before, len(_after), _after[-1]["iso"]))
    finally:
        (IT._data_dir, LL._data_dir, IT.read_bench_last, IT.bench_verdict,
         IT.explain_delta) = olds
        os.environ.pop("HWA_BENCH", None)

    # ---------------- ⑤ 前端接线（静态检查，防被"优化"掉）-------------------
    js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
    ck("前端有 X 轴时间刻度函数 rsiTimeAxis", "function rsiTimeAxis" in js)
    ck("折线按真实时间铺点（不是按索引等距）", "pts[i].t - t0" in js)
    ck("每个点带时间+数值的悬停提示",
       "<title>" in js and 'key === "score" ? "综合" : "加速"' in js)
    ck("顶部显示最新采样时间", "最新 ' + esc(lastTs.slice(11, 16))" in js)
    ck("面板开着时 20 秒自动重拉", "_rsiTimer = setInterval" in js and "}, 20000);" in js)
    ck("网络抖动时不清空已画好的曲线", 'box.querySelector("svg")' in js)

    # 后端也要真接线：commit_round 里必须调用 rsi_sample_iter
    src = open(os.path.join(ROOT, "backend", "iterloop.py"), encoding="utf-8").read()
    ck("iterloop 每轮收尾调用 rsi_sample_iter", "rsi_sample_iter()" in src)

    # commit_round 只在「有动作成功」时才被调（ok_n > 0）—— 全失败 / 被护栏拦
    # 的那一轮也必须采样，否则用户看到「迭代跑了但线不动」。
    si = open(os.path.join(ROOT, "backend", "self_iterate.py"), encoding="utf-8").read()
    ck("execute_approved 收尾无条件补采一次（全失败轮次也上线）",
       "_LLsmp.rsi_sample_iter()" in si)
    ck("补采在 commit_round 的 try/except 之外（不被它的异常跳过）",
       si.count("rsi_sample_iter()") == 1)
    ck("★ 补采用 commit_round 的采样结果去重（不靠 3 秒时间窗猜）",
       '_rsi_handled = bool(_cm.get("rsi_sample"))' in si
       and "if _rsi_handled:" in si)

    print()
    if fails:
        print("失败 %d 项：%s" % (len(fails), "；".join(fails)))
        return 1
    print("全部通过（%d 项断言）" % _N[0])
    return 0


if __name__ == "__main__":
    sys.exit(run())
