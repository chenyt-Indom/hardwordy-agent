"""新功能验收断言：RSI 常驻状态卡：一眼看到哪项异常

反复问「正常吗」导致重复劳动

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
    from backend.iterhealth import snapshot

    s = snapshot()
    assert isinstance(s, dict)
    for k in ('score', 'gates', 'todos', 'last_failure'):
        assert k in s, k
    assert isinstance(s['gates'], (list, dict))
    assert isinstance(s['todos'], int) and s['todos'] >= 0

    # 异常标记字段存在且为布尔
    if isinstance(s['gates'], list):
        for g in s['gates']:
            assert 'ok' in g and isinstance(g['ok'], bool)

    s2 = snapshot()
    assert s2['score'] == s['score']
    assert s2['todos'] == s['todos']
    print('ok')
