"""新功能验收断言：边说边演数字人小窗

长思考过程让用户干等；回复是纯文字流，缺少'在场感'

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
    assert 'mountAvatarDock' in src, 'app.js 缺少 mountAvatarDock'
    assert 'viseme' in src.lower(), 'app.js 未订阅 viseme 驱动'
    html = open('frontend/index.html', encoding='utf-8').read()
    assert 'canvas' in html.lower(), 'index.html 缺少 canvas 小窗'
    py = open('backend/avatar2.py', encoding='utf-8').read()
    assert 'def viseme_for_text' in py, 'avatar2.py 缺少 viseme_for_text'
    print('ok')
