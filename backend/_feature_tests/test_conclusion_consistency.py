# -*- coding: utf-8 -*-
"""回归：结论行必须与真实执行结果对得上（治「假完成」）。

真实误报（2026-11-25 用户反复踩）：
  助手回「已经解决 / 全部通过 / RSI 80.4」，而同一轮里动作有 failed、
  测试有失败项。用户只能一遍遍追问「搞定了吗 / 刚刚的任务完成了吗」，
  这是最伤信任的一类错 —— 靠模型自觉没用，必须有机器的对账闸门。

本测试锁住：
  ① 声称「已解决/全部通过」+ 本轮真有失败动作 → 必须追加「真实结果对账」；
  ② 没有失败动作 → 原样返回（绝不无中生有）；
  ③ 结论本来就在如实说问题 → 原样返回（不重复提醒）；
  ④ 已提到失败字样 → 不重复追加；
  ⑤ enforce 端到端：走完整闸门也能对得上；
  ⑥ 脏输入（None / 字符串动作 / 怪 dict）不许抛异常。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass


def run() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name
              + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    from backend import reply_gate as RG

    good_text = "已经解决，全部通过。\n\n细节略。"
    bad_actions = [
        {"action": "selfdev_patch", "file": "backend/a.py", "status": "failed"},
        {"action": "selfdev_patch", "file": "backend/b.py", "status": "done"},
    ]

    # ① 声称成功 + 有失败 → 必须对账
    out = RG._conclusion_consistency(good_text, bad_actions)
    ck("声称成功但有失败 → 追加对账", "真实结果对账" in out, out[-120:])
    ck("对账里点了失败动作", "selfdev_patch" in out or "失败" in out)
    ck("原文没被改写（仍含原句）", "已经解决，全部通过。" in out)

    # ② 没有失败动作 → 原样
    ok_actions = [{"action": "selfdev_patch", "status": "done"}]
    out2 = RG._conclusion_consistency(good_text, ok_actions)
    ck("全成功 → 原样返回", out2 == good_text, out2)
    out2b = RG._conclusion_consistency(good_text, [])
    ck("没有动作 → 原样返回", out2b == good_text)

    # ③ 结论本来就在说问题 → 原样
    honest = "结论：未通过，48 项里有 3 项失败。\n\n细节略。"
    out3 = RG._conclusion_consistency(honest, bad_actions)
    ck("本来就在如实说问题 → 原样", out3 == honest, out3)

    # ④ 结论提到「失败」字样 → 不重复追加
    half = "结论：已解决 2 项，但还有 1 项失败。\n\n细节略。"
    out4 = RG._conclusion_consistency(half, bad_actions)
    ck("已提到失败 → 不重复追加", "真实结果对账" not in out4, out4[-100:])

    # ⑤ enforce 端到端
    out5 = RG.enforce(good_text, actions=bad_actions)
    ck("enforce 端到端也会对账", "真实结果对账" in out5, out5[-150:])

    # ⑥ 脏输入不许崩
    for bad in (None, "", [{"status": "failed"}], "selfdev_patch failed",
                [{"status": "error", "title": "改 A 文件"}]):
        try:
            RG._conclusion_consistency(good_text, bad)
            ck("脏输入不崩：%r" % (bad,), True)
        except Exception as e:                          # noqa: BLE001
            ck("脏输入不崩：%r" % (bad,), False, repr(e))

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    if fails:
        print("失败项：" + "、".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
