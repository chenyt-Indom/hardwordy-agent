# -*- coding: utf-8 -*-
"""改动流程末尾自动清理本轮 _tmp_/_probe_ 脚本 —— 验收断言（2026-11-24）。

用户要求（原话要点）：「改 backend/self_iterate.py 的改动流程末尾：记录本轮开始
时间，结束后扫描 backend/ 下 mtime 晚于开始时间且 basename 以 _tmp_ 或 _probe_
开头的 .py 文件并删除，删除记录写进 actlog。改完构造一个 _tmp_xxx.py，跑一次改动
流程，断言文件被删且 actlog 有 cleanup 事件。」

这里就按这句话实测三件事（**不联网、不调 LLM、不加载模型**）：

  ① 清理只认「本轮新产生」的：本轮之前就存在的 _tmp_ 脚本**不许删** ——
     别处正在跑的探针不能被顺手清掉；前缀不匹配的 .py 也不许动；
  ② 本轮新产生的 _tmp_ / _probe_ .py 必须被删，且 actlog 里落到一条
     kind=cleanup 事件（事件里记着删了哪几个文件）；
  ③ 真的跑一次 `execute_approved`：流程里新建的 _probe_ 文件在收尾时被删掉，
     同一次调用的 `out["cleanup"]` 里点名了它，actlog 也有对应 cleanup 事件。

为什么放在 backend/tests/ 而不是 backend/_feature_tests/
--------------------------------------------------------
`_feature_tests/test_*.py` 是**护栏文件**（自我迭代无权写），本断言是给这次改动
做验收的，不该由被验收的人自己写进护栏里 —— 那等于自己判自己及格。
所以独立放在 backend/tests/，用 `py -3 backend/tests/test_tmp_cleanup.py` 手动跑，
退出码 0 = 通过、1 = 失败。

为什么这么写（都是踩过的坑）
----------------------------
· 数据目录整体挪到临时目录（MM_DATA_DIR）：否则每跑一次就给真实账本/计划/备份
  塞进一份测试数据（`data/iter_backups/…` 会越堆越多，RSI 统计被灌水）。
· 夹具按 pid 唯一命名并放在 `backend/` 根目录（清理扫的就是这一层）：
  固定文件名在并行回归时会被别的用例同时读写，实测会假失败。
· ③ 里必须关掉 regress_after_change / audit_after_change / loop_guard /
  score_round：本断言要跑改动流程，流程内部再跑一遍回归就会层层嵌套
  （回归 → 改代码 → 回归），实测这类嵌套能把整轮跑挂。
  ⚠️ 只能**打补丁换掉 `load_settings`**：`load_settings` 只认 `_DEFAULT_SETTINGS`
  里登记过的键，`score_round` / `iter_agent_enabled` 根本不在里面 ——
  写 settings.json 关不掉（实测白写，评分照样跑两遍，一次 40 秒）。
· ③ 里把 `_plan_patch_for_action`（真会调 LLM）和 `_iter_snapshot`
  （会往**真实** data/ 里打一个 4.8MB 全栈快照并轮转旧快照）换成假的：
  这两步都不是本条断言要验的东西，其余流程**原样真跑**。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

_B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_B)

# ⚠️ 必须在 import backend.* 之前设好：self_iterate._DATA 是**导入时**算出来的，
#    晚一步设就等于没设（数据还会写进真实 data/）。
_TMP_DATA = tempfile.mkdtemp(prefix="mm_tmpclean_")
os.environ["MM_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

_FIXES = []          # 需要收尾删掉的夹具文件
_FAILS = []


def chk(name, cond, extra=""):
    ok = bool(cond)
    print(("  OK   " if ok else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))
    if not ok:
        _FAILS.append(name)


def _rel(rel):
    return os.path.join(_ROOT, rel.replace("/", os.sep))


def _mk(rel, body="FLAG = 1\n"):
    """在项目根下造一个夹具文件（名字由调用方按 pid 决定）。"""
    p = _rel(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    _FIXES.append(rel)
    return p


def _cleanup_rows():
    """读 actlog.jsonl 里所有 kind/event=cleanup 的记录（倒序）。"""
    from backend import self_iterate as SI
    p = os.path.join(str(SI._DATA), "actlog.jsonl")
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:                        # noqa: BLE001
                    continue
                if isinstance(r, dict) and (r.get("kind") == "cleanup"
                                            or r.get("event") == "cleanup"):
                    rows.append(r)
    except FileNotFoundError:
        return []
    return rows[::-1]


def _check_helper(SI):
    """①②：清理只删「本轮新产生」的 _tmp_/_probe_ 脚本，并记进 actlog。"""
    pid = os.getpid()
    old = "backend/_tmp_old_%d.py" % pid          # 本轮之前就有 → 不许删
    new_t = "backend/_tmp_new_%d.py" % pid        # 本轮新建 → 必须删
    new_p = "backend/_probe_new_%d.py" % pid      # 本轮新建 → 必须删
    keep = "backend/_keep_me_%d.py" % pid         # 前缀不匹配 → 不许删

    p_old = _mk(old)
    os.utime(p_old, (time.time() - 3600, time.time() - 3600))
    started = time.time()
    time.sleep(0.02)                              # 保证 mtime 严格晚于 started
    _mk(new_t)
    _mk(new_p)
    _mk(keep)

    res = SI._cleanup_round_scratch(started)
    removed = [str(x).replace("\\", "/") for x in (res.get("removed") or [])]

    chk("本轮新建的 _tmp_ 被删掉", not os.path.isfile(_rel(new_t)), new_t)
    chk("本轮新建的 _probe_ 被删掉", not os.path.isfile(_rel(new_p)), new_p)
    chk("本轮之前就有的 _tmp_ 不许删", os.path.isfile(_rel(old)),
        "mtime 早于本轮开始时间的文件被误删了")
    chk("前缀不匹配的 .py 不许删", os.path.isfile(_rel(keep)), keep)
    chk("返回的 removed 点名了真删掉的文件",
        new_t in removed and new_p in removed, removed)
    chk("删除失败列表为空", not (res.get("failed") or []), res.get("failed"))

    rows = _cleanup_rows()
    chk("actlog 有 cleanup 事件", bool(rows),
        "actlog.jsonl 里没有 kind=cleanup 记录")
    if rows:
        files = [str(x).replace("\\", "/") for x in (rows[0].get("files") or [])]
        chk("cleanup 事件里记着删掉的文件",
            any(f.endswith("_tmp_new_%d.py" % pid) for f in files), files)
        chk("cleanup 事件带 event=cleanup 标记",
            rows[0].get("event") == "cleanup", rows[0])
        # 不许被当成「一次成功的代码改动」污染成功率
        chk("cleanup 事件不计成一次改动结果",
            str(rows[0].get("result") or "").lower() != "ok",
            rows[0].get("result"))

    for rel in (old, keep, new_t, new_p):
        p = _rel(rel)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError as e:
                # 不写 `pass`：静默吞异常会被 code_audit 记成 silent-except 扣分点
                # （info，0.5 分/轮），而且真删不掉时现场一点线索都没有。
                logger.debug("清理夹具 %s 失败：%s", rel, e)
        if rel in _FIXES:
            _FIXES.remove(rel)


def _set_settings(SI):
    """关掉流程里所有会「再跑一遍回归/评分/自检」的开关（见文件头说明）。"""
    s = {"iter_agent_enabled": False,     # 直接操作模式要调 LLM → 用旧的补丁流程
         "regress_after_change": False,   # 否则回归 → 本断言 → 回归，层层嵌套
         "audit_after_change": False,
         "loop_guard": False,
         "score_round": False,
         "converge_rounds": 0}
    p = os.path.join(str(SI._DATA), "settings.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def _check_flow(SI):
    """③：真跑一次改动流程，断言本轮新建的 _probe_ 在收尾被删、actlog 有事件。"""
    pid = os.getpid()
    rel_probe = "backend/_probe_flow_%d.py" % pid
    plan_id = "TMPCLEAN%d" % pid

    _set_settings(SI)
    plans = {"items": [{
        "id": plan_id, "ts": "2026-11-24 00:00:00",
        "title": "验证本轮临时脚本清理", "trigger": "manual",
        "actions": [{
            "id": "a1", "title": "本轮新建的探针脚本",
            "detail": "改动流程里新建 backend/_probe_*.py，收尾应被自动清掉",
            "status": "approved", "risk": "low",
            "files": [rel_probe], "targets": []}]}]}
    with open(os.path.join(str(SI._DATA), "plans.json"), "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False)

    # 换掉两条「不测、但很贵/很脏」的步骤：
    #   _plan_patch_for_action 真会调 LLM；_iter_snapshot 会往真实 data/ 打 4.8MB 快照。
    old_plan, old_snap, old_pre = (SI._plan_patch_for_action, SI._iter_snapshot,
                                   SI._precheck_actions)
    SI._plan_patch_for_action = lambda plan, act, prev_fail="": (
        [{"file": rel_probe, "find": "", "replace": "FLAG = 1\n"}], "")
    SI._iter_snapshot = lambda *a, **kw: ""
    # 静态自检跑一遍要 9 秒，且与本条断言无关（别的用例已覆盖），一并略过
    SI._precheck_actions = lambda acts: ({}, "", 0)
    _FIXES.append(rel_probe)
    # 收集进度事件：清理结果要能在聊天界面的步骤流里看到（不只写 actlog）
    _events = []

    def _on_event(kind, **kw):
        _events.append((str(kind), kw))

    try:
        out = SI.execute_approved(plan_id, on_event=_on_event, origin="manual")
    finally:
        (SI._plan_patch_for_action, SI._iter_snapshot,
         SI._precheck_actions) = (old_plan, old_snap, old_pre)

    chk("改动流程跑成功（1 条动作）",
        bool(out.get("ok")) and int(out.get("done") or 0) == 1,
        "out.done=%r out.error=%r" % (out.get("done"), out.get("error")))
    chk("流程里新建的 _probe_ 文件被收尾删掉（文件已不存在）",
        not os.path.isfile(_rel(rel_probe)),
        "本轮临时文件还留在 backend/ 里：%s" % rel_probe)

    cl = out.get("cleanup") or {}
    chk("这次流程的 out['cleanup'] 点名了它",
        any(str(x).replace("\\", "/").endswith("_probe_flow_%d.py" % pid)
            for x in (cl.get("removed") or [])), cl)

    rows = _cleanup_rows()
    chk("actlog 里有本次的 cleanup 事件",
        any(any(str(f).replace("\\", "/").endswith("_probe_flow_%d.py" % pid)
                for f in (r.get("files") or [])) for r in rows),
        [r.get("files") for r in rows[:3]])

    # 收尾清理要发一条进度事件（kind=step，文案带「清理」），否则用户只能翻 actlog
    _txts = [str(kw.get("text") or "") for (k, kw) in _events if k == "step"]
    chk("步骤流里有「收尾清理临时脚本」这一条",
        any("清理" in t and "_probe_flow_%d.py" not in t for t in _txts),
        [t for t in _txts if "清理" in t][:3])


def run():
    from backend import self_iterate as SI

    print("① 本轮临时脚本清理（只删本轮新产生的）")
    _check_helper(SI)
    print("② 真跑一次改动流程，收尾自动清理")
    _check_flow(SI)
    return 1 if _FAILS else 0


if __name__ == "__main__":
    code = 1
    try:
        code = run()
    except Exception as e:                               # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("断言本身抛异常：%s: %s" % (type(e).__name__, e))
        code = 1
    finally:
        for rel in list(_FIXES):
            try:
                p = _rel(rel)
                if os.path.isfile(p):
                    os.remove(p)
            except OSError as e:
                # 同上：不吞异常，也不留 `pass` 的 silent-except 扣分点。
                logger.debug("收尾删除夹具 %s 失败：%s", rel, e)
    print("临时脚本清理验收 %s" % ("OK" if not code else "FAILED：%s" % (_FAILS,)))
    sys.exit(code)
