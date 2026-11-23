"""新功能验收断言：答完给张卡：可交互小卡

对应摩擦点「回复被截断」和「过程过长/碎步」——文字流太长，用户看不到重点。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    lc = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'renderAnswerCard' in lc, 'livecard.js 缺少 renderAnswerCard'
    assert re.search(r'function\s+renderAnswerCard|renderAnswerCard\s*=', lc), 'renderAnswerCard 未定义'
    for k in ['weather','map','doc','image']:
        assert k in lc, f'缺少 kind 分支: {k}'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'renderAnswerCard' in app, 'app.js 未调用 renderAnswerCard'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert ':root' in css, 'style.css 缺少 :root 变量'
    print('ok')
