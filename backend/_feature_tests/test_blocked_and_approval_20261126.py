# -*- coding: utf-8 -*-
"""2026-11-26 三处「不是故障，却被记成失败」的修复回归。

用户这轮的要求是「把不完全正常的东西全面修到完全正常」。上一轮全面体检
（65 个测试全过、24 个接口全 200）之后还剩两条真实缺口，本文件把它们钉住：

  A. **批准被旧快照覆盖**（11-25 23:09:12 那条 failed 的真因）：
     执行器开工时把整份 plans 读进内存，跑的过程中用户点了「批准」写进磁盘；
     收尾时旧快照被写回，把 approved 盖成 pending —— 任务轮到时只剩一句
     「还有 2 条是待批准」。修法：动作级合并，approved 永远不被 pending 覆盖。

  B. **执行前重读一次**：进入函数那一刻的快照可能比批准早 0.2 秒，
     todo 为空时必须从磁盘强制重读，真有 approved 就用它继续跑（不许直接判失败）。

  C. **前置拦截不算执行失败**：提案全被幽灵闸门拒 / 动作还在等批准，
     这两类根本没动手，原来一律记 failed，历史里堆了 9 条红卡，
     把「体系自评」拖成 bad。修法：单独记 blocked。

  D. **幽灵修复的根因**：提示词里从来没给过 fail_kind 的取值来源，
     模型只能自己编，编出来的名字账本计数必然为 0 → 整轮全被判幽灵。
     修法：把「真实失败分类清单」贴进提示词（_real_fail_kinds_block）。

  E. 前端要认识 blocked 这个新状态（否则显示成生英文）。

全程用 MM_DATA_DIR 隔离，不碰真实账本；不改任何业务文件。
跑法：python backend/_feature_tests/test_blocked_and_approval_20261126.py
"""
import inspect
import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

_TMP = tempfile.mkdtemp(prefix="mm_blocked_test_")
_OLD_ENV = os.environ.get("MM_DATA_DIR")
os.environ["MM_DATA_DIR"] = _TMP

from backend import self_iterate as SI                  # noqa: E402
from backend import iter_runner as IR                   # noqa: E402

# ⚠️ 这里**不能**像别的测试那样 import 完就把 MM_DATA_DIR 恢复：
#   self_iterate._DATA 是 import 时算好的常量，但 actlog 是**运行时**调
#   config.data_root() 取路径 —— 一恢复环境变量，下面读账本的就跑到真实
#   账本上去了（实测第一次跑就读到真实 17 个分类）。整个测试期间保持隔离，
#   进程结束自然失效，不需要手动还原。

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _act(aid, st):
    return {"id": aid, "status": st, "title": "动作 " + aid}


print("\n[A] 动作级合并：approved 不许被旧快照的 pending 覆盖")
_disk = {"id": "p1", "_upd": 100.0, "actions": [_act("a1", "approved")]}
_mem = {"id": "p1", "_upd": 200.0, "actions": [_act("a1", "pending")]}
_r = SI._pick_plan(_disk, _mem)
check("磁盘 approved + 内存（更新）pending → 取 approved",
      _r["actions"][0]["status"] == "approved", repr(_r["actions"]))

print("\n[B] 原有规则不许被改坏：终态仍然优先")
_disk = {"id": "p1", "_upd": 100.0, "actions": [_act("a1", "done")]}
_mem = {"id": "p1", "_upd": 200.0, "actions": [_act("a1", "approved")]}
_r = SI._pick_plan(_disk, _mem)
check("已 done 的动作不被内存版改回 approved",
      _r["actions"][0]["status"] == "done", repr(_r["actions"]))
_disk = {"id": "p1", "_upd": 100.0, "actions": [_act("a1", "failed")]}
_mem = {"id": "p1", "_upd": 200.0, "actions": [_act("a1", "pending")]}
_r = SI._pick_plan(_disk, _mem)
check("已 failed 的动作不被内存版改回 pending",
      _r["actions"][0]["status"] == "failed", repr(_r["actions"]))

