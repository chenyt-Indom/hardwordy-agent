"""新功能验收断言：结论前置闸门：回复第一行强制「结论：X（依据：Y）」

用户问「能省多少」得到排查过程；问「正常吗」得到「…」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys
    sys.path.insert(0, 'backend')
    from backend.reply_gate import enforce_conclusion
    r1 = enforce_conclusion('先检查了日志，发现端口占用，然后重启服务，最后恢复正常。')
    assert r1.splitlines()[0].startswith(('结论：', '答案：', '是的', '不是')), r1
    r2 = enforce_conclusion('')
    assert r2.strip(), '空正文未返回兜底文案'
    print('ok')
