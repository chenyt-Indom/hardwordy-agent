# -*- coding: utf-8 -*-
"""验收探针：评分前「静态面优先修」+ 单次改动文件数上限 3 个（2026-11-23）。

跑法：py -m backend._probe_static_first_check

它验的是需求原文那四件事：
  [1] 单次改动文件数上限从「无限制」降到 3 个，超出的必须**明说**
      「超上限、顺延到下一轮」—— 不许静默丢文件；
  [2] 构造一处 silent-except（`except X: pass`）的改动 → precheck_static
      在**评分之前**就把它修掉（有 logger 就改成 logger.debug 留痕）；
  [3] 先修后评的顺序：`_score_after_verify` 里 precheck_static 必须先跑，
      且 gate() 被调用那一刻那处 silent-except 已经不在了；
  [4] 分不低于基线（不是退步），且结果里同时带着回归证据与 RSI
      （用户备注「拉起测试 RSI」）。

约定：不联网、不调 LLM、不落盘真实数据 —— `iter_runner._si` / `_FILE` 全部
指向临时目录，跑完原样还原。返回 0 表示全部通过。
"""
import ast
import os
import sys
import tempfile
import types
from pathlib import Path

_B = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _B)
sys.path.insert(0, os.path.dirname(_B))

_PROBE_SRC = '''# -*- coding: utf-8 -*-
"""探针模块：只给验收脚本用，跑完就删。"""
import logging

logger = logging.getLogger(__name__)


def risky(x):
    try:
        return 10 // x
    except ZeroDivisionError:
        pass
    return None
'''


