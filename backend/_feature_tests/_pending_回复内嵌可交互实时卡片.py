"""新功能验收断言：回复内嵌可交互实时卡片

回复是文字墙，工具结果藏在折叠块

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re, pathlib
    src = pathlib.Path('frontend/livecard.js').read_text(encoding='utf-8')
    assert 'renderLiveCard' in src, 'livecard.js 缺少 renderLiveCard'
    for t in ['map','weather','image','doc','ppt']:
        assert t in src, f'缺少类型 {t}'
    app = pathlib.Path('frontend/app.js').read_text(encoding='utf-8')
    assert 'renderLiveCard' in app, 'app.js 未调用 renderLiveCard'
    print('ok')
