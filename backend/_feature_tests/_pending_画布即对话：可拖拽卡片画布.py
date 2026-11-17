"""新功能验收断言：画布即对话：可拖拽卡片画布

回复被截断（40 次）+ 信息呈现单一

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
    assert 'class CardCanvas' in src, 'CardCanvas 类未定义'
    assert re.search(r'addCard\s*\(', src), 'addCard 方法缺失'
    assert re.search(r'render\s*\(', src), 'render 方法缺失'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'CardCanvas' in app and 'addCard' in app, 'app.js 未接入 CardCanvas.addCard'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert 'card' in css.lower(), 'style.css 缺少卡片样式'
    print('ok')
