import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("MM_DATA_DIR", os.path.join(os.environ.get("TEMP", "/tmp"),
                                                  "mm_flow_time"))

t0 = time.time()
from backend import self_iterate as SI  # noqa: E402


def ev(kind, **kw):
    print("%6.1fs %s %s" % (time.time() - t0, kind,
                            str(kw.get("text") or kw)[:80]))


pid = "T%d" % os.getpid()
rel = "backend/_probe_flow_%d.py" % os.getpid()
p = os.path.join(str(SI._DATA), "settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump({"iter_agent_enabled": False, "regress_after_change": False,
           "audit_after_change": False, "loop_guard": False,
           "score_round": False, "converge_rounds": 0},
          open(p, "w", encoding="utf-8"))
json.dump({"items": [{"id": pid, "ts": "2026-11-24 00:00:00", "title": "t",
                      "trigger": "manual",
                      "actions": [{"id": "a1", "title": "p", "detail": "",
                                   "status": "approved", "risk": "low",
                                   "files": [rel], "targets": []}]}]},
          open(os.path.join(str(SI._DATA), "plans.json"), "w", encoding="utf-8"))
SI._plan_patch_for_action = lambda plan, act, prev_fail="": (
    [{"file": rel, "find": "", "replace": "FLAG = 1\n"}], "")
SI._iter_snapshot = lambda *a, **k: ""
SI._precheck_actions = lambda acts: ({}, "", 0)
out = SI.execute_approved(pid, on_event=ev, origin="manual")
print("total %.1fs done=%s cleanup=%s" % (time.time() - t0, out.get("done"),
                                          out.get("cleanup")))
print("exists", os.path.isfile(rel))
