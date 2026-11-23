"""新功能验收断言：一句话触发「查-修-验-清-评」闭环

对应摩擦点「同一需求被重复发送」106 次。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, time
    sys.path.insert(0, 'backend')
    from backend.iterhealth import run_full_cycle

    t0 = time.time()
    result = run_full_cycle(budget_seconds=30)
    elapsed = time.time() - t0
    assert elapsed < 60
    assert isinstance(result, dict)
    for stage in ['check', 'fix', 'verify', 'clean', 'evaluate']:
        assert stage in result, stage
    assert result['verify']['ran_subset'] is True
    assert result['verify']['elapsed'] <= 30
    print('cycle ok')