def main():
    from backend import iter_runner as IR

    checks = []

    def check(name, cond, detail=""):
        checks.append(bool(cond))
        print("%s %s%s" % ("OK  " if cond else "FAIL", name,
                           "" if cond else "  <- " + str(detail)[:220]))

    tmp = Path(tempfile.mkdtemp(prefix="iter_static_first_"))
    root = tmp / "root"
    data = tmp / "data"
    (root / "backend").mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    probe = root / "backend" / "probe_mod.py"
    probe.write_text(_PROBE_SRC, encoding="utf-8")

    saved = {"si": IR._si, "file": IR._FILE, "audit": IR._run_static_audit,
             "base": IR.load_static_baseline, "regress": IR._regress_evidence}
    IR._si = types.SimpleNamespace(_ROOT=root, _DATA=data,
                                   _pid=lambda: "t_static_first")
    IR._FILE = data / "task.json"

    def _audit():
        """真读探针文件：里面还有 `except: pass` 就报一条扣分点。

        必须「真读」—— 否则 fix_new_static_points 修的**第二遍自检**
        （修完再查一次，确认真的修掉了）验不出来。
        """
        try:
            tree = ast.parse(probe.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return {"ok": False, "findings": [], "error": str(e)}
        pts = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body or []
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                pts.append({"file": "backend/probe_mod.py",
                            "line": int(node.lineno), "kind": "silent-except",
                            "level": "mid", "msg": "静默吞异常", "noscore": False})
        return {"ok": True, "findings": pts,
                "counts_scored": {"high": 0, "mid": len(pts), "info": 0}}

    IR._run_static_audit = _audit
    IR.load_static_baseline = lambda: {"keys": set(), "source": "测试基线",
                                       "at": "2026-11-23 18:00:00"}

    from backend import scoregate as SG
    from backend import lessonloop as LL
    saved.update({"gate": SG.gate, "rsi_sample": LL.rsi_sample,
                  "rsi_line": LL.rsi_line})
    try:
        # ---------------- [1] 单次改动文件数上限 = 3 ----------------
        keep, drop = IR._clip_change_files(
            ["backend/a.py", "backend/b.py", "backend/c.py",
             "backend/d.py", "backend/e.py"])
        check("上限 3：5 个文件只认 3 个", len(keep) == 3 and len(drop) == 2,
              (keep, drop))
        check("上限值就是 3（不是无限制）", IR._MAX_CHANGE_FILES == 3,
              IR._MAX_CHANGE_FILES)
        keep2, drop2 = IR._clip_change_files(
            ["backend/a.py", "backend/a.py", "backend/b.py"])
        check("裁剪顺带去重（同文件重复不占名额）",
              keep2 == ["backend/a.py", "backend/b.py"] and not drop2,
              (keep2, drop2))

        sc_scope = IR.change_scope(
            ["backend/a.py", "backend/b.py", "backend/c.py", "backend/d.py"],
            tid="t_cap")
        ev_text = " ".join(str(e.get("text") or "") for e in IR.task_events("t_cap"))
        check("超上限要有事件点名（不许静默丢文件）",
              "超上限" in ev_text and len(sc_scope["dropped"]) == 1, ev_text[-140:])

        mv = IR.mark_verify("plan_cap", "测试上限",
                            ["backend/a.py", "backend/b.py",
                             "backend/c.py", "backend/d.py", "backend/e.py"],
                            tid="t_cap")
        vrec = (mv or {}).get("verify") or {}
        check("登记待验证时只收 3 个文件", len(vrec.get("files") or []) == 3, vrec)
        check("超出的 2 个记进 files_over_cap（有据可查）",
              len(vrec.get("files_over_cap") or []) == 2
              and int(vrec.get("files_all") or 0) == 5, vrec)

        # ---------------- [2] 评分前先修掉新增的 silent-except ----------------
        probe.write_text(_PROBE_SRC, encoding="utf-8")
        pf = IR.precheck_static("t_pre", {"title": "探针",
                                         "files": ["backend/probe_mod.py"]})
        new_src = probe.read_text(encoding="utf-8")
        still_pass = any(
            isinstance(n, ast.ExceptHandler)
            and len(n.body or []) == 1
            and isinstance((n.body or [None])[0], ast.Pass)
            for n in ast.walk(ast.parse(new_src)))
        check("静态预检认出新增扣分点", int(pf.get("added_n") or 0) == 1, pf)
        check("评分前已就地修掉（fixed_n=1）", int(pf.get("fixed_n") or 0) == 1, pf)
        check("修法可核对：pass 没了、换成 logger.debug 留痕",
              (not still_pass) and 'logger.debug("silent-except"' in new_src,
              new_src[-200:])

        # ---------------- [3][4] 先修后评 / 分不低于基线 / 拉起测试 RSI ----------
        probe.write_text(_PROBE_SRC, encoding="utf-8")
        order = []
        seen = {}
        _orig_pre = IR.precheck_static

        def _pre(tid, v):
            order.append("precheck")
            return _orig_pre(tid, v)

        def _gate(**kw):
            order.append("gate")
            src = probe.read_text(encoding="utf-8")
            seen["pass_at_score"] = "\n        pass\n" in src
            return {"ran": True, "prev": 80.0, "total": 82.0, "delta": 2.0,
                    "verdict": "improved"}

        IR.precheck_static = _pre
        SG.gate = _gate
        IR._regress_evidence = lambda tid="", allow_run=True: {
            "ok": True, "total": 52, "passed": 52, "failed": 0, "skipped": 0,
            "scope": "quick", "summary": "全部通过（52 条）"}
        LL.rsi_sample = lambda force=False, min_gap=60.0: {"ok": True}
        LL.rsi_line = lambda: "RSI 61（最该修：静态面 silent-except）"

        sc = IR._score_after_verify(
            "t_score", {"title": "探针", "files": ["backend/probe_mod.py"]})
        check("顺序：precheck_static 在 gate 之前",
              order == ["precheck", "gate"], order)
        check("评分时那处 silent-except 已经修掉了",
              seen.get("pass_at_score") is False, seen)
        check("结果里带上「先修了几条」",
              int((sc.get("static_new") or {}).get("fixed") or 0) == 1,
              sc.get("static_new"))
        check("分不低于基线（不是退步）",
              float(sc.get("total") or 0) >= float(sc.get("prev") or 0)
              and sc.get("verdict") != "regressed",
              (sc.get("total"), sc.get("verdict")))
        check("评分结果同时带回归证据（拉起测试）",
              "52/52" in str((sc.get("regress") or {}).get("line") or ""),
              sc.get("regress"))
        check("评分结果同时带 RSI（拉起 RSI）", "RSI" in str(sc.get("rsi") or ""),
              sc.get("rsi"))
        check("结果里写明单次改动上限 = 3",
              int((sc.get("change_cap") or {}).get("limit") or 0) == 3,
              sc.get("change_cap"))

        md = IR._score_md(sc)
        check("报告里同时出现测试与 RSI",
              "测试（本轮回归）" in md and "RSI" in md, md[-300:])
        check("报告里写明单次改动上限", "单次改动上限" in md, md[-200:])
    finally:
        IR._si = saved["si"]
        IR._FILE = saved["file"]
        IR._run_static_audit = saved["audit"]
        IR.load_static_baseline = saved["base"]
        IR._regress_evidence = saved["regress"]
        SG.gate = saved["gate"]
        LL.rsi_sample = saved["rsi_sample"]
        LL.rsi_line = saved["rsi_line"]

    bad = len([c for c in checks if not c])
    print("\n%d/%d 通过" % (len(checks) - bad, len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
