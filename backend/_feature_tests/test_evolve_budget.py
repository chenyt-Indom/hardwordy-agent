# -*- coding: utf-8 -*-
"""进化预算（evolve budget）的离线回归 —— 只碰临时文件，绝不动真实账本。

用户 2026-11-24 转述的专家意见（L3 三缺之三）：
  「没有预算和自动循环（进化靠用户叫一声才动，不是自己在跑）」

这里验四类事：
  1) **账本语义**：先问再扣、超限拒绝并说清怎么办、手动豁免；
  2) **跨天重置**：只清消耗，不动用户调过的上限；
  3) **坏文件不炸**：账本读坏了按「今天没花过」处理，不能让自动化瘫痪；
  4) **真接线**：selfloop（计划数）/ iter_runner（改动文件、自动重启）
     三处必须真的在调它 —— 不然这套预算就是摆设。

跑法：python backend/_feature_tests/test_evolve_budget.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import evolve_budget as EB             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def main():
    print("=" * 62)
    print("进化预算 evolve_budget")
    print("=" * 62)

    real_path = EB.PATH
    tmp = os.path.join(tempfile.gettempdir(), "_evolve_budget_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    EB.PATH = tmp
    try:
        # ---- 1. 账本语义 ----
        st = EB.status()
        check("空账本：三类消耗都是 0",
              all(st["spent"][k] == 0 for k in EB.KINDS), repr(st["spent"]))
        check("空账本：上限都是正数",
              all(st["limits"][k] > 0 for k in EB.KINDS), repr(st["limits"]))
        check("空账本：没有一项被判定用完", not st["exhausted"], repr(st["exhausted"]))

        g = EB.gate("files", 3)
        check("额度够时放行", g["ok"] is True, repr(g))
        check("放行时不消耗（先问后扣）", EB.status()["spent"]["files"] == 0)

        EB.spend("files", 3, why="测试")
        check("spend 后累加", EB.status()["spent"]["files"] == 3)

        lim = EB.status()["limits"]["files"]
        g2 = EB.gate("files", lim)                 # 再要一整个上限 → 必然不够
        check("额度不够时拒绝", g2["ok"] is False, repr(g2))
        check("拒绝原因写明还剩多少、上限多少",
              "还剩" in (g2.get("why") or "") and "上限" in (g2.get("why") or ""),
              repr(g2.get("why")))
        check("拒绝原因给出出路（明天重置或手动）",
              "明天" in (g2.get("why") or "") and "手动" in (g2.get("why") or ""),
              repr(g2.get("why")))

        gm = EB.gate("files", 999, manual=True)
        check("手动触发永远放行", gm["ok"] is True, repr(gm))

        check("未知预算类型被拒绝", EB.gate("xxx", 1)["ok"] is False)

        # ---- 2. 跨天重置 ----
        d = EB.load()
        d["date"] = "2000-01-01"
        d["limits"]["files"] = 3            # 用户调过的上限
        EB.save(d)
        st2 = EB.status()
        check("跨天：消耗归零", all(st2["spent"][k] == 0 for k in EB.KINDS),
              repr(st2["spent"]))
        check("跨天：用户调过的上限保留", st2["limits"]["files"] == 3,
              repr(st2["limits"]))
        check("跨天：date 变成今天", st2["date"] != "2000-01-01", st2["date"])

        # ---- 3. 坏文件不炸 ----
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("{这不是 json")
        st3 = EB.status()
        check("账本读坏 → 按空账处理且不抛异常",
              all(st3["spent"][k] == 0 for k in EB.KINDS), repr(st3["spent"]))
        check("账本读坏 → 留下可读 note", bool(st3.get("note")), repr(st3.get("note")))

        # ---- 4. 上限可调 ----
        EB.set_limits(restarts=2)
        check("set_limits 生效", EB.status()["limits"]["restarts"] == 2,
              repr(EB.status()["limits"]))
        check("set_limits 忽略未知键", "bogus" not in EB.status()["limits"])

        # ---- 5. 摘要可读 ----
        txt = EB.brief()
        check("摘要含三类名称",
              all(x in txt for x in ("改进计划", "改动文件", "自动重启")), txt[:160])
        check("摘要带日期", EB.status()["date"] in txt)

        # ---- 6. 真接线（源码里必须真的在调）----
        wiring = [
            ("backend/selfloop.py", ['_eb.gate("plans"', '_eb2.spend("plans"']),
            ("backend/iter_runner.py", ['_eb.spend("files"', '_eb2.spend("restarts"']),
            ("backend/scheduler.py", ['_eb.gate("plans"', '_eb2.spend("plans"']),
        ]
        for rel, frags in wiring:
            src = ""
            try:
                with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
                    src = fh.read()
            except Exception as e:                       # noqa: BLE001
                check("能读 %s" % rel, False, str(e)[:120])
                continue
            for frag in frags:
                check("%s 里有 %s" % (os.path.basename(rel), frag), frag in src)
    finally:
        EB.PATH = real_path
        try:
            os.remove(tmp)
        except OSError:
            pass

    # 真实账本不该被这次测试写脏
    check("测试没有写真实账本",
          not os.path.exists(real_path) or EB.load().get("date") == EB._today())

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
