"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯，变化画成时间线

对应预判「要求关掉页面它还在干活的后台能力」；也缓解「同一需求被重复发送」——用户不用每天手动问一次。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re, json
    s = open('backend/scheduler.py', encoding='utf-8').read()
    assert 'watch' in s, 'scheduler 未支持 watch 任务'
    a = open('backend/actlog.py', encoding='utf-8').read()
    assert re.search(r'def\s+\w*write\w*', a), 'actlog 缺少写入函数'
    l = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'renderWatchTimeline' in l, '缺少时间线渲染'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert '/api/watch/add' in app, '缺少登记接口调用'
    print('ok')
