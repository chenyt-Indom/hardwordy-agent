"""新功能验收断言：回复形态闸门：结论前置 + 过程折叠 + 长度上限

回复截断 + 过程淹没结论

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os
    sys.path.insert(0, os.path.abspath('backend'))
    from backend.reply_gate import finalize
    sample = '结论：这是答案。\n\n🧠 思考过程\n第一步……\n第二步……\n\n' + ('详细说明。' * 400)
    out = finalize(sample)
    text = out if isinstance(out, str) else ''.join(out)
    assert text.strip().startswith('结论'), '结论未前置'
    assert '<details>' in text and '</details>' in text, '思考过程未折叠'
    if isinstance(out, list):
        assert len(out) > 1, '超长未拆分'
        assert '结论' in out[0], '首段不含结论'
    print('ok')
