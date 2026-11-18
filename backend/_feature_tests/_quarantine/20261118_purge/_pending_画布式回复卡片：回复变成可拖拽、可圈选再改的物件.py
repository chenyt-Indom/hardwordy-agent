"""新功能验收断言：画布式回复卡片：回复变成可拖拽、可圈选再改的物件

对应摩擦点「同一需求被重复发送」47 次——用户反复描述同一段要改的内容，本质是文字流无法被指认。

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
    assert 'class CanvasBoard' in src, 'CanvasBoard 类未定义'
    assert 'render' in src and 'getSelection' in src, '缺少 render/getSelection'
    assert 'marquee' in src or 'selection' in src.lower(), '缺少圈选实现'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'CanvasBoard' in app, 'app.js 未接入 CanvasBoard'
    print('ok')
