"""新功能验收断言：盯梢小助手一句话入口 + 主动弹卡

对应预判『盯梢小助手的日常使用』；对应摩擦点『同一需求被重复发送』——他会反复来问『盯到了吗』。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    h = open('backend/handoff.py', encoding='utf-8').read()
    assert re.search(r'def\s+parse_watch_intent\s*\(', h)
    s = open('backend/scheduler.py', encoding='utf-8').read()
    assert 'push_livecard' in s
    assert 'check_watch' in s
    j = open('frontend/jobplan.js', encoding='utf-8').read()
    assert '盯梢' in j
    print('ok')
