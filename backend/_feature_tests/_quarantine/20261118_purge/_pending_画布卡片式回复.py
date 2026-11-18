"""新功能验收断言：画布卡片式回复

回复形态单一，生成物埋在文字流里找不到；用户要反复滚动定位。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src=open('frontend/app.js',encoding='utf-8').read()
    assert 'renderMessage' in src
    assert 'data-block-id' in src or 'blockId' in src
    art=open('frontend/artifacts.js',encoding='utf-8').read()
    assert 'renderBlock' in art
    css=open('frontend/style.css',encoding='utf-8').read()
    assert '.canvas-grid' in css and '.block' in css
    print('ok')
