"""新功能验收断言：记忆桌面：把长期/短期记忆做成可视化卡片墙，可拖拽钉住合并，助手据此提前备好下一步草稿

对应「记忆层是黑盒、用户无法确认记住了什么」的痛点，以及用户对「提前准备下一步」的潜在需求。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    from backend import memcore, hive

    memcore.add('用户专业是建筑学', source='profile')
    memcore.add('用户在做 PPT 项目', source='project')
    cards = hive.list_cards()
    assert len(cards) >= 2
    for c in cards:
        assert set(['id', 'content', 'source', 'hits']).issubset(c.keys())
    cid = cards[0]['id']
    hive.pin_card(cid)
    assert any(c['id'] == cid and c.get('pinned') for c in hive.list_cards())
    before = len(hive.list_cards())
    a, b = cards[0]['id'], cards[1]['id']
    hive.merge_cards(a, b)
    assert len(hive.list_cards()) == before - 1
    print('memory desk ok')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['memcore', 'hive']),
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
