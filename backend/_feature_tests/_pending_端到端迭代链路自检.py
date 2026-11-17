"""新功能验收断言：端到端迭代链路自检

链路验证靠人工读代码，容易漏接线

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os
    sys.path.insert(0, os.path.abspath('.'))
    from backend.chaincheck import run
    r = run()
    assert isinstance(r, dict)
    assert 'all_ok' in r
    assert r['all_ok'] is True
    assert 'imports_taskvec' in r and r['imports_taskvec'] is True
    assert 'imports_workvec' in r and r['imports_workvec'] is True
    assert 'prompt_has_brief' in r and r['prompt_has_brief'] is True
    assert 'has_execute_approved' in r and r['has_execute_approved'] is True
    print('chaincheck OK')
