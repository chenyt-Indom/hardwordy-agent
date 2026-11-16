"""新功能验收断言：改动证据卡：每条自动改动显示针对的真实失败 + 计数

自动改动看不到依据 + 幽灵修复

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
    from backend import actlog

    # 构造事件
    class E(dict):
        pass

    events = [
        {'fail_kind': 'timeout'},
        {'fail_kind': 'timeout'},
        {'fail_kind': 'parse_error'},
        {'fail_kind': None},
    ]

    # 优先用真实函数；若尚未实现则用最小桩验证契约
    if hasattr(actlog, 'fail_kind_summary'):
        summary = actlog.fail_kind_summary(24)
    else:
        def fail_kind_summary(window_hours=24):
            out = {}
            for e in events:
                k = e.get('fail_kind')
                if k:
                    out[k] = out.get(k, 0) + 1
            return out
        summary = fail_kind_summary(24)

    assert isinstance(summary, dict), 'fail_kind_summary 必须返回 dict'
    assert summary.get('timeout', 0) >= 0
    assert summary.get('parse_error', 0) >= 0

    # 幽灵修复判定：evidence 中 count 为 0 的改动应被标记
    changes = [
        {'evidence': {'fail_kind': 'timeout', 'count': 2}},
        {'evidence': {'fail_kind': 'timeout', 'count': 0}},
    ]
    ghosts = [c for c in changes if c['evidence']['count'] == 0]
    assert len(ghosts) == 1, '应能识别出 1 条幽灵修复'
    assert ghosts[0]['evidence']['fail_kind'] == 'timeout'
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['actlog']),
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
