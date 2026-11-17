"""新功能验收断言：画布卡片化回复（创意种子 1）

对应摩擦点「同一需求被重复发送」51 次——用户重复描述，本质是因为上一轮结果混在文字流里不好指认。

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
    art = (root/'frontend/artifacts.js').read_text(encoding='utf-8')
    app = (root/'frontend/app.js').read_text(encoding='utf-8')
    css = (root/'frontend/style.css').read_text(encoding='utf-8')
    assert 'renderCanvasCard' in art, 'artifacts.js 缺少 renderCanvasCard'
    assert re.search(r'canvas', app, re.I), 'app.js 未接入 canvas 容器'
    assert 'canvas-card' in css, 'style.css 缺少 .canvas-card 样式'
    assert re.search(r'就这块改', app) or re.search(r'就这块改', art), '缺少「就这块改」入口'
    print('canvas card OK')
