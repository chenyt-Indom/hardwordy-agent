"""新功能验收断言：盯事助手：一句话登记监控，后台盯变化出时间线

对应预判「让助手在后台盯事」；也回应「主动愿意去思考」的原话诉求。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location('sched', 'backend/scheduler.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert hasattr(m, 'register_watch') and hasattr(m, 'run_watch'), 'scheduler 缺少 watch 接口'
    src = open('backend/main.py', encoding='utf-8').read()
    for r in ['/watch/add', '/watch/list', '/watch/events']:
        assert r in src, f'main.py 缺少路由 {r}'
    act = open('backend/actlog.py', encoding='utf-8').read()
    assert 'log_watch_event' in act, 'actlog 缺少 log_watch_event'
    assert 'livecard' in open('frontend/index.html', encoding='utf-8').read(), 'index.html 未引入 livecard'
    print('watch ok')
