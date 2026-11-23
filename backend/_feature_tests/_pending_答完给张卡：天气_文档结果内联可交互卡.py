"""新功能验收断言：答完给张卡：天气/文档结果内联可交互卡

信息呈现单一，用户要从文字里自己拼

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, types
    # 模拟前端不可用，只验证后端卡片元数据契约
    sys.path.insert(0, 'backend')
    try:
        from backend import livecard_meta
    except Exception as e:
        raise AssertionError('livecard_meta 无法导入: %r' % e)
    weather_result = {'city': '北京', 'days': [{'date': '2025-01-01', 'high': 5, 'low': -3}, {'date': '2025-01-02', 'high': 6, 'low': -2}, {'date': '2025-01-03', 'high': 4, 'low': -4}, {'date': '2025-01-04', 'high': 7, 'low': -1}, {'date': '2025-01-05', 'high': 8, 'low': 0}]}
    card = livecard_meta.build_card('weather', weather_result)
    assert isinstance(card, dict), 'build_card 应返回 dict'
    assert card.get('type') == 'weather', '卡片类型应为 weather'
    payload = card.get('payload') or {}
    assert isinstance(payload.get('days'), list) and len(payload['days']) == 5, '天气卡应含 5 天数据'
    doc_card = livecard_meta.build_card('doc', {'pages': 12, 'thumb': 'x.png', 'title': 't'})
    assert doc_card.get('type') == 'doc' and doc_card['payload'].get('pages') == 12, '文档卡应含页数'
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['livecard_meta']),
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
