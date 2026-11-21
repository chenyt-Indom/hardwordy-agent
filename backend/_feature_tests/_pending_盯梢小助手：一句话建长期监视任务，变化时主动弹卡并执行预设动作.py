"""新功能验收断言：盯梢小助手：一句话建长期监视任务，变化时主动弹卡并执行预设动作

对应摩擦点「同一需求被重复发送」75 次 + 预判「让助手主动盯任务」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, tempfile
    sys.path.insert(0, 'backend')
    import watch

    rec = watch.create_from_text('盯着 /tmp/x.log，有变化就告诉我')
    assert rec is not None
    assert rec.get('target') == '/tmp/x.log'
    assert rec.get('frequency')
    assert rec.get('action')

    # 注册到 scheduler 后应能查到
    from backend import scheduler
    scheduler.register_watch(rec)
    assert any(getattr(j, 'watch_id', None) == rec.get('id') for j in scheduler.list_jobs())

    # 变化检测：内容变化应触发回调
    fired = []
    rec2 = watch.create_from_text('盯着 /tmp/y.log，有变化就告诉我')
    rec2['on_change'] = lambda old, new: fired.append((old, new))
    watch.check_once(rec2, old_content='a', new_content='b')
    assert len(fired) == 1
    watch.check_once(rec2, old_content='b', new_content='b')
    assert len(fired) == 1

    print('watch assertions OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['scheduler']),
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
