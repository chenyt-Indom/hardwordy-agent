"""新功能验收断言：专家路由可解释卡片

预判「评估是否引入多模型/专家路由」；痛点「只诊断不修复」——已落地但用户看不见

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

    # 1) expert_router.route 返回 reason
    from backend import expert_router
    res = expert_router.route('帮我写一段 Python 排序代码')
    assert isinstance(res, dict), 'route 应返回 dict'
    assert 'reason' in res and isinstance(res['reason'], str) and res['reason'].strip(), 'route 必须带非空 reason'
    assert 'expert' in res, 'route 必须带 expert'

    # 2) deepseek-auto 分支事件结构（用桩替换真实 LLM 调用，只验证组装逻辑）
    from backend import main
    assert hasattr(main, 'build_expert_card') or hasattr(main, 'deepseek_auto_event'), 'main 需暴露卡片组装函数'
    build = getattr(main, 'build_expert_card', None) or getattr(main, 'deepseek_auto_event')
    evt = build({'expert': 'coder', 'reason': '任务含代码关键词', 'temperature': 0.2})
    assert isinstance(evt, dict)
    for k in ('expert', 'reason', 'temperature'):
        assert k in evt, f'事件缺字段 {k}'
    assert evt['reason'].strip(), 'reason 不能为空'
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['expert_router']),
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
