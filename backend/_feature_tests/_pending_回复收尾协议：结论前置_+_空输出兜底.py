"""新功能验收断言：回复收尾协议：结论前置 + 空输出兜底

诊断轮次无结论、空回复、长输出截断

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
    from reply_gate import format_reply

    cases = [
        ('', 'empty'),
        ('。。。', 'punct'),
        ('A' * 2000, 'long'),
        ('我看一下这个日志', 'unfinished'),
    ]
    for text, kind in cases:
        out = format_reply(text)
        assert isinstance(out, str) and out.strip(), f'{kind} 输出为空'
        if kind in ('empty', 'punct'):
            assert '当前已知' in out and '下一步要查' in out, f'{kind} 未兜底: {out[:80]}'
        if kind == 'long':
            assert out.lstrip().startswith('结论') or '结论' in out.split('\n')[0], f'long 未结论前置: {out[:80]}'
        if kind == 'unfinished':
            assert '结论：' in out, f'unfinished 未强制收尾: {out[-80:]}'
    print('reply_finalize assertions passed')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('reply_gate', ['format_reply']),
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

# ⚠️ 用装饰器包装而不是再写一个 def run：重定义会被静态自检判成「同名函数定义两次」。
if callable(globals().get('run')):
    run = _with_wiring(run)
