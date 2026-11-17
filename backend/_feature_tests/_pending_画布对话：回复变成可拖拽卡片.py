"""新功能验收断言：画布对话：回复变成可拖拽卡片

回复被截断、结论埋在过程里；用户要反复滚动找内容

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import os
    p = 'frontend/artifacts.js'
    assert os.path.exists(p), 'artifacts.js 不存在'
    src = open(p, encoding='utf-8').read()
    assert 'render' in src, '缺少 render 函数'
    assert 'artifact_id' in src or 'artifact_id' in open('frontend/app.js', encoding='utf-8').read(), '缺少 artifact_id 上下文传递'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert 'canvas' in css or 'card' in css, '缺少画布/卡片样式'
    print('ok')
