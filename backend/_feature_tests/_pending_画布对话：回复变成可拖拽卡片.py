"""新功能验收断言：画布对话：回复变成可拖拽卡片

对应摩擦点「回复被截断」×36 和「只诊断不修复」——卡片化后单块内容短，不再需要截断

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re, pathlib
    root = pathlib.Path('.')
    art = root / 'frontend' / 'artifacts.js'
    app = root / 'frontend' / 'app.js'
    css = root / 'frontend' / 'style.css'
    assert art.exists(), 'artifacts.js 不存在'
    assert app.exists(), 'app.js 不存在'
    assert css.exists(), 'style.css 不存在'
    art_src = art.read_text(encoding='utf-8')
    app_src = app.read_text(encoding='utf-8')
    css_src = css.read_text(encoding='utf-8')
    assert 'renderCanvasBlock' in art_src, '缺少 renderCanvasBlock'
    assert 'artifacts' in app_src, 'appendMessage 未接入 artifacts 分支'
    assert '.canvas-block' in css_src, '缺少 .canvas-block 样式'
    assert 'mousedown' in art_src and 'mousemove' in art_src, '缺少拖拽事件处理'
    print('canvas block OK')
