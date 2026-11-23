# -*- coding: utf-8 -*-
"""探针校准：每条教训的 probe 必须指向**真实存在**的修复代码。

为什么需要这个测试（2026-11-24）：
  `跑分退步（低于上一版）` 的探针原来指向 `iter_runner.py` 的 `static_first`
  —— 那和「跑分退步」不是同一个问题，真修复在 `scoregate.py` 的噪声带判定。
  探针指错文件 → 恒命中 → 这条教训永远显示「已修」，
  实际它反复复发（×7，教训库里最高频的一条）。**假阳性比没检查更危险。**

本测试守住两条：
  ① 每个 needle 必须在对应文件里真的存在（否则该教训永远判 pending = 假阴性）；
  ② 「跑分退步」必须指向 scoregate 的 _verdict_of（本次修复的回归测试）。

跑法：py -3 -m backend._feature_tests.test_probe_calibration
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import lessonloop as L  # noqa: E402


def run():
    ok = []
    probes = getattr(L, "PROBES", {}) or {}
    assert probes, "PROBES 是空的 —— 所有教训都会变成 unprobed"

    # ① 每条探针的文件与锚点都必须真实存在
    bad = []
    for kind, pr in probes.items():
        try:
            rel, needle = pr
        except Exception:
            bad.append("%s：探针格式不对（应为 (文件, 锚点)）" % kind)
            continue
        path = os.path.join(getattr(L, "_PROJECT_ROOT", ROOT),
                            str(rel).replace("/", os.sep))
        if not os.path.isfile(path):
            bad.append("%s → 文件不存在：%s" % (kind, rel))
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                body = f.read()
        except OSError as e:
            bad.append("%s → 读不到 %s：%s" % (kind, rel, e))
            continue
        if not needle or needle not in body:
            bad.append("%s → %s 里找不到锚点 %r" % (kind, rel, needle))
    assert not bad, "探针失效（这些教训永远判 pending）：\n  " + "\n  ".join(bad)
    ok.append("%d 条探针全部指向真实存在的代码" % len(probes))

    # ② 关键回归：「跑分退步」必须指向真正的修复逻辑
    pr = probes.get("跑分退步（低于上一版）")
    assert pr, "「跑分退步」没有登记探针"
    rel, needle = pr
    assert rel == "backend/scoregate.py", \
        "「跑分退步」的探针又指错文件了：%s（真修复在 scoregate.py）" % rel
    assert "_verdict_of" in needle, \
        "「跑分退步」应指向噪声带判定 _verdict_of，实际锚点：%r" % needle
    ok.append("「跑分退步」指向 scoregate._verdict_of")

    # ③ 每条探针都要能真的判定出结果（三态里不能是「读不到」）
    errs = []
    for kind, pr in probes.items():
        rel, needle = pr
        hit = L._probe_hit(rel, needle)
        if hit is None:
            errs.append("%s → %s 读不到" % (kind, rel))
        elif hit is not True:
            errs.append("%s → %s 里没命中 %r" % (kind, rel, needle))
    assert not errs, "探针判定异常：\n  " + "\n  ".join(errs)
    ok.append("每条探针都判得出「命中」")

    # ④ 提醒：教训库里若有未登记探针的 kind，那条永远无法自动判定
    try:
        kinds = {str(it.get("kind") or "").strip() for it in L.load()}
        kinds.discard("")
        un = sorted(k for k in kinds if k not in probes)
        if un:
            print("  [提示] 这些教训 kind 还没有探针（会单列 unprobed，不算进落地率）：")
            for k in un:
                print("         - " + k)
    except Exception as e:                                  # noqa: BLE001
        print("  [提示] 读教训库失败（不影响本测试）：%s" % e)

    for line in ok:
        print("  [OK] " + line)
    print("全部通过（%d 项）" % len(ok))
    return 0


if __name__ == "__main__":
    sys.exit(run())
