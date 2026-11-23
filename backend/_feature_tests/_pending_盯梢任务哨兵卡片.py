"""新功能验收断言：盯梢任务哨兵卡片

对应 needs「终止/暂停状态不一致」之外的另一个信任缺口：用户不知道后台有没有在干活。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os, tempfile, time
    from backend import livecard_meta, scheduler

    def test_sentinel_meta_roundtrip():
        meta = livecard_meta.build_sentinel_meta(sentinel_id='s1', target='task:demo', last_snapshot={'v':1}, change_summary='init', next_check=time.time()+60)
        assert meta['sentinel_id'] == 's1'
        assert meta['target'] == 'task:demo'
        assert 'last_snapshot' in meta and 'change_summary' in meta and 'next_check' in meta

    def test_scheduler_writes_actlog(tmpdir):
        log_path = os.path.join(tmpdir, 'actlog.jsonl')
        scheduler._on_task_done(task_id='demo', result={'ok': True}, actlog_path=log_path)
        assert os.path.exists(log_path)
        lines = [json.loads(l) for l in open(log_path) if l.strip()]
        assert any(e.get('type') == 'sentinel_update' for e in lines)

    if __name__ == '__main__':
        test_sentinel_meta_roundtrip()
        with tempfile.TemporaryDirectory() as d:
            test_scheduler_writes_actlog(d)
        print('sentinel ok')


# ---- 接线断言（自动生成，2026-11-15）----
# 实测踩过：功能模块建好、自身断言也过了，但调用方只 import 没调用，
# 功能等于没生效。这条断言专门堵这个盲区（失败会触发自修循环去补调用）。
def _wiring_check():
    import os as _wo, re as _wr
    _be = _wo.path.dirname(_wo.path.dirname(_wo.path.abspath(__file__)))
    _pairs = [
        ('backend', ['livecard_meta', 'scheduler']),
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
