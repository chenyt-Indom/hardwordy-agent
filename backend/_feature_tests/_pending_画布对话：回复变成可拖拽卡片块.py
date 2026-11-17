"""新功能验收断言：画布对话：回复变成可拖拽卡片块

对应摩擦点「同一需求被重复发送 20 次」：信息在文字流里定位不到，用户只能重问。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('frontend/app.js', encoding='utf-8').read()
    assert 'canvas-card' in src, 'app.js 未生成 canvas-card 块'
    art = open('frontend/artifacts.js', encoding='utf-8').read()
    assert 'cardFromBlock' in art, 'artifacts.js 缺少 cardFromBlock'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.canvas-card' in css, 'style.css 缺少 .canvas-card 样式'
    assert 'draggable' in src or 'dragstart' in src, '缺少拖拽实现'
    print('canvas card ok')
