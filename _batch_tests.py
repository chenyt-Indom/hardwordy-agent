# -*- coding: utf-8 -*-
"""临时批跑器：把根目录 test_*.py 按索引区间串行跑一遍（跑完即删）。

用法：python _batch_tests.py 起始索引 结束索引 [单个超时秒]
"""
import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.dirname(os.path.abspath(__file__))

_raw = " ".join(sys.argv[1:]).split()
a = int(_raw[0]) if len(_raw) > 0 else 0
b = int(_raw[1]) if len(_raw) > 1 else 4
per = int(_raw[2]) if len(_raw) > 2 else 45

files = sorted(f for f in os.listdir(ROOT) if f.startswith("test_") and f.endswith(".py"))
sel = files[a:b]
print("本批 %d 个（总 %d）：%s" % (len(sel), len(files), ", ".join(sel)))

ok, bad, slow = [], [], []
t_all = time.time()
for f in sel:
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, f], cwd=ROOT, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=per)
        code = p.returncode
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, out, err = -1, "", "超时"
    dt = time.time() - t0
    if code == 0:
        ok.append(f)
        print("PASS  %-38s %5.1fs" % (f, dt))
    else:
        bad.append((f, code, out, err))
        print("FAIL  %-38s %5.1fs  exit=%s" % (f, dt, code))
        if code == -1:
            slow.append(f)
        tail = (out or "").strip().splitlines()[-6:]
        for ln in tail:
            print("        | " + ln[:160])
        e = (err or "").strip().splitlines()[-4:]
        for ln in e:
            print("        ! " + ln[:160])

print("\n本批通过 %d / 失败 %d，用时 %.1fs" % (len(ok), len(bad), time.time() - t_all))
if bad:
    print("失败：" + ", ".join(x[0] for x in bad))
sys.exit(0 if not bad else 1)
