"""新功能验收断言：画布圈选再改：回复变成可拖拽卡片，圈选某块说「就这块再改改」

用户改局部内容时要重新描述上下文，浪费轮次；且长回复里想单独操作某一块很难。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, json, re
    sys.path.insert(0, os.path.abspath('frontend'))

    # 前端 JS 无法直接 import，改为静态检查关键函数是否已定义
    src = open(os.path.join('frontend', 'artifacts.js'), encoding='utf-8').read()
    for fn in ['renderCanvas', 'selectBlock', 'mergeBlocks']:
        assert re.search(r'function\s+' + fn + r'\b|' + fn + r'\s*[:=]\s*(async\s*)?function|' + fn + r'\s*[:=]\s*\(', src), fn + ' not defined'

    # app.js 里应有 canvas 开关分支
    app = open(os.path.join('frontend', 'app.js'), encoding='utf-8').read()
    assert 'canvas' in app.lower()

    # style.css 应有画布相关样式
    css = open(os.path.join('frontend', 'style.css'), encoding='utf-8').read()
    assert 'canvas' in css.lower() or 'block' in css.lower()
    print('canvas ok')
