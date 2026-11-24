# -*- coding: utf-8 -*-
"""待批准积压闸门的口径回归 —— 不跑模型、不联网、不改用户数据。

为什么要有（2026-11-24 实测）：
  用户问「积压 172 条为什么降不下来、自动迭代是不是不干活」。
  查出来两个真问题：
    ① 172 条里 107 条是同一件事的重复提案（模型直接给的 actions 从不和历史比对）；
    ② 闸门数的是**全部历史 pending**，而历史存量根本批不完
       → backlog_gate（30 条）永久封死对话触发，只剩「死锁保险」一天放行一次。
  这里锁住 ② 的修法：
    · 闸门按 backlog_days（默认 7 天）统计最近新增，不是三周前的存货；
    · 阈值 30 → 80；
    · 老落盘配置里的 30 要能被 schema 迁移升上来（否则改 DEFAULTS 没用）。

跑法：python backend/_feature_tests/test_backlog_scope.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

from backend import selfloop as SL                 # noqa: E402

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
    print("待批准积压闸门口径回归")
    print("=" * 62)

    # ---------- 1) 默认值 ----------
    check("默认阈值是 80（不再是 30）",
          int(SL.DEFAULTS.get("backlog_gate") or 0) == 80,
          "实际 %r" % (SL.DEFAULTS.get("backlog_gate"),))
    check("默认按最近 7 天统计",
          int(SL.DEFAULTS.get("backlog_days") or 0) == 7,
          "实际 %r" % (SL.DEFAULTS.get("backlog_days"),))

    # ---------- 2) schema 迁移：老配置里的 30 必须被升上来 ----------
    old = {"schema": 2, "backlog_gate": 30, "min_gap_sec": 90}
    changed = SL._migrate(old)
    check("老配置被识别为需要迁移", changed is True)
    check("迁移后 backlog_gate = 80（不是 30）",
          int(old.get("backlog_gate") or 0) == 80,
          "实际 %r" % (old.get("backlog_gate"),))
    check("迁移后 schema 记成最新版",
          int(old.get("schema") or 0) == int(SL._SCHEMA))
    # 迁移只做一次：再跑一遍不能把用户手调的值覆盖掉
    old2 = {"schema": int(SL._SCHEMA), "backlog_gate": 120}
    SL._migrate(old2)
    check("已是新版就不再覆盖用户手调值",
          int(old2.get("backlog_gate") or 0) == 120)

    # ---------- 3) _backlog 口径 ----------
    try:
        n_all = SL._backlog(0)
        n_7 = SL._backlog(7)
        ok = True
    except Exception as e:                              # noqa: BLE001
        ok = False
        check("_backlog 能跑", False, "%s: %s" % (type(e).__name__, e))
    if ok:
        check("_backlog 能跑", True)
        check("返回的是整数", isinstance(n_all, int) and isinstance(n_7, int))
        check("全量口径 >= 最近 7 天口径（不能倒挂）", n_all >= n_7,
              "all=%r 7d=%r" % (n_all, n_7))
        check("最近 7 天口径不为负", n_7 >= 0)

    # ---------- 4) 调用处真的用上了新口径 ----------
    try:
        with open(os.path.join(_ROOT, "backend", "selfloop.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
    except Exception as e:                              # noqa: BLE001
        src = ""
        check("能读 selfloop.py", False, str(e)[:100])
    check("_backlog 里把 days 透传给了统计函数",
          "list_pending_actions(days=days)" in src)
    check("缓存带 days 键（换口径不会读到旧数）",
          '"days"' in src and "_BACKLOG_CACHE.get(\"days\")" in src)

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
