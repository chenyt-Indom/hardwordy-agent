"""新功能验收断言：画布对话卡片

对应摩擦点「回复被截断」×36 和「只诊断不修复」×38——卡片块天然限长，且每块可单独操作。

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
    assert 'renderCanvasCard' in src, 'artifacts.js 缺少 renderCanvasCard'
    assert re.search(r'function\s+renderCanvasCard\s*\(', src), 'renderCanvasCard 不是函数定义'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'renderCanvasCard' in app, 'app.js 未调用 renderCanvasCard'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.canvas-block' in css, 'style.css 缺少 .canvas-block 样式'
    print('ok')
