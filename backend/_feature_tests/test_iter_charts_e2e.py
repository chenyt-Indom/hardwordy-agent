# -*- coding: utf-8 -*-
"""端到端回归：反思链路真的会把 charts 写进计划（不是只有函数没人调用）。

为什么单独做一条 e2e：
  `test_iter_charts.py` 测的是 _clean_charts 本身 + 静态接线（字符串在不在）。
  但「字符串在」不等于「真跑起来能过」—— 本轮就踩过一次：一开始写的是
  `_clean_charts(..., _prompt)`，静态检查全绿，真跑却会 NameError 把整轮反思
  打断。所以这里用假模型返回**真跑一次 reflect()**，锁住整条链路。

不花钱、不联网：_llm_text 与 _build_reflect_prompt 都换成假的。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

CORPUS = "真实运行指标：共 42 条动作，失败 7 次。"
FAKE = {
    "summary": "测试用：本轮加图表",
    "charts": [
        {"title": "动作统计", "type": "bar", "unit": "条",
         "data": [{"label": "提出", "value": 42},
                  {"label": "失败", "value": 7}]},
        {"title": "编造的数字", "type": "pie", "unit": "个",
         "data": [{"label": "假的A", "value": 999},
                  {"label": "假的B", "value": 888}]},
    ],
    "weaknesses": [{"title": "测试项", "detail": "只是为了跑通链路",
                    "severity": "low"}],
    "user_insights": [], "forecast": [], "needs": [], "features": [],
    "questions": [], "tech_takeaways": [], "fail_lessons": [], "actions": [],
}


def run() -> int:
    old = os.environ.get("MM_DATA_DIR")
    os.environ["MM_DATA_DIR"] = tempfile.mkdtemp(prefix="mm_charts_e2e_")
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    try:
        from backend import self_iterate as SI

        # 关掉论文回灌（会加载向量模型，纯拖时间，与本测试无关）
        try:
            _st = SI.load_settings()
            _st["feature_paper_enrich"] = False
            SI.save_settings(_st)
        except Exception as e:                          # noqa: BLE001
            ck("关掉论文回灌", False, repr(e))

        # 让 prompt 里带上真实数字，模拟「材料里确实有这些数」
        _orig_build = SI._build_reflect_prompt

        def fake_build(*a, **kw):
            return _orig_build(*a, **kw) + "\n" + CORPUS

        SI._build_reflect_prompt = fake_build
        SI._llm_text = lambda prompt, **kw: (json.dumps(FAKE, ensure_ascii=False), "")

        out = SI.reflect(trigger="probe", use_web=False, polish=False,
                         auto_execute=False)
        ck("reflect 跑通了", bool(out.get("ok")), str(out.get("error")))
        plan = out.get("plan") or {}
        charts = plan.get("charts")
        ck("plan 里有 charts 且是 list", isinstance(charts, list),
           type(charts).__name__)
        if isinstance(charts, list):
            ck("两张图都保留", len(charts) == 2, str(len(charts)))
            if len(charts) == 2:
                ck("真实数字的图 → 不标未核实",
                   charts[0].get("unverified") is False,
                   str(charts[0].get("miss")))
                ck("编造的图 → 标未核实",
                   charts[1].get("unverified") is True,
                   str(charts[1].get("miss")))
                ck("标出具体项", set(charts[1].get("miss") or []) == {"假的A", "假的B"},
                   str(charts[1].get("miss")))

        d = SI.load_plans()
        saved = (d.get("items") or [{}])[0]
        ck("落盘的计划里也有 charts", len(saved.get("charts") or []) == 2,
           str(len(saved.get("charts") or [])))
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    if fails:
        print("失败项：" + "、".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
