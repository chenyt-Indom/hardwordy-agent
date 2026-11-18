"""新功能验收断言：动作卡片改动摘要渲染

动作卡片信息不足，用户无法判断迭代价值

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
    from backend.actlog import _log_act
    rec = _log_act({'patches': [{'find': 'old line', 'replace': 'new line'}]})
    assert 'changelog' in rec and rec['changelog'], 'changelog 字段缺失或为空'
    assert 'new line' in rec['changelog'] or 'old line' in rec['changelog'], '摘要未包含 patch 首行'
    print('OK')
