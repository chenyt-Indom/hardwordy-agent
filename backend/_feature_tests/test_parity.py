# -*- coding: utf-8 -*-
"""行为基线快照（parity）的离线回归 —— 不联网、不改代码、不碰业务数据。

用户 2026-11-24 转述的专家意见（L3 三缺之一）：
  「没有 parity 断言集（改坏了查不出行为变化）」

这里验五件事：
  1) **可复现**：同一份代码连采两次，42 条指纹必须一模一样
     （否则基线天天自己变，报出来的全是假漂移）；
  2) **有覆盖**：目标 ≥ 8 个、输入 ≥ 40 条，且不许有空 target；
  3) **够灵敏**：把基线里任意一条输出改一个字符，compare 必须报出来
     —— 这是「改坏了查得出」的唯一证明；
  4) **不误报**：拿真实基线比当前代码，必须 ok=True；
  5) **坏环境不崩**：基线缺失 / 函数抛异常 / target 被删，都要给出
     可读结论，而不是抛异常把整轮验证带崩。

跑法：python backend/_feature_tests/test_parity.py
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import parity as P                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s  %s" % (name, detail))


def main():
    print("=" * 62)
    print("行为基线快照 parity")
    print("=" * 62)

    # ---- 1. 可复现 ----
    a = P.capture()
    b = P.capture()
    diff = []
    for n in a["targets"]:
        ca = a["targets"][n]["cases"]
        cb = b["targets"][n]["cases"]
        for i in range(min(len(ca), len(cb))):
            if ca[i]["hash"] != cb[i]["hash"]:
                diff.append("%s[%d]" % (n, i))
    check("连采两次 42 条指纹完全一致", not diff, "不一致：%r" % diff[:5])

    # ---- 2. 有覆盖 ----
    check("目标数 ≥ 8", len(a["targets"]) >= 8, "实际 %d" % len(a["targets"]))
    check("输入数 ≥ 40", a["cases"] >= 40, "实际 %d" % a["cases"])
    check("没有空的 target",
          all(blk.get("cases") for blk in a["targets"].values()),
          "空 target：%r" % [n for n, blk in a["targets"].items() if not blk.get("cases")])
    check("没有 import 失败被跳过", not a["skipped"], "跳过：%r" % a["skipped"])
    check("没有 case 意外抛异常",
          not [1 for blk in a["targets"].values() for c in blk["cases"] if c.get("exc")],
          "异常：%r" % [(n, c["exc"]) for n, blk in a["targets"].items()
                       for c in blk["cases"] if c.get("exc")][:3])

    # ---- 3. 够灵敏（改一个字必须报出来）----
    base = json.loads(json.dumps(a))          # 深拷贝，别动真实基线
    victim = "hardguard.asks_internal_prompt"
    before = base["targets"][victim]["cases"][0]["out"]
    base["targets"][victim]["cases"][0]["out"] = (not before)
    base["targets"][victim]["cases"][0]["hash"] = P._hash(not before)
    r = P.compare(baseline=base)
    hit = [c for c in (r.get("changed") or []) if c.get("target") == victim]
    check("改一条输出 → compare 报出漂移", bool(hit), "changed=%r" % (r.get("changed") or [])[:2])
    check("漂移报告里带「改前 / 改后」原文",
          bool(hit) and hit[0].get("before") is not None and hit[0].get("after") is not None,
          repr(hit[:1]))
    check("漂移时 ok=False", r.get("ok") is False)

    # ---- 3b. 少一条 case 也要报（目标被删/输入被删）----
    base2 = json.loads(json.dumps(a))
    base2["targets"][victim]["cases"].append(
        {"in": ["凭空多出来的一条"], "out": True, "hash": P._hash(True), "exc": ""})
    r2 = P.compare(baseline=base2)
    check("基线里多出一条 → 报 missing（不算静默通过）",
          any(m.get("target") == victim for m in (r2.get("missing") or [])),
          repr(r2.get("missing")[:2]))

    # ---- 4. 不误报（真实基线 vs 当前代码）----
    if os.path.exists(P.BASELINE_PATH):
        r3 = P.compare()
        check("真实基线比对当前代码 → 一条都没变", r3.get("ok") is True,
              P.as_text(r3)[:300])
        check("比对条数与采集条数一致",
              int(r3.get("checked") or 0) == a["cases"],
              "checked=%r cases=%r" % (r3.get("checked"), a["cases"]))
    else:
        check("真实基线存在（data/parity/baseline.json）", False, "文件不存在")

    # ---- 5. 坏环境不崩 ----
    missing = P.compare(path=os.path.join(tempfile.gettempdir(), "__no_such_parity__.json"))
    check("基线缺失 → missing_baseline 而不是抛异常",
          missing.get("missing_baseline") is True and "capture" in (missing.get("hint") or ""))

    tmp = os.path.join(tempfile.gettempdir(), "_parity_test_baseline.json")
    try:
        P.write_baseline(path=tmp, snap=a)
        loaded = P.load_baseline(path=tmp)
        check("写盘 → 读回结构完整",
              loaded.get("version") == P.VERSION and
              len(loaded.get("targets") or {}) == len(a["targets"]))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # 注入一个会抛异常的目标：异常类型必须进指纹，而不是让 capture 崩
    P._TARGETS.append({"name": "__boom__", "mod": "backend.parity",
                       "fn": "_hash", "args": [[], []]})
    try:
        c = P.capture()
        boom = (c["targets"].get("__boom__") or {}).get("cases") or []
        check("函数抛异常 → 记成 exc 而不是崩", len(boom) == 2 and all(x.get("exc") for x in boom),
              repr(boom[:1]))
        check("异常也参与指纹（hash 非空）", all(x.get("hash") for x in boom))
    finally:
        P._TARGETS.pop()

    # ---- 6. 接线：报告是给人看的中文，不是一堆哈希 ----
    txt = P.as_text({"ok": True, "targets": 9, "checked": 42})
    check("通过时报告写明「一条都没变」", "一条都没变" in txt, txt[:80])
    txt2 = P.as_text(r)
    check("漂移报告点出是哪个函数", victim in txt2, txt2[:120])

    # ---- 7. 真接线：建了不接等于没建（专家原话）----
    #   不在这里真跑 regress（要 20 秒），而是查两个真实入口的源码里有没有这段调用：
    #   · backend/iterloop.py  regress_part() —— 每轮评分时跑；
    #   · backend/selftest.py  build_report() —— 自检报告卡里出条目。
    import io
    import os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

    def _src(rel):
        with io.open(_os.path.join(_root, rel), encoding="utf-8") as f:
            return f.read()

    it_src = _src(_os.path.join("backend", "iterloop.py"))
    st_src = _src(_os.path.join("backend", "selftest.py"))
    check("每轮评分真的跑 parity（iterloop.regress_part 接线）",
          "from . import parity as _PA" in it_src and '"parity": par' in it_src)
    check("自检报告卡真的出 parity 条目（selftest.build_report 接线）",
          "行为基线·parity" in st_src and '"parity": par' in st_src)
    check("基线落在 data/ 而不是代码目录",
          _os.path.join("data", "parity") in P.BASELINE_PATH.replace("/", _os.sep))

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
