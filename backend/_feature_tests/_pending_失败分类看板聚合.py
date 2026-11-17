"""新功能验收断言：失败分类看板聚合

用户问「为什么几次自动迭代会失败」时我空回或手查

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
    from backend.actlog import fail_kind_stats
    events = [
        {'fail_kind': 'timeout'},
        {'fail_kind': 'timeout'},
        {'fail_kind': 'syntax'},
        {'fail_kind': None},
    ]
    stats = fail_kind_stats(events)
    assert stats.get('timeout') == 2, 'timeout 计数错误'
    assert stats.get('syntax') == 1, 'syntax 计数错误'
    print('OK')
