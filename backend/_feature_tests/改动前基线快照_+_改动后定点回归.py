"""新功能验收断言：改动前基线快照 + 改动后定点回归

同一文件反复改、回滚7次、熔断4次

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""

def run():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))
    import types
    from snapdiff import snapshot, diff
    m = types.ModuleType('m')
    m.add = lambda a, b: a + b
    before = snapshot(m, ['add'])
    after_same = snapshot(m, ['add'])
    assert diff(before, after_same) == [], diff(before, after_same)
    m.add = lambda a, b: str(a + b)
    after_changed = snapshot(m, ['add'])
    d = diff(before, after_changed)
    assert len(d) >= 1, d
    print('ok')
