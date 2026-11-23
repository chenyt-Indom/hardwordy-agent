# -*- coding: utf-8 -*-
"""「按来源决定是否自动重启」的离线回归。

用户 2026-11-23 原话：
  「自我迭代闭环也得分情况啊，其他的迭代是自动重启然后自动弹卡片没错，
    但是我跟你交流不是自我迭代啊，而是我要求你进行自我迭代啊」

背景：`execute_approved` 里原来只判断 `loop_auto_restart`，**完全不看来源**，
所以用户在界面上点「执行」、或在对话里让我跑迭代，改完 3 秒后进程也被换掉
（实测 20:26:31 写标记 → 20:26:36 新进程起来），人还在等回复就断了。

这里验四件事（不联网、不重启、不改任何代码）：
  1) 判定函数：manual / 空 → 不重启；auto / featradar / approved → 重启；
     设置里关掉开关 → 一律不重启；
  2) 自动重启那段代码真的调用了判定函数（不是又绕回去只看 loop_auto_restart）；
  3) execute_approved 的签名接受 origin，且收敛递归会把 origin 传下去；
  4) 三个真实调用点都透传了来源（main 的执行/复核、iter_runner 的 worker）。

跑法：python backend/_feature_tests/test_restart_by_origin.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import self_iterate as SI      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def _read(rel):
    p = os.path.join(_ROOT, rel)
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def main():
    print("=" * 62)
    print("按来源决定是否自动重启 · 回归")
    print("=" * 62)

    orig_settings = SI.load_settings

    # ---------- 1) 判定函数本身 ----------
    print("\n[1] 来源判定")
    SI.load_settings = lambda: {"loop_auto_restart": True,
                                "auto_restart_after_iter": True}
    check("manual（用户发起）→ 不自动重启",
          SI._origin_auto_restart_ok("manual")["ok"] is False)
    check("空来源 → 按 manual 处理（不重启）",
          SI._origin_auto_restart_ok("")["ok"] is False)
    check("None → 按 manual 处理（不重启）",
          SI._origin_auto_restart_ok(None)["ok"] is False)
    check("auto（后台自动迭代）→ 自动重启",
          SI._origin_auto_restart_ok("auto")["ok"] is True)
    check("featradar（推荐迭代）→ 自动重启",
          SI._origin_auto_restart_ok("featradar")["ok"] is True)
    check("approved（批准后二级迭代）→ 自动重启",
          SI._origin_auto_restart_ok("approved")["ok"] is True)
    check("大小写/空格不敏感（ AUTO ）→ 自动重启",
          SI._origin_auto_restart_ok(" AUTO ")["ok"] is True)
    check("不重启时必须给出原因（给用户看）",
          bool(SI._origin_auto_restart_ok("manual").get("why")))

    SI.load_settings = lambda: {"loop_auto_restart": False,
                                "auto_restart_after_iter": True}
    check("设置关了 loop_auto_restart → 连 auto 也不重启",
          SI._origin_auto_restart_ok("auto")["ok"] is False)

    SI.load_settings = lambda: {"loop_auto_restart": True,
                                "auto_restart_after_iter": False}
    check("设置关了 auto_restart_after_iter → 连 auto 也不重启",
          SI._origin_auto_restart_ok("auto")["ok"] is False)

    SI.load_settings = lambda: {"loop_auto_restart": True}   # 缺 key → 默认允许
    check("设置缺 auto_restart_after_iter 时按默认允许",
          SI._origin_auto_restart_ok("auto")["ok"] is True)

    SI.load_settings = orig_settings

    # ---------- 2) 自动重启那段真的用了判定函数 ----------
    print("\n[2] 闭环里的自动重启条件")
    src = _read("backend/self_iterate.py")
    check("自动重启前先算 _origin_auto_restart_ok(origin)",
          "_auto_why = _origin_auto_restart_ok(origin)" in src)
    check("条件里用的是判定结果，不是裸的 loop_auto_restart",
          'ok_n > 0 and _auto_why["ok"]' in src)
    check("不重启时把原因写进 loop.auto_restart（卡片能显示）",
          '"skipped": True, "why": _auto_why["why"]' in src)

    # ---------- 3) 签名 + 递归继承 ----------
    print("\n[3] execute_approved 的 origin 透传")
    check("签名接受 origin（默认 manual）",
          'force=None,\n                     origin="manual"' in src)
    check("收敛递归继承 origin（第二轮不会偷偷变成允许重启）",
          "_depth=_depth + 1, origin=origin" in src)

    # ---------- 4) 真实调用点 ----------
    print("\n[4] 调用点是否带上来源")
    m = _read("backend/main.py")
    check("/api/selfiter/execute 透传来源（默认 manual）",
          'origin=str(b.get("source") or "manual")' in m)
    check("/api/selfiter/force（人工复核）固定 manual",
          'origin="manual")' in m)
    ir = _read("backend/iter_runner.py")
    check("iter_runner worker 透传队列来源（默认 auto）",
          'origin=str(item.get("source") or "auto")' in ir)
    check("iter_runner 自己的 _should_auto_restart 仍在（manual 白名单外）",
          '_AUTO_RESTART_SOURCES = ("auto", "featradar", "approved")' in ir)
    check("execute_approved 已排程重启时 worker 不再排第二次（不连断两次）",
          "_exec_scheduled_restart = bool(" in ir
          and "if not _exec_scheduled_restart:" in ir)

    # ---------- 汇总 ----------
    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗ %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
