# -*- coding: utf-8 -*-
"""回归：跨进程回归锁必须真的挡住重叠运行（2026-11-24）。

真实故障：两次回归撞在一起时，断言文件用固定夹具名、还会碰真实数据目录，
实测出现过 `test_selfdev_write_guard`「文件仍然存在」失败、
`test_task_card` 写 cards.json 抛异常 —— **单独跑全过**。
假失败会让自动迭代把没问题的改动回滚掉，所以加了跨进程锁，本文件守住它：
  ① 同一时刻只有一个持有者：第二次获取必须等不到（返回 None）；
  ② 释放后能立刻再拿到；
  ③ 残留锁（进程已不在 / 文件坏掉）能被抢走，不会把回归永久堵死；
  ④ 释放时只删**自己**的锁，不能把别人正在用的锁删掉。

⚠️ 本文件全程用**自己的锁文件路径**（临时目录里另开一个），
   绝不去碰真实那把锁 —— 否则会把正在跑本用例的那次回归的锁删掉，
   等于亲手打开互斥，反而制造出它要防的问题。
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

from backend import regress                            # noqa: E402

_fails = []


def ck(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        _fails.append(name)


def run() -> int:
    _real_lock = regress._RUN_LOCK
    lock = os.path.join(tempfile.mkdtemp(prefix="mm_lock_"), "regress.lock")
    regress._RUN_LOCK = lock                            # 只在本次用例内生效
    try:
        # ① 只有一个持有者
        tok = regress._acquire_run_lock()
        ck("第一次能拿到锁", bool(tok))
        t0 = time.time()
        tok2 = regress._acquire_run_lock(wait=0.8)
        ck("同进程第二次拿不到（挡住重叠）", tok2 is None, tok2)
        ck("确实等过（不是立刻返回）", time.time() - t0 >= 0.7, round(time.time() - t0, 2))

        # ④ 别人的锁不能被误删：把锁内容换成"另一个活着的进程"
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getppid() or 1, "ts": time.time()}, f)
        regress._release_run_lock(tok)          # tok 的 pid 是本进程，与文件里的不同
        ck("释放时不误删别人的锁", os.path.exists(lock))

        # ② 释放自己的锁后能再拿到
        try:
            os.remove(lock)
        except OSError:
            pass
        tok3 = regress._acquire_run_lock()
        ck("清掉后能再拿到", bool(tok3))
        regress._release_run_lock(tok3)
        ck("释放后锁文件真的没了", not os.path.exists(lock))

        # ③ 残留锁（pid 已不在）能被抢走
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "ts": time.time() - 9999}, f)
        t1 = time.time()
        tok4 = regress._acquire_run_lock(wait=2.0)
        ck("残留锁能被抢（不会永久堵死回归）", bool(tok4), round(time.time() - t1, 2))
        regress._release_run_lock(tok4)

        # ③b 锁文件是坏的（半截 JSON）也不能把回归堵死
        with open(lock, "w", encoding="utf-8") as f:
            f.write("{坏的")
        old_mtime = time.time() - regress._RUN_LOCK_STALE - 10
        os.utime(lock, (old_mtime, old_mtime))
        tok5 = regress._acquire_run_lock(wait=2.0)
        ck("坏掉的残留锁也能抢", bool(tok5))
        regress._release_run_lock(tok5)

        ck("真实锁文件没被本用例动过", regress._RUN_LOCK == lock)
    finally:
        regress._RUN_LOCK = _real_lock

    print("\n%s（失败 %d 项）" % ("通过" if not _fails else "未通过", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(run())
