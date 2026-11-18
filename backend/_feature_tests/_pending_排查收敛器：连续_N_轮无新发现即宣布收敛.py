"""新功能验收断言：排查收敛器：连续 N 轮无新发现即宣布收敛

对应 weaknesses「再查无终止条件」和摩擦点「同一需求被重复发送」63 次。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys
    sys.path.insert(0, 'backend')
    from backend.reverify import ConvergenceTracker

    t = ConvergenceTracker(window=2)
    t.record(3)
    assert t.status()['converged'] is False
    t.record(0)
    assert t.status()['converged'] is False
    t.record(0)
    s = t.status()
    assert s['converged'] is True and s['rounds'] == 3
    t.record(1)
    assert t.status()['converged'] is False
    print('convergence ok')
