# -*- coding: utf-8 -*-
"""回归：只要是改过代码的都要记账 + 四环节体系健康（2026-11-25 用户要求）。

用户原话：「不只是自动迭代要记账，我主动要求的迭代也需要记账，只要是改过代码
的都要记，还要确保反思以及日志还有跑分全面正常体系化运作。」

改动前的真实缺陷（查证过，不是猜的）：
  · selfdev_write / selfdev_patch / selfdev_insert_after 成功后**什么账都不记**；
  · 即使记（patch 锚点未命中那条），写的也是会话账本
    data/actlog/<session>.jsonl，而 RSI 读的是 data/self_iterate/actlog.jsonl
    —— 两本账不通；
  · 实测：11-25 改了 11 个文件，RSI 账本 0 条新增，综合分 6 个点全是 89.9。

本测试锁住：
  ① log_change 把改动写进 self_iterate 账本（RSI 的数据源），字段齐全；
  ② 失败改动也记（result=fail），成功率才有真实分母；
  ③ _log_act 带 src：用户要求的记 manual、后台迭代记 auto；
  ④ tools.py 四个自开发工具分支都调了 _code_ledger；
  ⑤ pipeline_health 四个环节（反思/日志/采样/跑分）都有状态；
  ⑥ 新增的 /api/selfiter/pipeline 路由真的注册了。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

# ⚠️ 必须在 import backend.self_iterate 之前设：_DATA 在导入时就定好了
_TMP = tempfile.mkdtemp(prefix="mm_ledger_")
os.environ["MM_DATA_DIR"] = _TMP

from backend import self_iterate as si                    # noqa: E402

_ok = 0
_fail = []


def chk(cond, name, extra=""):
    global _ok
    if cond:
        _ok += 1
    else:
        _fail.append("%s%s" % (name, ("｜" + str(extra)) if extra else ""))


def _rows():
    # ⚠️ 路径真相：self_iterate._DATA = config.data("data")/"self_iterate"
    #   → MM_DATA_DIR 之下还有一层 data/（实测踩过：少这层就读成 0 条）
    p = os.path.join(_TMP, "data", "self_iterate", "actlog.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    for ln in open(p, encoding="utf-8").read().splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:                            # noqa: BLE001
                pass
    return out


def main():
    # ---- ① log_change 写进 self_iterate 账本 ----
    n0 = len(_rows())
    r = si.log_change(tool="selfdev_patch", rels=["backend/tools.py"],
                      ok=True, note="补记账", detail="改了 3 行", src="selfdev")
    rows = _rows()
    chk(r is True, "① log_change 返回 True")
    chk(len(rows) == n0 + 1, "① 账本真的多了一条", "%d -> %d" % (n0, len(rows)))
    last = rows[-1] if rows else {}
    chk(last.get("kind") == "code_change", "① kind=code_change", last.get("kind"))
    chk(last.get("src") == "selfdev", "① src=selfdev", last.get("src"))
    chk(last.get("tool") == "selfdev_patch", "① tool 记录正确", last.get("tool"))
    chk(last.get("files") == ["backend/tools.py"], "① files 记录正确",
        last.get("files"))
    chk(last.get("result") == "ok", "① result=ok", last.get("result"))
    chk(bool(last.get("ts")), "① 带时间戳", last.get("ts"))
    chk(last.get("probe") is False, "① 显式标成非探针", last.get("probe"))

    # ---- ② 失败改动也要记 ----
    si.log_change(tool="selfdev_write", rels=["backend/x.py"], ok=False,
                  detail="语法错误被拦下", src="selfdev")
    last = _rows()[-1]
    chk(last.get("result") == "fail", "② 失败记成 fail", last.get("result"))
    chk("语法错误" in str(last.get("detail")), "② 失败原因落盘", last.get("detail"))

    # ---- ③ _log_act 的 src：manual / auto ----
    si._log_act({"id": "p1", "_src": "manual"},
                {"id": "a1", "title": "用户要求改的", "files": ["backend/a.py"]},
                True, "用户主动要求")
    last = _rows()[-1]
    chk(last.get("src") == "manual", "③ 用户要求记 manual", last.get("src"))
    chk(last.get("result") == "ok", "③ 结果 ok", last.get("result"))

    si._log_act({"id": "p2"}, {"id": "a2", "title": "后台自己改的",
                               "files": ["backend/b.py"]}, True, "自动迭代")
    last = _rows()[-1]
    chk(last.get("src") == "auto", "③ 后台迭代记 auto", last.get("src"))

    # 两种来源都在账本里 —— RSI 的样本是全量，不再只有自动迭代
    srcs = set(x.get("src") for x in _rows() if x.get("kind") != "code_change")
    chk("manual" in srcs and "auto" in srcs, "③ 账本里两种来源都有", srcs)

    # ---- ④ tools.py 四个分支都接上 _code_ledger ----
    tp = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8",
              errors="replace").read()
    chk("def _code_ledger(" in tp, "④ 定义了 _code_ledger")
    chk("self_iterate as _si" in tp and "_si.log_change(" in tp,
        "④ 转发给 self_iterate.log_change")
    for t in ("selfdev_write", "selfdev_patch", "selfdev_insert_after",
              "selfdev_apply"):
        chk('_code_ledger("%s"' % t in tp, "④ %s 分支已接账本" % t)

    # ---- ⑤ pipeline_health 四环节 ----
    h = si.pipeline_health()
    chk(bool(h.get("ok")), "⑤ pipeline_health 返回 ok")
    items = h.get("items") or {}
    for k, zh in (("reflect", "反思"), ("actlog", "日志"),
                  ("rsi", "采样"), ("score", "跑分")):
        it = items.get(k) or {}
        chk(it.get("zh") == zh, "⑤ 有「%s」这一项" % zh, list(items.keys()))
        chk("state" in it and "last" in it and "age_min" in it,
            "⑤「%s」字段齐全" % zh, it)
    # 刚写过账本 → 日志环节必须是 ok（不许把刚发生的判成过期）
    chk((items.get("actlog") or {}).get("state") == "ok",
        "⑤ 日志环节刚写过就是 ok", (items.get("actlog") or {}).get("state"))
    chk(int((items.get("actlog") or {}).get("n24") or 0) >= 3,
        "⑤ 24h 计数能数到刚写的几条")
    chk(isinstance(h.get("summary"), str) and h.get("summary"),
        "⑤ 有一句中文结论", h.get("summary"))

    # ---- ⑥ 路由注册 ----
    mp = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8",
              errors="replace").read()
    chk('@app.get("/api/selfiter/pipeline")' in mp, "⑥ pipeline 路由已注册")
    chk("_si.pipeline_health()" in mp, "⑥ 路由真的调了 pipeline_health")


try:
    main()
except Exception as e:                                  # noqa: BLE001
    import traceback
    traceback.print_exc()
    _fail.append("异常：%s: %s" % (type(e).__name__, e))

print("通过 %d / 失败 %d" % (_ok, len(_fail)))
for f in _fail:
    print("  ✗", f)
sys.exit(0 if not _fail else 1)
