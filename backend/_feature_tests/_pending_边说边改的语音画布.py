"""新功能验收断言：边说边改的语音画布

摩擦点「同一需求被重复发送」「过程过长/碎步」；预判「继续做行程类文档交付」

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
    from backend import composite

    assert hasattr(composite, 'apply_card_op'), 'composite 需有 apply_card_op'

    card = {'type': 'table', 'rows': [['a', 1], ['b', 2], ['c', 3]], 'color': 'black'}

    # 删除第 2 行
    op_del = {'action': 'delete_row', 'index': 1}
    out = composite.apply_card_op(card, op_del)
    assert len(out['rows']) == 2 and out['rows'][0][0] == 'a' and out['rows'][1][0] == 'c', '删除行失败'

    # 改颜色
    op_color = {'action': 'set_color', 'color': 'red'}
    out2 = composite.apply_card_op(card, op_color)
    assert out2['color'] == 'red', '改色失败'

    # 原卡片不被就地修改（不可变）
    assert card['color'] == 'black' and len(card['rows']) == 3, 'apply_card_op 不应就地修改入参'

    # 前端渲染函数存在性（不执行浏览器 DOM，仅校验导出）
    import re
    src = open('frontend/studio.js', encoding='utf-8').read()
    assert 'renderCanvas' in src, 'studio.js 缺 renderCanvas'
    assert 'applyVoiceEdit' in src, 'studio.js 缺 applyVoiceEdit'
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['composite']),
    ]
    _bad = []
    for _mod, _fns in _pairs:
        if not _wo.path.isfile(_wo.path.join(_be, _mod + '.py')):
            continue
        _hit = False
        for _fn in _fns:
            for _f in _wo.listdir(_be):
                if not _f.endswith('.py') or _f == _mod + '.py':
                    continue
                try:
                    _s = open(_wo.path.join(_be, _f), encoding='utf-8', errors='ignore').read()
                except Exception:
                    continue
                if _wr.search(r'\b' + _wr.escape(_fn) + r'\s*\(', _s):
                    _hit = True
                    break
            if _hit:
                break
        if not _hit:
            _bad.append('%s 里的 %s 没有被任何其他模块调用（只 import 不算接线）'
                        % (_mod, '/'.join(_fns)))
    assert not _bad, '功能未接入主链路：' + '；'.join(_bad)

def _with_wiring(fn):
    def _wrapped():
        fn()
        _wiring_check()
    return _wrapped

# ⚠️ 用**装饰器包装**而不是再写一个 def run：
#   2026-11-15 实测，重定义 run 会被静态自检判成「同名函数定义两次」（mid），
#   而且模型自己若也写了 _orig_run 会互相覆盖。包装法只保留模型那一个 run。
if callable(globals().get('run')):
    run = _with_wiring(run)
else:
    def run():
        _wiring_check()
