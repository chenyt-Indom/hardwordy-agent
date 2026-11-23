# -*- coding: utf-8 -*-
"""回归：短确认规则（质量不降）+ 缓存前缀（token 降）。

用户 2026-11-24 原话：「质量只能提升不能降，而且 token 消耗也要降」。
这里守三条底线：

  1. **不挡活**：「继续 / 然后呢 / 还有呢」是催活指令，绝不能被判成短确认
     压成 3 行（那会把该动手的活儿挡回去）；规则文本里也不许再有
     「要我改吗？」这种把活推回给用户的写法。
  2. **规则不消失**：短确认规则从系统提示挪到了用户消息，但必须**真的送到**模型手里。
  3. **缓存前缀不被动**：system 消息必须原样不变（它是 ≈13000 token 工具定义的缓存锚点），
     且原 messages 列表不能被就地改写（否则会污染落盘历史）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def run() -> int:
    from backend import main as M
    from backend import tools as T

    fails = []

    def chk(cond, name):
        if not cond:
            fails.append(name)

    # ---------- 1. 短确认判定：催活指令不算，纯确认算 ----------
    for t in ("继续", "然后呢", "还有呢"):
        chk(M._is_short_confirm(t) is False,
            "「%s」不该判成短确认（那是催活，不是确认）" % t)
    for t in ("是", "好了吗", "对吗？", "确认"):
        chk(M._is_short_confirm(t) is True, "「%s」应判成短确认" % t)
    # 排除词仍然生效：≤10 字但是真任务
    chk(M._is_short_confirm("帮我画个图吗") is False, "带「帮我」的不算短确认")
    chk(M._is_short_confirm("怎么改的？") is False, "带「怎么」的不算短确认")
    # 长句一律不算
    chk(M._is_short_confirm("这个功能是不是已经完全验证过了呢") is False,
        "超过 10 字不算短确认")

    # ---------- 2. 规则文本：限制篇幅，但不许挡活 ----------
    ctx = M._short_confirm_ctx()
    chk("要我改吗" not in ctx, "规则里不能再有「要我改吗」把活推回去")
    chk("照常调用工具" in ctx, "规则必须写明：该动手就动手")
    chk("3 行" in ctx, "仍然限制正文 3 行（篇幅要压）")
    chk("200" in ctx, "仍然限制思考 200 字")

    # ---------- 3. 挂载位置：system 不变，规则落在用户消息 ----------
    base = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "好了吗"}]
    out = M._attach_live_ctx(base, short_ctx=ctx)
    chk(out[0]["content"] == "SYS", "system 必须原样不变（缓存前缀锚点）")
    chk("本轮篇幅规则" in out[1]["content"], "短确认规则要挂到用户消息上")
    chk(ctx in out[1]["content"], "规则正文必须完整送达")
    chk("好了吗" in out[1]["content"], "原问题不能被覆盖")
    chk(base[1]["content"] == "好了吗", "原 messages 不能被就地改写")

    # 非短确认轮次：一个字都不该多
    out2 = M._attach_live_ctx([{"role": "system", "content": "SYS"},
                               {"role": "user", "content": "帮我做个 PPT"}])
    chk("本轮篇幅规则" not in out2[1]["content"], "非短确认轮次不许注入篇幅规则")

    # ---------- 4. lean 工具集：真省 token，但不砍该有的工具 ----------
    full = T.make_schemas(True, True, True, lean=False)
    lean = T.make_schemas(True, True, True, lean=True)
    chk(len(lean) < len(full),
        "lean 工具数应更少（full=%d lean=%d）" % (len(full), len(lean)))
    names_lean = {s.get("function", {}).get("name") for s in lean}
    for must in ("run_python", "read_file", "ask_user"):
        chk(must in names_lean, "lean 必须保留 %s（短确认也可能要核实/动手）" % must)
    for dropped in ("map_plan", "get_weather"):
        chk(dropped not in names_lean, "lean 应砍掉 %s" % dropped)

    # 粗略 token 估算（schema 字数 / 3.5，只用来判断量级）
    def _sz(schs):
        return int(len(__import__("json").dumps(schs, ensure_ascii=False)) / 3.5)
    saved = _sz(full) - _sz(lean)
    print("  工具集估算：full≈%d token / lean≈%d token，省≈%d" % (_sz(full), _sz(lean), saved))
    chk(saved > 1500, "lean 至少要省下 1500 token 量级（实测省 %d）" % saved)

    print("  通过 %d / 失败 %d" % (11 + 4 + 6 + 6 - len(fails), len(fails)))
    for f in fails:
        print("  [X] " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
