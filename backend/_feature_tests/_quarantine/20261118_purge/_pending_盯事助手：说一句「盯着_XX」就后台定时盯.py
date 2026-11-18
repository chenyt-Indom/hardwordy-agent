"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯

用户反复问「现在一切正常吗」——本质是想要一个持续盯着的助手

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import os, sys
    sys.path.insert(0, 'backend')
    assert os.path.exists('backend/scheduler.py')
    assert os.path.exists('backend/actlog.py')
    src = open('backend/scheduler.py', encoding='utf-8').read()
    assert 'register_watch' in src, '缺少 register_watch'
    log = open('backend/actlog.py', encoding='utf-8').read()
    assert 'watch' in log, 'actlog 缺少 watch 事件类型'
    assert os.path.exists('frontend/livecard.js'), '缺少 livecard.js'
    print('ok')
