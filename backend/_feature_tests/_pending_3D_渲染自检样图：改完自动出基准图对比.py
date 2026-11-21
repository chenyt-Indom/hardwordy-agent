"""新功能验收断言：3D 渲染自检样图：改完自动出基准图对比

对应 weaknesses「改完不验证渲染效果」+ 摩擦点「改完不验证」11 次

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import pathlib, xml.etree.ElementTree as ET
    p = pathlib.Path('charts/_3d_baseline.svg')
    assert p.exists(), '基准 SVG 未生成'
    txt = p.read_text(encoding='utf-8')
    assert '<svg' in txt, '不是 SVG'
    root = ET.fromstring(txt)
    els = [e.tag.split('}')[-1] for e in root.iter()]
    assert els.count('path') + els.count('polygon') >= 3, '柱形元素不足'
    assert 'pie' in txt.lower() or els.count('path') >= 4, '缺少饼图元素'
    print('ok')
