"""新功能验收断言：回复形态闸门：结论前置 + 过程折叠 + 空输出兜底

只诊断不修复14次、回复被截断13次、空回复3次

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    from backend import reply_gate

    # 空输出兜底
    out = reply_gate.sanitize('')
    assert isinstance(out, str) and len(out.strip()) > 0, 'empty reply must get fallback'
    assert '下一步' in out or '已知' in out, 'fallback must state known+next'

    # 内部标记剥离
    raw = '结论：已修复。\n🧠 本次没有思考内容\n归位\n过程细节...'
    out2 = reply_gate.sanitize(raw)
    assert '🧠 本次没有思考内容' not in out2
    assert '归位' not in out2
    assert '结论' in out2, 'conclusion must be preserved'

    # 超长折叠且保留结论段
    long_raw = '结论：根因是 X。\n' + ('过程行\n' * 5000)
    out3 = reply_gate.sanitize(long_raw)
    assert len(out3) < len(long_raw), 'long reply must be folded'
    assert '结论' in out3, 'conclusion must survive folding'
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['reply_gate']),
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
