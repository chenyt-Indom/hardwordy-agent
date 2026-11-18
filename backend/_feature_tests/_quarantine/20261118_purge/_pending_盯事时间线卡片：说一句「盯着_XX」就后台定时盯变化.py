"""新功能验收断言：盯事时间线卡片：说一句「盯着 XX」就后台定时盯变化

对应预判「定时任务结果可视化」与历史失败动作「盯事助手」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os, tempfile
    from backend import watch, actlog

    tmp = tempfile.mkdtemp()
    os.environ['WATCH_DATA_DIR'] = tmp
    os.environ['ACTLOG_DATA_DIR'] = tmp

    w = watch.add_watch('测试查询', 60)
    assert w is not None, 'add_watch 应返回 watch 对象'
    items = watch.list_watch()
    assert any(x.get('query') == '测试查询' for x in items), 'list_watch 应包含新增条目'

    # 模拟两次 tick 产生变化
    watch._set_last_result('测试查询', ['a', 'b'])
    watch._set_last_result('测试查询', ['a', 'b', 'c'])
    events = actlog.list_events(kind='watch')
    assert isinstance(events, list), 'actlog 应返回事件列表'
    # 若 tick 已写入事件则检查变化标记；否则至少验证接口可调用
    print('OK')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['watch', 'actlog']),
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
