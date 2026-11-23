# -*- coding: utf-8 -*-
"""节流（min_gap_sec）自检 —— 2026-11-23「反思节流 90 → 300 秒」的落地验证。

为什么要留这个脚本：节流是「跑错了也看不出来」的那种参数 —— 默认值改对了、
但存量 data/selfloop.json 里还写着老值 90，实际行为就跟没改一样。所以这里
把三条容易被搞坏的性质钉成可重复执行的断言：
  ① DEFAULTS["min_gap_sec"] == 300；
  ② 老数据里显式写死的 90 会被一次性迁移成 300，且用户之后自己调的值
     不会被二次覆盖（_migrate + schema）；
  ③ 5 分钟窗口内自动触发一律让路（100 / 299 秒被挡、301 秒放行），
     而手动「立即反思」（think_now / source="manual"）永远放行。

安全性：不碰 data/ 真文件（_path 指到临时目录）、不调模型（线程换成假的、
只记录「有没有真的开跑」）。直接 `py -3 backend/_tmp_gapcheck.py` 即可。
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import selfloop as sl                      # noqa: E402

tmp = Path(tempfile.mkdtemp())
state = tmp / "selfloop.json"
state.write_text(json.dumps({
    "enabled": True, "min_gap_sec": 90, "daily_plans": 12,
    "backlog_gate": 0, "boot_gap_sec": 0, "last_at": 0,
}), encoding="utf-8")
sl._path = lambda: state
sl._BACKLOG_CACHE["at"] = time.time()
sl._BACKLOG_CACHE["n"] = 0

ok = []


def chk(name, cond, extra=""):
    ok.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (("  <- " + str(extra)) if extra else ""))


# ---- ① 默认值 ----
chk("DEFAULTS.min_gap_sec == 300", sl.DEFAULTS["min_gap_sec"] == 300, sl.DEFAULTS["min_gap_sec"])

# ---- ② 老落盘数据（显式 90）被一次性升级成 300 ----
c = sl.cfg()
chk("老数据 90 迁移成 300", c.get("min_gap_sec") == 300, c.get("min_gap_sec"))
chk("迁移标记已写入", int(c.get("schema") or 0) == sl._SCHEMA, c.get("schema"))

# ---- ③ 用户自己显式调回 90 之后，不会再被迁移覆盖 ----
c["min_gap_sec"] = 90
sl._save(c)
chk("用户显式 90 不被二次覆盖", sl.cfg().get("min_gap_sec") == 90, sl.cfg().get("min_gap_sec"))

# 复位成默认 300，开始测节流
d = sl.cfg()
d["min_gap_sec"] = 300
d["last_at"] = 0
sl._save(d)

# 假线程：只记录「真的开跑了」，不执行 _cycle（否则会真调模型）
started = []


class _FakeThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.args = args

    def start(self):
        started.append(self.args)


sl.threading = types.SimpleNamespace(Thread=_FakeThread)
sl._BOOT_TS[0] = time.time() - 10 ** 6       # 跳过启动冷却
sl._BUSY[0] = False

USER = "帮我把设置抽屉的节流参数改成五分钟一次"
ASST = "好的，已经改了。"

# ---- ④ 100 秒后（旧的 90 秒会放行）自动触发必须被挡 ----
sl._LAST_AT[0] = time.time() - 100
started.clear()
r = sl._maybe_think("验证-100秒", USER, source="chat")
print("  100 秒后自动触发 ->", r)
chk("100 秒时自动触发被节流拦下", r.get("skipped") and not started, r)
chk("拦截原因写明需 300 秒", "需 300" in str(r.get("skipped")), r.get("skipped"))

# ---- ⑤ 299 秒仍被挡 ----
sl._LAST_AT[0] = time.time() - 299
started.clear()
r = sl._maybe_think("验证-299秒", USER, source="chat")
chk("299 秒时自动触发仍被拦下", r.get("skipped") and not started, r)

# ---- ⑥ 301 秒放行（说明只是拉长间隔，没变成永不触发）----
sl._LAST_AT[0] = time.time() - 301
started.clear()
r = sl._maybe_think("验证-301秒", USER, source="chat")
chk("301 秒时自动触发放行", r.get("started") and started, r)

# ---- ⑦ 手动「立即反思」在节流窗口内仍放行 ----
sl._BUSY[0] = False
sl._LAST_AT[0] = time.time()          # 刚跑完，自动触发铁定会被挡
started.clear()
blocked = sl._maybe_think("验证-刚跑完自动", "再给我加一个导出按钮吧，现在没地方导出", source="chat")
chk("刚跑完 0 秒时自动触发被挡", blocked.get("skipped") and not started, blocked)
started.clear()
m = sl.think_now("手动：我现在就想让它想一次")
print("  手动触发 ->", m)
chk("手动触发 think_now 立刻放行", m.get("started") and started, m)
chk("手动触发走 manual 线程", started and started[-1][2] == "manual", started[-1] if started else None)

# 手动按钮路径就算走 _maybe_think(source="manual") 也该放行
sl._BUSY[0] = False
sl._LAST_AT[0] = time.time()
started.clear()
r = sl._maybe_think("验证-手动", USER, source="manual")
chk("source=manual 穿过节流放行", r.get("started") and started, r)

print("\n%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