print("\n[C] 整份 plans 合并（真实覆盖场景）")
_d = {"items": [{"id": "p1", "_upd": 100.0,
                 "actions": [{"id": "a1", "status": "approved"}]}]}
_m = {"items": [{"id": "p1", "_upd": 200.0,
                 "actions": [{"id": "a1", "status": "pending"}]}]}
_r = SI._merge_plans(_d, _m)
check("_merge_plans 后批准还在",
      _r["items"][0]["actions"][0]["status"] == "approved",
      repr(_r["items"][0]["actions"]))

print("\n[D] 执行前重读：todo 为空时必须从磁盘再取一次")
# ⚠️ execute_approved 被 _exec_guard 包了一层，inspect.getsource 拿到的是
#   wrapper（实测 617 字，不含函数体）。所以直接按源码文本切出函数体来断言。
_si_py = os.path.join(_ROOT, "backend", "self_iterate.py")
_txt = open(_si_py, encoding="utf-8", errors="replace").read()
_i0 = _txt.find("def execute_approved(")
_i1 = _txt.find("\ndef ", _i0 + 10)
_body = _txt[_i0:_i1 if _i1 > 0 else len(_txt)]
check("execute_approved 里有磁盘重读兜底",
      "_find_plan(pid)" in _body and "磁盘上有" in _body,
      "函数体 %d 字" % len(_body))
_idx_todo = _body.find('todo = [a for a in (plan.get("actions") or [])')
_idx_re = _body.find("_find_plan(pid)", _idx_todo)
_idx_bad = _body.find("这次反思没有已批准的动作")
check("重读发生在「todo 为空」之后、「判失败」之前",
      _idx_todo >= 0 and _idx_re > _idx_todo
      and (_idx_bad < 0 or _idx_re < _idx_bad),
      "todo=%d 重读=%d 判失败=%d" % (_idx_todo, _idx_re, _idx_bad))

print("\n[E] 拦截判定：不算执行失败")
_cases = [
    ("幽灵全拒 → phantom", IR._blocked_kind(
        "全部动作都是幽灵修复（近 7 天无真实失败记录），已拒绝执行", 0, False), "phantom"),
    ("没有已批准的动作 → no_approved", IR._blocked_kind(
        "这次反思没有已批准的动作（还有 2 条是待批准状态）", 0, False), "no_approved"),
    ("没有可执行的动作 → no_approved", IR._blocked_kind(
        "这次没有可执行的动作", 0, False), "no_approved"),
    ("真失败（报错）→ 不算拦截", IR._blocked_kind(
        "patch 失败：锚点没命中", 0, False), ""),
    ("成功过就不算拦截", IR._blocked_kind("幽灵修复", 2, False), ""),
    ("用户终止不算拦截", IR._blocked_kind("幽灵修复", 0, True), ""),
]
for _n, _got, _want in _cases:
    check(_n, _got == _want, "got=%r want=%r" % (_got, _want))

print("\n[F] 真实失败分类清单进提示词（治幽灵修复的根因）")
_attr = SI._DATA / "fail_attribution.jsonl"
_blk0 = SI._real_fail_kinds_block(days=7)
check("账本为空时不硬编（返回空串）", _blk0 == "", repr(_blk0[:80]))
try:
    SI._DATA.mkdir(parents=True, exist_ok=True)
    with open(_attr, "a", encoding="utf-8") as _f:
        _now = time.strftime("%Y-%m-%d %H:%M:%S")
        _f.write(json.dumps({"kind": "test_kind_zz", "kind_zh": "测试分类ZZ",
                             "ts": _now, "detail": "回归测试造的数据"},
                            ensure_ascii=False) + "\n")
    _blk = SI._real_fail_kinds_block(days=7)
    check("清单里出现真实分类 test_kind_zz", "test_kind_zz" in _blk, repr(_blk[:200]))
    check("清单带真实计数", "1 次" in _blk, repr(_blk[:200]))
    check("清单带硬约束（只能原样复制）", "原样复制" in _blk)
