"""新功能验收断言：画布对话：回复变成可拖拽的卡片块

回复被截断 ×36；只诊断不修复时用户找不到交付物在哪。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re, io
    src = io.open('frontend/artifacts.js', encoding='utf-8').read()
    assert 'renderBlocks' in src, 'artifacts.js 应定义 renderBlocks'
    assert 'canvas_mode' in src or 'canvas_mode' in io.open('frontend/app.js', encoding='utf-8').read(), '应有 canvas_mode 开关'
    css = io.open('frontend/style.css', encoding='utf-8').read()
    assert '.artifact-card' in css, 'style.css 应有 .artifact-card 样式'
