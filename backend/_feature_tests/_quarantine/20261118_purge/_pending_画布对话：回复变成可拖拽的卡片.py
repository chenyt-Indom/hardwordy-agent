"""新功能验收断言：画布对话：回复变成可拖拽的卡片

回复被截断、信息难定位；用户反复问「详细说明改了什么」说明文字流不好读

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('frontend/artifacts.js', encoding='utf-8').read()
    assert 'renderCanvas' in src, 'artifacts.js 缺少 renderCanvas'
    assert re.search(r'function\s+renderCanvas|renderCanvas\s*=', src), 'renderCanvas 未定义'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.canvas-grid' in css and '.canvas-card' in css, '缺少画布样式'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'renderCanvas' in app, 'app.js 未接入 renderCanvas'
    print('canvas ok')
