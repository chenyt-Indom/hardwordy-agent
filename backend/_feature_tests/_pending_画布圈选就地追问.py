"""新功能验收断言：画布圈选就地追问

对应预判『画布式交互』；对应摩擦点『过程过长/碎步』——圈选比打字快。

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
    assert 'initCanvasOverlay' in src
    assert 'canvas' in src.lower()
    html = open('frontend/index.html', encoding='utf-8').read()
    assert 'canvas' in html.lower()
    py = open('backend/canvas.py', encoding='utf-8').read()
    assert re.search(r'def\s+ask_region\s*\(', py)
    assert re.search(r'def\s+redraw_region\s*\(', py)
    assert '/api/canvas/ask' in py or 'canvas/ask' in py
    assert '/api/canvas/redraw' in py or 'canvas/redraw' in py
    print('ok')