except Exception as _e:                                 # noqa: BLE001
    check("写测试账本失败：%s" % _e, False)

print("\n[G] 前端认识 blocked 状态")
_js = os.path.join(_ROOT, "frontend", "iterjournal.js")
try:
    _t = open(_js, encoding="utf-8", errors="replace").read()
    check("LABEL 里有 blocked", "blocked:" in _t)
    check("badgeCls 处理 blocked", 'st === "blocked"' in _t)
except Exception as _e:                                 # noqa: BLE001
    check("读 iterjournal.js 失败：%s" % _e, False)

print("\n[H] 健康度口径：真虚构计入，口径误判复核剔除")
_orig_al, _orig_lp = SI.list_actlog, SI.load_plans
try:
    SI.list_actlog = lambda n=300: []
    SI.load_plans = lambda: {"items": [{
        "id": "p_h", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "actions": [
            # ① 真的不存在于账本的分类 → 必须算幽灵
            {"id": "a1", "status": "rejected", "phantom": True,
             "fail_kind": "根本不存在分类ZZZ"},
            # ② 账本里查得到计数的（[F] 刚写入 test_kind_zz）→ 只是当时口径没对上
            {"id": "a2", "status": "rejected", "phantom": True,
             "fail_kind": "test_kind_zz"},
        ]}]}
    _h = SI.health_snapshot()
    check("真虚构仍然计入幽灵", int(_h.get("phantom_fix_count_7d") or 0) >= 1,
          "7d=%r" % _h.get("phantom_fix_count_7d"))
    check("能查到计数的被复核剔除",
          int(_h.get("phantom_recheck_cleared") or 0) >= 1,
          "cleared=%r" % _h.get("phantom_recheck_cleared"))
    check("health 返回窗口字段",
          _h.get("window_days") == 7 and "phantom_fix_count_7d" in _h)
    check("level 有值且是已知档位",
          _h.get("level") in ("good", "warn", "bad", "unknown"),
          str(_h.get("level")))
finally:
    SI.list_actlog, SI.load_plans = _orig_al, _orig_lp

print("\n[I] 真实读写盘：执行器旧快照写回时，批准必须还在")
try:
    SI._ensure()
    _pid = "p_race_%d" % int(time.time())
    SI.save_plans({"items": [{
        "id": _pid, "title": "并发批准回归",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "trigger": "test",
        "actions": [{"id": "a1", "title": "动作", "status": "pending"}]}]})
    _snap = json.loads(json.dumps(SI.load_plans()))     # 执行器开工时读到的旧快照
    _ap = SI.approve(_pid, ["a1"], note="回归测试：批准")
    _chk1 = [a["status"] for p in SI.load_plans()["items"] if p.get("id") == _pid
             for a in p.get("actions") or []]
    check("approve 后磁盘上是 approved", _chk1 == ["approved"], str(_chk1))
    # 执行器收尾：把旧快照的计划级 _upd 刷得比批准还新，再整份写回 —— 这就是真实竞态
    for _p in _snap["items"]:
        if _p.get("id") == _pid:
            _p["_upd"] = time.time() + 5
    SI.save_plans(_snap)
    _chk2 = [a["status"] for p in SI.load_plans()["items"] if p.get("id") == _pid
             for a in p.get("actions") or []]
    check("旧快照写回后批准没被覆盖", _chk2 == ["approved"], str(_chk2))
    check("approve 真的生效（approved=1）", (_ap or {}).get("approved") == 1, str(_ap))
except Exception as _e:                                 # noqa: BLE001
    check("并发批准回归异常：%s" % _e, False)

print("\n通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    for _x in FAIL:
        print("  ✗ %s" % _x)
    sys.exit(1)
print("全部通过：批准不再被覆盖 / 拦截不再算失败 / 幽灵修复有了根因修法")
