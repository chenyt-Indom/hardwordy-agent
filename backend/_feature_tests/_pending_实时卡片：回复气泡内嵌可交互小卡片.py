"""新功能验收断言：实时卡片：回复气泡内嵌可交互小卡片

回复被截断 + 信息呈现单一，用户需要在一大段文字里找结果

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
    assert 'renderLiveCard' in src, 'livecard.js 缺少 renderLiveCard'
    for t in ['map','weather','image','doc','ppt','video']:
        assert t in src, f'缺少类型 {t}'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'renderLiveCard' in app, 'app.js 未调用 renderLiveCard'
    assert 'attachments' in app, 'app.js 未处理 attachments 字段'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert 'livecard' in css.lower() or 'live-card' in css.lower(), 'style.css 缺少卡片样式'
    print('ok')
