# -*- coding: utf-8 -*-
"""2026-11-25 三个真实故障的回归测试（用户报：迭代一直失败 / 终止停不下来 / 曲线走平）。

本文件把四件事钉住，防止以后改回去：

  A. 幽灵修复闸门：模型写**中文** fail_kind（「跑分退步（低于上一版）」）时，
     账本里 kind=score_regress + kind_zh=同一个中文名，必须能命中；
     真正没发生过的分类仍然要拒绝（闸门不能被放水）。
  B. 统计表不许被事件类型污染：task.json events 里的 kind 是 step/code/think，
     那是**事件类型**，不是失败分类，不能进 fail_kind_counts。
  C. 终止必须**立刻**停：_exec 命中终止时抛 _Cancelled（原来返回一句话让模型
     再跑一轮 finish，本地模型一次几分钟，实测等了 297 秒）。
  D. 曲线不许被同分点刷平：评分闸门每分钟一轮，分数没变时只延后最后一点，
     不新增点；真变了才追加。

全程用 MM_DATA_DIR + monkeypatch 隔离，不碰真实账本。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="mm_iterfix_test_")
_OLD_ENV = os.environ.get("MM_DATA_DIR")
os.environ["MM_DATA_DIR"] = _TMP

from backend import actlog, iter_agent, lessonloop  # noqa: E402

if _OLD_ENV is None:
    os.environ.pop("MM_DATA_DIR", None)
else:
    os.environ["MM_DATA_DIR"] = _OLD_ENV

_OK = 0
_FAIL = 0
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, detail=""):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _FAIL += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


def _write_attr(rows):
    d = os.path.join(_TMP, "data", "self_iterate")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "fail_attribution.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _write_task_events(rows):
    d = os.path.join(_TMP, "data", "self_iterate")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "task.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"events": rows}, f, ensure_ascii=False)
    return p


def main():
    # ★ 隔离：actlog 的归因账本走 config.data_root()，必须指到临时目录 ——
    #   否则会读真实账本（那里 score_regress 有 18 条），断言 1 条永远不过。
    actlog.config.data_root = lambda *a, **k: _TMP
    print("\n[A] 幽灵修复闸门：中文 kind_zh 必须能命中")
    _write_attr([
        {"ts": __import__("time").time() - 3600, "kind": "score_regress",
         "kind_zh": "跑分退步（低于上一版）", "why_failed": "掉了 1.38 分"},
        {"ts": __import__("time").time() - 7200, "kind": "model_error",
         "kind_zh": "模型输出不可用", "why_failed": "输出被截断"},
    ])
    r1 = actlog.propose_patch("跑分退步（低于上一版）")
    check("中文全角括号能命中", bool(r1.get("accepted")) and int(r1.get("count") or 0) == 1,
          "count=%s" % r1.get("count"))
    r2 = actlog.propose_patch("跑分退步(低于上一版)")
    check("中文半角括号也能命中", bool(r2.get("accepted")) and int(r2.get("count") or 0) == 1,
          "count=%s" % r2.get("count"))
    r3 = actlog.propose_patch("score_regress")
    check("英文 kind 仍然能命中", bool(r3.get("accepted")) and int(r3.get("count") or 0) == 1,
          "count=%s" % r3.get("count"))
    r4 = actlog.propose_patch("凭空捏的失败分类")
    check("真正没发生过的仍然拒绝（闸门没放水）", not r4.get("accepted"), str(r4.get("reason"))[:60])

    print("\n[B] 统计表不被事件类型污染")
    _write_task_events([
        {"kind": "step", "text": "第 1 步", "fail_kind": ""},
        {"kind": "code", "text": "卡片"},
        {"kind": "think", "text": "思考"},
        {"kind": "phantom_reject", "fail_kind": "跑分退步（低于上一版）"},
    ])
    cnt = actlog.fail_kind_counts(
        7, path=os.path.join(_TMP, "data", "self_iterate", "task.json"))
    check("step/code/think 不进统计", not ({"step", "code", "think"} & set(cnt)),
          "keys=%s" % list(cnt)[:6])
    check("phantom 事件的 fail_kind 正常计数",
          int(cnt.get("跑分退步(低于上一版)") or 0) == 1, str(cnt))

    print("\n[C] 终止必须立刻停（抛 _Cancelled，不再让模型跑一轮 finish）")

    class _Ctx(object):
        def __init__(self):
            self.stopped = False

        def stop(self):
            return True

        def record(self, *a, **k):
            pass

        def emit(self, *a, **k):
            pass

        def over(self):
            return ""

    ctx = _Ctx()
    raised = False
    try:
        iter_agent._exec(ctx, "write_file", {"path": "x.py", "content": "1"})
    except iter_agent._Cancelled:
        raised = True
    except Exception as e:                                   # noqa: BLE001
        print("     意外异常：%r" % (e,))
    check("_exec 命中终止抛 _Cancelled", raised)
    check("ctx.stopped 被置位", ctx.stopped is True)

    src = open(os.path.join(_ROOT, "iter_agent.py"), encoding="utf-8").read()
    check("_run_once 单独捕获 _Cancelled（不当模型失败）", "except _Cancelled:" in src)
    check("终止时跳过最终复核", "if cancelled:\n            why = why or \"用户终止\"" in src)
    check("流式生成中每块都检查终止", "用户终止（生成中）" in src)
    check("本机循环轮间也检查终止", "用户终止（轮间）" in src)
    si = open(os.path.join(_ROOT, "self_iterate.py"), encoding="utf-8").read()
    check("execute_approved 有终止专用分支", 'elif _ar.get("cancelled"):' in si)
    _blk = si.split('elif _ar.get("cancelled"):', 1)[1].split("else:", 1)[0]
    check("终止分支不写失败账本", "_log_act(plan, a, False" not in _blk)
    check("终止分支把动作退回待批准", 'a["status"] = "pending"' in _blk)

    print("\n[D] 曲线：同分不新增点，真变了才追加")
    hist = os.path.join(_TMP, "rsi_hist.jsonl")
    with open(hist, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1000.0, "iso": "t0", "score": 94.9,
                            "parts": {"a": 1.0}}, ensure_ascii=False) + "\n")
    lessonloop._rsi_hist_path = lambda: hist
    _cur = {"score": 94.9, "parts": [{"name": "a", "value": 1.0}]}
    lessonloop.rsi_index = lambda: dict(_cur)
    n0 = sum(1 for _ in open(hist, encoding="utf-8"))
    lessonloop.rsi_sample_score()
    n1 = sum(1 for _ in open(hist, encoding="utf-8"))
    check("同分时点数不增加", n1 == n0, "before=%d after=%d" % (n0, n1))
    last = json.loads([l for l in open(hist, encoding="utf-8") if l.strip()][-1])
    check("同分时最后一点的时间被延后", float(last.get("ts") or 0) > 1000.0,
          "ts=%s" % last.get("ts"))
    check("同分点标记 src=score", last.get("src") == "score", str(last.get("src")))

    _cur = {"score": 95.4, "parts": [{"name": "a", "value": 1.2}]}
    lessonloop.rsi_sample_score()
    n2 = sum(1 for _ in open(hist, encoding="utf-8"))
    check("分数真变了就追加新点", n2 == n1 + 1, "before=%d after=%d" % (n1, n2))

    # 分数连续上升 → 曲线必须真的往上走（用户要的「不涨走平」就是这条）。
    # ⚠️ 必须给**不同时间戳**：rsi_trend 会把同一秒内的点折叠成一个（那是防并发
    #    双写的既有正确行为），同一秒连写 5 条当然只剩 1 点。
    with open(hist, "w", encoding="utf-8") as f:
        for _i, _s in enumerate([94.9, 96.0, 97.2, 98.6]):
            f.write(json.dumps({"ts": 1000.0 + _i * 600, "iso": "t%d" % _i,
                                "score": _s, "parts": {"a": _s / 100.0}},
                               ensure_ascii=False) + "\n")
    lessonloop._RSI_CACHE.update({"ts": 0.0, "val": None})
    _tr = lessonloop.rsi_trend()
    check("分数连升后趋势斜率为正", float(_tr.get("slope") or 0) > 0,
          "slope=%s shape=%s" % (_tr.get("slope"), _tr.get("shape")))
    check("趋势点数 = 真实落点数", int(_tr.get("n") or 0) >= 4,
          "n=%s" % _tr.get("n"))

    il = open(os.path.join(_ROOT, "iterloop.py"), encoding="utf-8").read()
    check("commit_round 支持 sample 口径", 'sample="round"' in il and "rsi_sample_score()" in il)
    sg = open(os.path.join(_ROOT, "scoregate.py"), encoding="utf-8").read()
    check("评分闸门传 sample=score", 'sample="score"' in sg)

    print("\n[E] 点「执行已批准的」不再报「没有已批准的动作」")
    mn = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    check("enqueue 里有自动批准兜底", "enqueue 自动批准" in mn and "auto_approved" in mn)
    check("只批准 pending，不碰 rejected/dup",
          'a.get("status") == "pending"' in mn and "_si.approve(pid, action_ids=_pd" in mn)

    print("\n结果：通过 %d / 失败 %d" % (_OK, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
