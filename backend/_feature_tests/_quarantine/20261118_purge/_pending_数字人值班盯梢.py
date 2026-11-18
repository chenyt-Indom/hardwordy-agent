"""新功能验收断言：数字人值班盯梢

用户反复交代同一件事（盯网页/盯新闻），助手不能自主持续关注

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import os, sys, tempfile, time
    sys.path.insert(0, 'backend')
    from backend.scheduler import add_watch_task, run_watch_task, list_tasks

    d = tempfile.mkdtemp()
    f = os.path.join(d, 'watched.txt')
    open(f, 'w').write('v1')
    task = add_watch_task({'source': f, 'interval': 5})
    assert task is not None
    assert any(t.get('source') == f for t in list_tasks())
    r1 = run_watch_task(task)
    assert r1.get('changed') in (False, None)
    time.sleep(0.05)
    open(f, 'w').write('v2 changed')
    r2 = run_watch_task(task)
    assert r2.get('changed') is True
    assert 'v2' in str(r2.get('diff', '')) or 'v2' in str(r2)
    r3 = run_watch_task(task)
    assert r3.get('changed') is False
    print('ok')
