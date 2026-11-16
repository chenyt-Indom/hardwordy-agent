"""新功能验收断言：批准执行时间线：批准→执行→结果全程可见

批准后无执行反馈 + 迭代失败原因不透明

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, tempfile, json
    sys.path.insert(0, os.path.abspath('backend'))
    from backend import actlog

    # ⚠️ 2026-11-17 修：原来写死 plan_id='test-plan-timeline-001'，
    #    而阶段流水是**追加式** jsonl —— 第一次跑通过，第二次就跑出 6 条，
    #    断言 names == stages 必然失败（实测跑到第 9 轮攒了 27 条）。
    #    测试必须幂等：每次用唯一 id，跑完把自己的流水删掉。
    import uuid
    plan_id = 'test-plan-timeline-%s' % uuid.uuid4().hex[:10]
    stages = ['queued', 'agent_step', 'verify_pass']
    try:
        for s in stages:
            actlog.record_stage(plan_id, s, detail=str(s))

        events = actlog.get_stages(plan_id) if hasattr(actlog, 'get_stages') else None
        assert events is not None, 'actlog 缺少按 plan_id 读取阶段事件的接口'
        names = [e.get('stage') or e.get('name') for e in events]
        assert names == stages, f'阶段事件顺序不符: {names}'
        assert all('ts' in e or 'timestamp' in e for e in events), '阶段事件缺少时间戳'
        print('OK timeline')
    finally:
        # 清理：不留测试流水，避免污染真实日志
        try:
            import os as _o, glob as _g
            _d = _o.path.join(_B, '..', 'data', 'actlog', 'stages')
            for _f in _g.glob(_o.path.join(_d, plan_id + '*')):
                _o.remove(_f)
        except Exception:
            pass


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
