# -*- coding: utf-8 -*-
"""评分闸门的退步判定：噪声带内不判退步。

背景（2026-11-24 实测）：
  原来 `delta < 0` 就判 regressed。实测 scoregate.json 的 5 次 regressed 里，
  4 次跌幅只有 0.44~0.5 分，且同一版本 24 秒内来回跳（−0.5 判退步 → +0.5 判进步）。
  每次误判都会沉淀「跑分退步」教训并起自动修复（最多 3 轮）——教训库里
  最高频的那条 ×7 就是这么来的。

跑法：py -3 -m backend._feature_tests.test_score_noise_band
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import scoregate as G  # noqa: E402


def run():
    ok = []
    v = G._verdict_of

    # ① 实测噪声（0.44~0.5 分）必须判持平 —— 这是本次修复的核心
    for d in (-0.5, -0.44, -0.5, -0.5, 0.5, 0.0):
        assert v(d) == "flat", "%.2f 分波动仍被判成 %s（应持平）" % (d, v(d))
    ok.append("实测的 ±0.5 分噪声判持平（原来判退步）")

    # ② 真实退步仍要抓到（实测有过 −7.0）
    assert v(-7.0) == "regressed", "真实退步 −7.0 没被判退步"
    assert v(-1.01) == "regressed", "刚过噪声带的跌幅没被判退步"
    ok.append("带外的真实退步仍判退步（−7.0 / −1.01）")

    # ③ 进步同理（带内算持平，别把 0.5 分当进步去庆祝）
    assert v(7.0) == "improved", "真实进步 +7.0 没被判进步"
    assert v(1.01) == "improved", "刚过噪声带的涨幅没被判进步"
    ok.append("带外的真实进步仍判进步")

    # ④ 边界值：噪声带本身算持平（含端点）
    assert v(-G._NOISE_BAND) == "flat", "跌幅正好等于噪声带应判持平"
    assert v(G._NOISE_BAND) == "flat", "涨幅正好等于噪声带应判持平"
    ok.append("边界值（±%.1f）判持平" % G._NOISE_BAND)

    # ⑤ 没有上一次分数时不瞎判
    assert v(None) == "unknown", "delta=None 应判 unknown"
    ok.append("无基准分时判 unknown")

    # ⑥ 给模型看的文案必须解释「为什么 -0.5 算持平」
    m = G._msg({"prev": 154.59, "total": 154.09, "delta": -0.5,
                "verdict": "flat"})
    assert "持平" in m, "文案里没写持平：%r" % m[:120]
    assert "噪声带" in m, "没解释噪声带，用户看到 -0.5 说持平会以为算错：%r" % m[:160]
    ok.append("文案解释了噪声带")

    # ⑦ 真退步的文案不能被噪声带说明污染
    m2 = G._msg({"prev": 160.0, "total": 153.0, "delta": -7.0,
                 "verdict": "regressed"})
    assert "退步" in m2, "真退步文案没写退步"
    assert "噪声带" not in m2, "真退步不该带噪声带说明"
    ok.append("真退步文案干净")

    # ⑧ 两处判定口径一致：闸门与后台自动评分必须用同一个函数
    import inspect
    src = inspect.getsource(G)
    assert "if _delta < -_NOISE_BAND:" in src, \
        "后台自动评分那条路没同步用噪声带（两处口径会不一致）"
    assert "verdict = _verdict_of(delta)" in src, \
        "闸门判定没走 _verdict_of"
    ok.append("两处判定口径一致（都用噪声带）")

    for line in ok:
        print("  [OK] " + line)
    print("全部通过（%d 项）" % len(ok))
    return 0


if __name__ == "__main__":
    sys.exit(run())
