"""清理 _feature_tests 下的自迭代暂存垃圾：移到 _quarantine/20261118_purge/（可恢复）。

三类：
  1. _pending_*.py  —— 自迭代快照暂存（54 个）
  2. 无前缀中文名 .py —— 早期验收暂存脚本（14 个），只有 test_*.py 是真测试
  3. _quarantine/ 下已隔离的旧文件（不删目录本身）
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "_quarantine", "20261118_purge")
os.makedirs(DEST, exist_ok=True)

moved = []
kept = []

for f in sorted(os.listdir(HERE)):
    p = os.path.join(HERE, f)
    if not os.path.isfile(p) or not f.endswith(".py"):
        continue
    if f.startswith(("_pending_", "_tmp_")):
        moved.append(f)
    elif not f.startswith("test_"):
        moved.append(f)
    else:
        kept.append(f)

# _quarantine 下的旧隔离文件也一并归到 purge 子目录
q = os.path.join(HERE, "_quarantine")
for f in sorted(os.listdir(q)):
    p = os.path.join(q, f)
    if os.path.isfile(p):
        moved.append(("_quarantine", f))

n_ok = 0
for item in moved:
    if isinstance(item, tuple):
        src = os.path.join(HERE, "_quarantine", item[1])
        dst = os.path.join(DEST, item[1])
    else:
        src = os.path.join(HERE, item)
        dst = os.path.join(DEST, item)
    if os.path.exists(dst):
        base, ext = os.path.splitext(dst)
        i = 1
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        dst = f"{base}_{i}{ext}"
    try:
        shutil.move(src, dst)
        n_ok += 1
    except Exception as e:
        print(f"  FAIL {item}: {e}")

# __pycache__ 顺手清（可再生）
for d in (HERE, os.path.join(HERE, "_quarantine")):
    pc = os.path.join(d, "__pycache__")
    if os.path.isdir(pc):
        shutil.rmtree(pc, ignore_errors=True)
        print(f"  清掉 {pc}")

left = [f for f in os.listdir(HERE)
        if f.endswith(".py") and not f.startswith("test_")]
print(f"移走 {n_ok} 个 -> {DEST}")
print(f"保留真测试 {len(kept)} 个: {kept}")
print(f"顶层剩余非 test_ 的 .py: {len(left)} 个 {left}")
print(f"顶层 .py 总数: {len([f for f in os.listdir(HERE) if f.endswith('.py')])}")
