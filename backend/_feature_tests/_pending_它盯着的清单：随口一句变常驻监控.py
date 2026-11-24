"""新功能验收断言：它盯着的清单：随口一句变常驻监控

对应预判「继续追 RSI 长期稳定性」——把「盯 RSI 是否正常」也变成一条可自动巡检的清单项。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    jp = open('backend/jobplan.py', encoding='utf-8').read()
    assert 'watch_create' in jp, 'jobplan.py 缺少 watch_create'
    assert re.search(r'盯着|关注|提醒', jp), 'watch_create 未解析触发句式'
    sc = open('backend/scheduler.py', encoding='utf-8').read()
    assert 'watch' in sc, 'scheduler.py 缺少 watch 分支'
    assert 'hash' in sc.lower(), 'scheduler.py 缺少哈希对比'
    tc = open('frontend/taskcard.js', encoding='utf-8').read()
    assert 'red' in tc.lower() or 'dot' in tc.lower(), 'taskcard.js 缺少红点渲染'
    print('ok')
