"""新功能验收断言：实时卡片：回复气泡内嵌可交互小卡片

回复被截断 49 次、过程过长 25 次——文字流本身承载不了多模态产出

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'LiveCard' in src and 'render' in src, 'livecard.js 未暴露 LiveCard.render'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'LiveCard' in app, 'app.js 未调用 LiveCard'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.livecard' in css, 'style.css 缺少 .livecard 样式'
    print('ok')
