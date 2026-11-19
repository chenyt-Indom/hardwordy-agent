"""新功能验收断言：实时卡片：回复气泡里嵌可交互结果卡

回复被截断/过程过长时，用户看不到实际产出；纯文字墙难读

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
    assert 'renderLiveCard' in src, '缺少 renderLiveCard 定义'
    for t in ['map','weather','image','doc','ppt','video']:
        assert t in src, f'缺少类型 {t}'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'attachments' in app and 'renderLiveCard' in app, 'app.js 未接入 attachments 渲染'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.livecard' in css, '缺少 .livecard 样式'
    main = open('backend/main.py', encoding='utf-8').read()
    assert 'attachments' in main, 'main.py 未输出 attachments 字段'
    print('livecard ok')
