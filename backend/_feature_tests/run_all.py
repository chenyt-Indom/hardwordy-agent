# -*- coding: utf-8 -*-
"""_feature_tests 全量验证入口（脚本式测试专用）

为什么不用 pytest（2026-11-24 实测）：
  1) 本目录 27 个测试文件**全部是脚本式** —— 整份代码在模块顶层执行，末尾
     `sys.exit(1 if fail else 0)`，没有一个是 `def test_xxx` 的 pytest 用例。
     它们的设计用法就是 `py -3 backend/_feature_tests/test_xxx.py`。
  2) 当前环境 Python 3.14 + 该版 pytest 的 capture 插件不兼容：任何文件都会在
     收尾时抛 `ValueError: I/O operation on closed file`，连 `--collect-only` 都崩。
     所以以前用 pytest 跑出来的「0 通过 / N 失败」是**假故障**，不是代码有问题。

用法：
  py -3 backend/_feature_tests/run_all.py                # 全跑
  py -3 backend/_feature_tests/run_all.py --start 10 --count 8
  py -3 backend/_feature_tests/run_all.py --only test_rsi
  py -3 backend/_feature_tests/run_all.py --list
退出码：全部通过 0，否则 1。
"""
import argparse
import os
import subprocess
import sys
import time

# ⚠️ 2026-11-24 实测：Windows 控制台默认 GBK，汇总行的「合计 / 通过」会变成
#   乱码（�ϼ� / ͨ��），看起来像程序坏了。统一按 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SELF = os.path.basename(os.path.abspath(__file__))


def _files():
    return sorted(f for f in os.listdir(HERE)
                  if f.startswith("test_") and f.endswith(".py") and f != SELF)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=0, help="0 = 到结尾")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--only", default="", help="文件名包含这段文字")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    files = _files()
    if a.only:
        files = [f for f in files if a.only in f]
    if a.list:
        for i, f in enumerate(files):
            print("%2d  %s" % (i, f))
        return 0
    if a.start:
        files = files[a.start:]
    if a.count:
        files = files[:a.count]

    ok, bad, slow = [], [], []
    t_all = time.time()
    for f in files:
        p = os.path.join(HERE, f)
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, p], cwd=ROOT, timeout=a.timeout,
                               capture_output=True, text=True, errors="replace")
            dt = time.time() - t0
            if r.returncode == 0:
                ok.append((f, dt))
                print("PASS  %-34s %5.1fs" % (f, dt))
            else:
                bad.append((f, r.returncode, dt, (r.stdout or "") + (r.stderr or "")))
                print("FAIL  %-34s %5.1fs  exit=%s" % (f, dt, r.returncode))
        except subprocess.TimeoutExpired:
            dt = time.time() - t0
            slow.append((f, dt))
            print("TIMEOUT %-32s %5.1fs" % (f, dt))

    print("\n" + "=" * 64)
    print("合计 %d 个文件，用时 %.1fs" % (len(files), time.time() - t_all))
    print("通过 %d / 失败 %d / 超时 %d" % (len(ok), len(bad), len(slow)))
    for f, rc, dt, out in bad:
        print("\n---- 失败：%s (exit=%s) 末尾 15 行 ----" % (f, rc))
        print("\n".join([l for l in out.splitlines() if l.strip()][-15:]))
    for f, dt in slow:
        print("超时：%s" % f)
    return 0 if not bad and not slow else 1


if __name__ == "__main__":
    sys.exit(main())
