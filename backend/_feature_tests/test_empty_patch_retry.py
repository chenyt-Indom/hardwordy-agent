"""验证「模型给了合法 JSON 但 patches 为空」时的重试（2026-11-24）。

actlog 实测 6 次「模型没有给出任何 patch」直接判死 —— 合法 JSON、patches 空，
等于白花一轮。修法是重试一次并明确二选一（给 patches / 用 skip 说明原因）。
这里用假 _llm_text 断言四条：救回、真放弃、走 skip、正常路径不多花一次。
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import self_iterate as SI     # noqa: E402


def _utf8():
    """只改编码，不新建 sys.stdout 对象（新建会在模块回收时关闭 regress 的缓冲）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                    # noqa: BLE001
        pass

_results = []


def check(name, cond, extra=""):
    _results.append((name, bool(cond), extra))
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))


GOOD = '{"patches":[{"file":"backend/lessonloop.py","find":"def audit() -> dict:",' \
       '"replace":"def audit() -> dict:"}]}'
EMPTY = '{"patches":[]}'
SKIP = '{"skip":"这段代码不在当前文件里"}'

PLAN = {"id": "t", "title": "t"}
ACTION = {"title": "测试动作", "detail": "改 backend/lessonloop.py 的 audit",
          "files": ["backend/lessonloop.py"], "origin": "feature", "note": ""}


def run_with(outputs):
    """把 _llm_text 换成按顺序吐 outputs 的假函数，返回 (patches, err, 调用次数)。"""
    calls = []

    def fake(prompt, max_tokens=0, temperature=0.0, **kw):
        calls.append(prompt)
        i = min(len(calls) - 1, len(outputs) - 1)
        return outputs[i], ""

    old = SI._llm_text
    SI._llm_text = fake
    try:
        patches, err = SI._plan_patch_for_action(PLAN, ACTION)
    finally:
        SI._llm_text = old
    return patches, err, len(calls)


def main():
    _utf8()
    # ① 空 patches → 重试后被救回
    p, e, n = run_with([EMPTY, GOOD])
    check("空 patches -> 重试救回", bool(p) and not e, (p, e))
    check("救回时确实调了 2 次", n == 2, n)
    check("救回的 patch 内容正确",
          p and p[0].get("file") == "backend/lessonloop.py", p[:1] if p else None)

    # ② 两次都空 → 明确说已重试，而不是含糊一句
    p, e, n = run_with([EMPTY, EMPTY])
    check("两次都空 -> 失败", not p and bool(e), (p, e))
    check("两次都空 -> 说明已重试 1 次", "已重试 1 次" in str(e), e)
    check("两次都空 -> 调了 2 次", n == 2, n)

    # ③ 重试时改用 skip → 按「无法安全实施」处理（不当成解析失败）
    p, e, n = run_with([EMPTY, SKIP])
    check("重试后给 skip -> 判无法实施", not p and "无法安全实施" in str(e), e)

    # ④ 一次就给出 patches → 绝不能多调一次（不浪费 token）
    p, e, n = run_with([GOOD])
    check("正常路径 -> 成功", bool(p) and not e, (p, e))
    check("正常路径 -> 只调 1 次", n == 1, n)

    # ⑤ 重试的 prompt 里必须带上「必须二选一」的明确要求
    _, _, _ = run_with([EMPTY, GOOD])
    calls = []
    old = SI._llm_text

    def fake2(prompt, max_tokens=0, temperature=0.0, **kw):
        calls.append(prompt)
        return (EMPTY if len(calls) == 1 else GOOD), ""

    SI._llm_text = fake2
    try:
        SI._plan_patch_for_action(PLAN, ACTION)
    finally:
        SI._llm_text = old
    check("重试 prompt 点名 patches 为空", len(calls) == 2 and "没有 patches" in calls[1],
          len(calls))
    check("重试 prompt 要求 find 唯一", len(calls) == 2 and "唯一" in calls[1])

    bad = [x for x in _results if not x[1]]
    print("\n通过 %d / 失败 %d" % (len(_results) - len(bad), len(bad)))
    for n_, _, e_ in bad:
        print("  FAILED:", n_, e_)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
