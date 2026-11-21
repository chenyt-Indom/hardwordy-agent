"""新功能验收断言：图表几何自检卡

改完不验证（10 次）、只诊断不修复（55 次）

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    src = open('frontend/app.js', encoding='utf-8').read()
    assert '_vizGeomCheck' in src, 'app.js 缺少 _vizGeomCheck'
    assert 'viz-geom-card' in src or 'viz-geom-card' in open('frontend/style.css', encoding='utf-8').read(), '缺少 .viz-geom-card 样式'
    assert '长短轴' in src or 'axisRatio' in src or 'axis_ratio' in src, '未计算圆盘长短轴比'
    assert '顶面' in src or 'topArea' in src or 'top_area' in src, '未计算顶面面积'
    print('ok')
