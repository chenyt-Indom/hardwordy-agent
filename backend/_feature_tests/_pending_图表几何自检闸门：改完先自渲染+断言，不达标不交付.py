"""新功能验收断言：图表几何自检闸门：改完先自渲染+断言，不达标不交付

对应 weaknesses「改完渲染器不自检就交付」+ 摩擦点「改完不验证」11 次

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, types
    sys.path.insert(0, 'backend')
    import viz_selfcheck as v

    # 1) 正常实现应通过
    assert v.run_checks() is True or v.run_checks() is None

    # 2) 坏实现（压扁比越界）应被断言拦住
    orig = v._vizPie3D
    v._vizPie3D = lambda *a, **k: {'squash': 0.3}
    try:
        v.run_checks()
        raise SystemExit('expected AssertionError')
    except AssertionError:
        pass
    finally:
        v._vizPie3D = orig

    # 3) 近远柱高比越界也应被拦住
    orig2 = v._vizBar3D
    v._vizBar3D = lambda *a, **k: {'near_far_ratio': 1.5}
    try:
        v.run_checks()
        raise SystemExit('expected AssertionError')
    except AssertionError:
        pass
    finally:
        v._vizBar3D = orig2

    print('viz selfcheck assertions OK')
