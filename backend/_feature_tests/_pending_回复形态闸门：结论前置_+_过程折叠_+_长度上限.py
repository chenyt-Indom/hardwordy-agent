"""新功能验收断言：回复形态闸门：结论前置 + 过程折叠 + 长度上限

回复被截断、只诊断不修复

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys
    sys.path.insert(0,'backend')
    from backend.reply_gate import gate
    long_text='过程'*5000
    out=gate(long_text)
    assert isinstance(out,str)
    assert len(out)<=2000
    assert out.startswith('结论') or '结论' in out[:200]
    print('ok')
