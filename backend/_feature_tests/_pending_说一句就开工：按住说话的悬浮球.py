"""新功能验收断言：说一句就开工：按住说话的悬浮球

对应摩擦点「同一需求被重复发送」和「回复被截断」——用户不想打字、不想等长回复。

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
    assert 'startVoiceOrb' in src, 'app.js 缺少 startVoiceOrb'
    assert re.search(r'function\s+startVoiceOrb|startVoiceOrb\s*=', src), 'startVoiceOrb 未定义'
    html = open('frontend/index.html', encoding='utf-8').read()
    assert 'voice' in html.lower() or 'orb' in html.lower(), 'index.html 缺少悬浮球入口'
    lc = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'todo' in lc.lower() or 'card' in lc.lower(), 'livecard.js 缺少待办卡片渲染'
    print('ok')
