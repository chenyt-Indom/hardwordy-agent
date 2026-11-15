"""新功能验收断言：改动卡片展示真实变更摘要

用户看不到改了什么，只能看到文件名

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, json
    sys.path.insert(0,'backend')
    from backend import actlog

    # 构造一个含 patches 的 act 记录，调用 _log_act 后检查 changelog
    patches=[{'file':'backend/x.py','find':'def old():\n    return 1','replace':'def new():\n    return 2'}]
    rec={'action':'edit','patches':patches,'files':['backend/x.py']}
    try:
        out=actlog._log_act(rec)
    except TypeError:
        # 兼容不同签名：尝试关键字
        out=actlog._log_act(action='edit', patches=patches, files=['backend/x.py'])
    cl=out.get('changelog') if isinstance(out,dict) else None
    assert cl, 'changelog must be generated: %r' % (out,)
    assert 'old' in cl or 'new' in cl or 'def' in cl, 'changelog must summarize find/replace: '+str(cl)

    # 前端渲染：读取含 changelog 的卡片应输出摘要
    src=open('frontend/app.js',encoding='utf-8').read()
    assert 'changelog' in src, 'app.js must reference changelog field'
    print('ok')


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
