# -*- coding: utf-8 -*-
"""回归：教训的「约束力」与「可见性」（用户 2026-11-24 要求）。

用户原话：「教训涉及反思机制，所以还是希望把它放在约束力高的位置，
或者也可以让它能被看见。」

守五条：

  1. **约束力**：系统提示里必须有一段**静态**硬约束声明 —— 明确「历史教训不是参考资料，
     是必须执行的硬约束」。没有这句，模型会把教训当普通背景信息随手忽略。
  2. **静态性（最重要）**：教训正文一变，系统提示必须**一字不变**。
     否则排在后面的 ≈13000 token 工具定义每轮作废，省下来的 token 全还回去。
  3. **正文仍在用户消息**：教训内容一个字不少地送到模型手里，口径是「硬约束」，
     不再写「参考建议」。
  4. **可见性**：每轮真正挂上教训时留痕，界面能查到「上次注入了哪几条、什么时候、多长」。
  5. **留痕有节流**：相同内容 5 分钟内不重复写盘（它跑在对话主循环里，不能每轮 IO）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# 控制台默认 GBK，中文断言信息会变成乱码；统一按 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

# ⚠️ 必须先隔离数据目录：留痕会写 data/self_iterate/，绝不能在测试里污染真账本。
_TMP = tempfile.mkdtemp(prefix="mm_lesson_vis_")
os.environ["MM_DATA_DIR"] = _TMP


def run() -> int:
    from backend import main as M
    from backend import lessonloop as LL

    fails = []

    def chk(cond, name):
        if not cond:
            fails.append(name)

    # ---------- 1 & 2. 系统提示：静态硬约束在，教训正文不在，且内容变化时提示不变 ----------
    orig_hint = LL.hint
    seq = {"n": 0}

    def fake_hint(*a, **k):
        seq["n"] += 1
        # 交替返回两段**完全不同**的教训：模拟「RSI 采样变了 / 累计次数变了」
        if seq["n"] % 2:
            return ("\n【历史教训（未修完，别重复踩）】\n"
                    "- 「anchor_miss」累计 1 次：改法-AAA\n")
        return ("\n【历史教训（未修完，别重复踩）】\n"
                "- 「no_change」累计 9 次：改法-BBB\n")

    try:
        LL.hint = fake_hint
        o1 = {}
        s1 = M._SystemPrompt.build("你好", out=o1)
        o2 = {}
        s2 = M._SystemPrompt.build("你好", out=o2)
    finally:
        LL.hint = orig_hint

    chk("【历史教训 = 硬约束" in s1, "系统提示里必须有【历史教训 = 硬约束】静态声明")
    chk("必须执行" in s1, "声明要写清：教训是必须执行的，不是参考资料")
    _seg = s1.split("【历史教训 = 硬约束")[1][:500]
    chk("参考建议" not in _seg and "仅供参考" not in _seg,
        "声明里不许把教训说成「参考建议/仅供参考」（模型会随手忽略）")
    chk("不要自己编造教训" in s1, "要写明：教训块为空时不许自己编教训")
    chk("改法-AAA" not in s1 and "改法-BBB" not in s1,
        "教训**正文**不许进系统提示（否则每轮作废 13000 token 工具定义）")
    chk(s1 == s2, "教训内容变了，系统提示必须一字不变（前缀缓存锚点）")
    chk(o1.get("lesson") and o2.get("lesson") and o1["lesson"] != o2["lesson"],
        "教训正文仍要经 out 交给用户消息（内容不许丢）")

    # ---------- 3. 正文挂载：口径是硬约束，且不动 system、不改原消息 ----------
    body = "【历史教训（未修完，别重复踩）】\n- 「X」累计 2 次：改法-CCC\n"
    base = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "把这个改掉"}]
    out = M._attach_live_ctx(base, lesson_ctx=body)
    chk(out[0]["content"] == "SYS", "system 必须原样不变")
    chk("硬约束" in out[1]["content"], "用户消息里的教训块口径必须是「硬约束」")
    chk("参考建议" not in out[1]["content"], "不许再写「参考建议」（模型会随手忽略）")
    chk("改法-CCC" in out[1]["content"], "教训正文必须完整送达")
    chk("把这个改掉" in out[1]["content"], "原问题不能被覆盖")
    chk(base[1]["content"] == "把这个改掉", "原 messages 不能被就地改写")

    # ---------- 4. 可见性：真正挂上时留痕，且能读回来 ----------
    LL._last_inj["ts"] = 0.0
    LL._last_inj["text"] = ""
    p = LL._injections_path()
    if os.path.exists(p):
        os.remove(p)

    M._attach_live_ctx([{"role": "user", "content": "hi"}], lesson_ctx=body)
    li = LL.last_injection()
    chk(bool(li.get("ok")), "挂载后要能读到注入记录")
    chk("改法-CCC" in (li.get("text") or ""), "记录里要有教训原文")
    chk(int(li.get("count") or 0) >= 2, "记录要带条数（供界面显示「N 条」）")
    chk(int(li.get("chars") or 0) > 0, "记录要带字数")
    chk(bool(li.get("iso")), "记录要带时间")

    # ---------- 5. 节流：相同内容不重复写，空内容不写，内容变了要写 ----------
    def n_lines():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return len([x for x in f if x.strip()])
        except OSError:
            return 0

    n1 = n_lines()
    LL.record_injection(body)          # 与上一条完全相同，且刚写过 → 不写
    chk(n_lines() == n1, "相同内容 5 分钟内不许重复写盘（节流）")
    LL.record_injection("")
    chk(n_lines() == n1, "空内容不写")
    LL.record_injection("【历史教训】\n- 「Y」累计 1 次：改法-DDD\n")
    chk(n_lines() == n1 + 1, "内容变了必须写")
    chk("改法-DDD" in (LL.last_injection().get("text") or ""), "最近一条要是新的")

    # ---------- 6. 接口带出该字段（界面靠它显示） ----------
    try:
        r = M.selfiter_rsi(limit=5)
        chk("last_injected" in r, "/api/selfiter/rsi 必须带 last_injected 字段")
        chk(bool((r.get("last_injected") or {}).get("ok")),
            "接口返回的 last_injected 要能读到记录")
    except Exception as e:                              # noqa: BLE001
        fails.append("selfiter_rsi 调用失败：%s: %s" % (type(e).__name__, e))

    if fails:
        print("[FAIL] 教训约束力/可见性回归：%d 项未通过" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("[OK] 教训约束力/可见性回归：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
