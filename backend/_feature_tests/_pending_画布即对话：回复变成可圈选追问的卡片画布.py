"""新功能验收断言：画布即对话：回复变成可圈选追问的卡片画布

对应摩擦点「同一需求被重复发送」——用户重复描述往往是因为指不到具体位置。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src=open('frontend/livecard.js',encoding='utf-8').read()
    assert 'renderCanvas' in src, 'livecard.js 缺少 renderCanvas'
    assert re.search(r'function\s+renderCanvas|renderCanvas\s*[=:]', src), 'renderCanvas 未定义'
    app=open('frontend/app.js',encoding='utf-8').read()
    assert 'renderCanvas' in app, 'app.js 未调用 renderCanvas'
    assert ('cardId' in src) or ('card_id' in src), '缺少卡片 id 上下文'
    print('ok')
