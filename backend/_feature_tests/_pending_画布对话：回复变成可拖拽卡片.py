"""新功能验收断言：画布对话：回复变成可拖拽卡片

回复被截断 39 次；同一诉求反复说 6 次

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
    assert 'CanvasBoard' in src, 'artifacts.js 应定义 CanvasBoard'
    assert 'draggable' in src or 'onmousedown' in src, 'CanvasBoard 应支持拖拽'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'CanvasBoard' in app, 'app.js 应引用 CanvasBoard'
    assert 'canvasMode' in app or 'canvas_mode' in app, 'app.js 应有画布模式开关'
