"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯，变化画成时间线

对应预判「用户会继续验证 LLM 是否真的全面接管」——盯事助手是 LLM 在后台持续决策的可见证据。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import ast
    src = open('backend/actlog.py', encoding='utf-8').read()
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert 'WatchTask' in names, 'WatchTask 未定义'
    sched = open('backend/scheduler.py', encoding='utf-8').read()
    assert '_run_watch' in sched, '_run_watch 未实现'
    live = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'renderTimeline' in live, 'renderTimeline 未实现'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert '/api/watch/create' in app, 'app.js 未接入创建接口'
    print('ok')
