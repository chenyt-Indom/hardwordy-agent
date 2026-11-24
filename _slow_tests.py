# -*- coding: utf-8 -*-
"""顺序跑三个慢测试（每个给足 6 分钟），日志落盘。跑完即删。"""
import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["test_loop.py", "test_run_timeout.py", "test_memory_extract.py"]

bad = []
for f in FILES:
    print("\n" + "=" * 60)
    print("跑 %s（最多 360 秒）" % f)
    print("=" * 60)
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, f], cwd=ROOT, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=360)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        code = p.returncode
    except subprocess.TimeoutExpired:
        code, out, err = -1, "", "超时"
    print("耗时 %.1fs  exit=%s" % (time.time() - t0, code))
    tail = (out or "").strip().splitlines()
    for ln in tail[-14:]:
        print("  | " + ln[:170])
    if code != 0:
        bad.append(f)
        for ln in (err or "").strip().splitlines()[-8:]:
            print("  ! " + ln[:170])

print("\n" + "=" * 60)
print("三个慢测试：通过 %d / 失败 %d" % (len(FILES) - len(bad), len(bad)))
if bad:
    print("失败：" + ", ".join(bad))
