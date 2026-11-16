"""新功能验收断言：画布式回复卡片（创意种子 1 的精简落地版）

对应摩擦点「同一需求被重复发送」——用户反复用文字描述「就刚才那个图/那段文字」，因为没法直接指。

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
    assert 'renderReplyBlocks' in src, 'artifacts.js 缺少 renderReplyBlocks'
    assert 'data-block-id' in src or 'dataset.blockId' in src, '缺少块 id 标记'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'renderReplyBlocks' in app, 'app.js 未调用 renderReplyBlocks'
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.rb-block' in css, 'style.css 缺少 .rb-block 样式'
    print('ok')
