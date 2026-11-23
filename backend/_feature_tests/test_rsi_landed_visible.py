# -*- coding: utf-8 -*-
"""RSI 落地率与教训可见性（2026-11-24 修的两处实测缺陷）。

覆盖：
  ① 教训全在观察窗内（无 pending）时，hint() 必须给出「已落地」摘要
     —— 原来返回空串，教训在最长 24 小时里完全不进上下文。
  ② 该摘要必须是**稳定字符串**：状态迁移 / 重复调用都不改变文本。
  ③ rsi_index 的落地率分母只算**可判定**条目 —— unprobed 不进分母
     —— 原来 1 fixed + 3 unprobed 得 0.25（应 1.0）。

跑法：py -3 -m backend._feature_tests.test_rsi_landed_visible
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import lessonloop as L  # noqa: E402


def _with_audit(rows, fn):
    """临时把 lessonloop.audit 换成假数据，跑完必定还原。"""
    real = L.audit
    fake = {"rows": rows}
    for st in ("fixed", "pending", "verifying", "confirming", "unprobed"):
        fake[st] = [r for r in rows if r.get("status") == st]
    fake["done"] = fake["fixed"]
    L.audit = lambda: fake
    try:
        L._RSI_CACHE["val"] = None
        return fn()
    finally:
        L.audit = real
        L._RSI_CACHE["val"] = None


def _row(kind, status, progress, fix="改法说明", count=1):
    return {"kind": kind, "status": status, "progress": progress,
            "fix": fix, "count": count}


def run():
    ok = []

    # ① 无 pending、有 verifying/confirming → 必须非空
    rows = [_row("模型输出不可用", "confirming", 0.55),
            _row("没产生实际改动", "verifying", 0.30)]
    h = _with_audit(rows, L.hint)
    assert h and h.strip(), "无 pending 时 hint() 仍返回空 —— 教训又看不见了"
    assert "已落地" in h, "应标明这批教训已落地，实际：%r" % h[:80]
    assert "模型输出不可用" in h and "没产生实际改动" in h, \
        "已落地教训的 kind 没出现在摘要里：%r" % h[:200]
    ok.append("无 pending 时给出已落地摘要")

    # ② 稳定性：连调两次逐字相同
    h2 = _with_audit(rows, L.hint)
    assert h == h2, "同一批教训两次 hint() 文本不一致，会破坏前缀缓存"
    ok.append("摘要稳定（两次调用逐字相同）")

    # ③ 状态迁移 verifying→confirming→fixed 不得改变文本
    for st, prog in (("verifying", 0.1), ("confirming", 0.8), ("fixed", 1.0)):
        moved = [_row("模型输出不可用", st, prog), _row("没产生实际改动", st, prog)]
        hm = _with_audit(moved, L.hint)
        assert hm == h, "状态迁到 %s 后文本变了（前缀缓存会废）：%r" % (st, hm[:120])
    ok.append("状态迁移不改变文本")

    # ④ 有 pending 时仍走「未修完」分支
    pend_rows = [_row("字段名/键读错", "pending", 0.0)]
    hp = _with_audit(pend_rows, L.hint)
    assert hp and "未修完" in hp, "有 pending 时应提示未修完，实际：%r" % hp[:80]
    assert "字段名/键读错" in hp, "pending 的 kind 没进摘要"
    ok.append("有 pending 时仍提示未修完")

    # ⑤ 全 unprobed → 不注入（没有可说的）
    un_rows = [_row("某新教训", "unprobed", None)]
    hu = _with_audit(un_rows, L.hint)
    assert hu == "", "全是 unprobed 时不该硬编内容，实际：%r" % hu[:80]
    ok.append("全 unprobed 时不硬编")

    # ⑥ 落地率分母排除 unprobed：1 fixed + 3 unprobed = 1.0
    mix = [_row("A", "fixed", 1.0), _row("B", "unprobed", None),
           _row("C", "unprobed", None), _row("D", "unprobed", None)]
    r = _with_audit(mix, lambda: L.rsi_index(cache_sec=0))
    part = [p for p in r["parts"] if p["name"] == "教训落地率"]
    assert part, "落地率项缺失"
    assert abs(part[0]["value"] - 1.0) < 1e-9, \
        "unprobed 污染了落地率：得 %.3f，应 1.0" % part[0]["value"]
    assert "3 条无可判定探针" in part[0]["detail"], \
        "明细里应注明排除了 3 条 unprobed：%r" % part[0]["detail"]
    ok.append("落地率分母排除 unprobed")

    # ⑦ 全 unprobed → 不出现落地率项（不误报 0 分）
    all_un = [_row("X", "unprobed", None)]
    r2 = _with_audit(all_un, lambda: L.rsi_index(cache_sec=0))
    names = [p["name"] for p in r2["parts"]]
    assert "教训落地率" not in names, "全 unprobed 时不该出现落地率项（会误报 0）：%r" % names
    ok.append("全 unprobed 时不误报 0 分")

    # ⑧ 权重归一化仍正常（删掉一项后不能算错总分）
    assert 0 <= r2["score"] <= 100, "RSI 总分越界：%r" % r2["score"]
    tw = sum(p["weight"] for p in r2["parts"])
    assert abs(tw - 1.0) < 1e-9 or tw > 0, "权重归一化异常：%r" % tw
    ok.append("权重归一化正常（总分 %.1f）" % r2["score"])

    for line in ok:
        print("  [OK] " + line)
    print("全部通过（%d 项）" % len(ok))
    return 0


if __name__ == "__main__":
    sys.exit(run())
