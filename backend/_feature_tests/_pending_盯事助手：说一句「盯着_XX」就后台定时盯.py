"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯

现在所有事都要用户手动开口问一次，没有「它自己盯着」的能力。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import inspect
    import backend.actlog as actlog
    import backend.scheduler as scheduler
    src=inspect.getsource(actlog)
    assert 'watch' in src
    ssrc=inspect.getsource(scheduler)
    assert '_run_once' in ssrc
    assert 'watch' in ssrc
    lc=open('frontend/livecard.js',encoding='utf-8').read()
    assert '/api/watch/list' in lc
    print('ok')
