import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

# 2026-11-15 修：原断言查 frontend/app.js 里有没有 'conclusionFirst' / 'collapseThink'
# 这两个**字面字符串** —— 既不检查行为，又绑死在前端实现上（实现搬到后端就误报）。
# 改成直接验 reply_gate 的真实行为：长过程折叠、结论保留、details 闭合、长度受控。
def run():
    from backend.reply_gate import format_reply
    proc = "排查过程。" * 400                       # 2400 字，远超折叠阈值 600
    out = format_reply("结论：三个问题全部修好。", proc, "产物：a.diff", 4000)
    assert "结论：三个问题全部修好。" in out, "结论必须完整保留"
    assert "<details" in out and "</details>" in out, "长过程应折叠成 details"
    assert out.count("<details") == out.count("</details>"), "details 必须成对闭合"
    assert len(out) <= 4000, "总长应受上限约束，实际 %d" % len(out)
    short = format_reply("结论：ok", "短过程", "", 4000)
    assert "<details" not in short, "短过程不该被折叠"
    assert short.startswith("结论："), "结论应在最前"
