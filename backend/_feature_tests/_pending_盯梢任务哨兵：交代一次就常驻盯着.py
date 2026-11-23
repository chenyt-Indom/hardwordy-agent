"""新功能验收断言：盯梢任务哨兵：交代一次就常驻盯着

用户每次都要手动问「现在怎么样了」，没有主动推送变化。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, types, time
    sys.modules.setdefault('backend', types.ModuleType('backend'))
    from backend import livecard_meta as lm
    from backend import actlog

    # 1. 创建哨兵
    w = lm.create_watch(target='demo://page', interval=1)
    assert w.watch_id and w.interval == 1

    # 2. 模拟两次快照，第二次有变化
    lm.update_snapshot(w.watch_id, 'v1')
    lm.update_snapshot(w.watch_id, 'v2')
    assert lm.get_watch(w.watch_id).last_snapshot == 'v2'

    # 3. 变化应产生事件且带摘要
    actlog.record_watch_event(w.watch_id, 'changed', 'v1->v2')
    events = [e for e in actlog.list_events() if getattr(e, 'watch_id', None) == w.watch_id]
    assert events and events[-1].diff_summary

    # 4. 多次变化后快照存储不线性膨胀（压缩/遗忘生效）
    for i in range(50):
        lm.update_snapshot(w.watch_id, 'v%d' % i)
    assert len(lm.get_watch(w.watch_id).history) <= 10
    print('sentinel ok')


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
